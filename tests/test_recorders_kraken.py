import json
from decimal import Decimal

from conftest import FIXTURES, load_fixture

from kbtc.recorders.base import ASK, BID, OrderIdBook, kraken_crc32
from kbtc.recorders.kraken import KrakenProcessor


def _load_raw_lines(venue: str, name: str) -> list[str]:
    """Return the fixture's raw JSON lines UNPARSED, as the recorder stored them on the wire.

    Feeding the raw strings (not pre-parsed dicts) is what exercises the exact-digit parsing in
    ``KrakenProcessor`` — the real bug only shows when ``limit_price``/``order_qty`` arrive as JSON
    numbers with trailing zeros, which ``json.loads`` would turn into precision-losing floats.
    """
    path = FIXTURES / venue / name
    return [ln for ln in path.read_text().splitlines() if ln.strip()]

# Official worked example from Kraken's L3 CRC32 guide (VERIFIED via WebFetch 2026-10-16:
# https://docs.kraken.com/exchange/guides/websockets/l3-checksum-v2). The input order book
# below is quoted from that page; its documented checksum is 1063832831. This is an EXTERNAL
# oracle guarding against a wrong-but-self-consistent CRC. NOTE on the two fixtures: `level3.jsonl`
# checksums ARE self-generated (a regression guard only), but `level3_real.jsonl` carries Kraken's
# OWN transmitted checksums from a real recording — a second external oracle. Do NOT regenerate the
# latter with kraken_crc32; that would make test_kraken_crc32_matches_real_recording circular.
_ORACLE_BIDS = [
    ("44939.4", ["0.88968699", "0.45210000", "0.10000000", "0.14296323", "0.25000000",
                 "0.10292988", "0.33880000", "1.28140860"]),
    ("44937.1", ["0.03346877"]),
    ("44934.7", ["0.35630000"]),
    ("44930.2", ["0.22734299", "0.01000000", "0.05550000", "0.70000000", "0.15000000"]),
    ("44928.0", ["0.00105240"]),
    ("44919.6", ["0.33870000"]),
    ("44919.5", ["0.07610000"]),
    ("44912.0", ["0.35630000"]),
    ("44909.7", ["0.06690000"]),
    ("44901.9", ["0.00088982"]),
]
_ORACLE_ASKS = [
    ("44939.5", ["4.52308393", "0.00111261", "0.00100000", "0.01000000"]),
    ("44950.0", ["0.10334926"]),
    ("44953.0", ["0.00064537"]),
    ("44955.0", ["0.00250000"]),
    ("44959.6", ["0.35630000", "0.35630000"]),
    ("44960.1", ["0.00338072"]),
    ("44960.2", ["0.88967575"]),
    ("44967.0", ["3.14392283"]),
    ("44978.5", ["0.06778960"]),
    ("44979.2", ["0.35630000"]),
]


def test_kraken_crc32_matches_official_worked_example():
    b = OrderIdBook()
    i = 0
    for px, qtys in _ORACLE_BIDS:
        for q in qtys:
            b.add(f"b{i}", BID, px, q)
            i += 1
    for px, qtys in _ORACLE_ASKS:
        for q in qtys:
            b.add(f"a{i}", ASK, px, q)
            i += 1
    assert kraken_crc32(b) == 1063832831


def test_level3_checksum_clean_then_mismatch_gap():
    msgs = load_fixture("kraken", "level3.jsonl")
    p = KrakenProcessor(product="BTC/USD", depth=10)
    ts = 1_760_000_000_000_000_000

    # snapshot + one good update: checksums match our CRC32, book reconstructs, no gap.
    p.process(msgs[0], ts)
    p.process(msgs[1], ts + 1)
    assert not p.gaps
    assert p.synced is True
    assert p.book.best_bid() == Decimal("99.8")  # b3 added
    assert "b2" not in p.book.orders  # b2 deleted
    assert p.book.best_ask() == Decimal("100.5")

    # Third update carries a deliberately WRONG checksum (seeded gap) -> detected + resync.
    p.process(msgs[2], ts + 2)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "checksum mismatch"
    assert p.needs_resnapshot is True


def test_kraken_crc32_matches_real_recording():
    """Replay a trimmed slice of a REAL recorded Kraken level3 hour and reproduce the venue's
    transmitted CRC32 on every update — the acceptance for the ROADMAP 2.1a(c) replay fix.

    The slice (``level3_real.jsonl``) is two real connection segments pulled from the VPS box:
    each opens with its OWN ``snapshot`` frame (the processor re-anchors the book at each, per the
    "fresh book at each snapshot" rule), then streams real ``update`` frames. It deliberately
    includes the two edge cases that the old hand-built string fixture never exercised and that
    made OFFLINE replay diverge (~8.5k false checksum gaps/hour) while the LIVE book looked fine:

    1. **Exact transmitted digits.** Real qtys carry trailing zeros (e.g. ``0.00010000``) and are
       sent as JSON *numbers*; the old path parsed them to floats and ``str()`` dropped the zeros
       (``0.00479590`` → ``0.0047959``), corrupting the CRC token. Feeding raw strings here forces
       the exact-digit parse.
    2. **Time-priority queue order.** When a price level re-enters the top-10 (several orders added
       at one price) or a ``modify`` raises qty / changes price (fresh timestamp → priority lost),
       the CRC orders the level by order ``timestamp``, not by our insertion order.

    If any update's reconstructed CRC32 diverged from the frame's ``checksum`` the processor would
    emit a ``GapDetected``; asserting zero gaps over the whole slice is the regression guard.
    Prices/qtys stay exact (Decimal / verbatim transmitted strings) throughout.
    """
    lines = _load_raw_lines("kraken", "level3_real.jsonl")
    assert lines, "real-slice fixture is empty"
    p = KrakenProcessor(product="BTC/USD", depth=10)

    n_updates = n_snapshots = 0
    ts = 1_760_000_000_000_000_000
    for i, raw in enumerate(lines):
        mtype = json.loads(raw)["type"]
        p.process(raw, ts + i)  # feed the RAW string, as the live/replay path does
        if mtype == "snapshot":
            n_snapshots += 1
            assert p.synced is True
        else:
            n_updates += 1
        # Every frame carried a checksum; a divergence would have recorded a gap on this frame.
        assert not p.gaps, f"checksum gap at frame {i}: {p.gaps[-1]}"

    assert n_snapshots == 2  # two separate connection segments, each self-anchoring
    assert n_updates >= 30
    # Book is non-empty and internally consistent (best bid strictly below best ask here).
    assert p.book.best_bid() is not None and p.book.best_ask() is not None
    assert isinstance(p.book.best_bid(), Decimal)
    assert p.book.best_bid() < p.book.best_ask()


def test_resync_resubscribe_snapshot_rebuilds():
    msgs = load_fixture("kraken", "level3.jsonl")
    p = KrakenProcessor(product="BTC/USD", depth=10)
    p.process(msgs[2], 1)  # pretend diverged
    # A fresh snapshot (resubscribe snapshot:true) rebuilds and clears the resync flag.
    p.apply_snapshot(msgs[0], 2)
    assert p.needs_resnapshot is False
    assert p.book.best_bid() == Decimal("99.5")
