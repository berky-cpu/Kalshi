"""Replay test for the Bullish Exchange public ``l2Orderbook`` (L2) adapter (ROADMAP 1.1-series).

Drives the pure processor from a fixture built to the primary-doc schema (the public market-data
orderbook page + the official ``bullish-exchange/api-examples`` sample, read 2026-10-21) and
CONFIRMED on the live wire by a read-only probe from BOTH the Ashburn-US and EU boxes
(``wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook``, ``l2Orderbook`` BTCUSD,
2026-10-21): a subscribe ack (control), full-book ``type:"snapshot"`` frames (``dataType``
``V1TALevel2``, flat ``bids``/``asks`` arrays) that chain via ``sequenceNumberRange`` first==prev.last+1
(the VERIFIED gap signal), a ``keepalivePing`` pong (control), a snapshot with a sequence-chain break
(logged but self-healed since every snapshot is a complete book), and end-to-end normalizer
completeness with the ``timestamp``-derived ``exch_ts_ns``. The UNVERIFIED incremental ``type:"update"``
path (never seen on the wire) is exercised with inline frames.
"""
from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.base import ASK, BID
from kbtc.recorders.bullish import (
    BullishL2Processor,
    io_config,
    resubscribe_messages,
    subscribe_messages,
)
from kbtc.recorders.health import data_classifier, make_processor
from kbtc.normalize import pipeline

T0 = 1_760_000_000_000_000_000


def test_io_config_and_subscribe_envelope():
    cfg = io_config()
    assert cfg.urls == ["wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook"]
    assert cfg.venue == "bullish" and cfg.channel == "l2_btcusd"
    assert cfg.max_size is None                      # full-book snapshot frame must not be size-rejected
    # Keepalive: the documented ``keepalivePing`` on a fixed cadence inside the 5-min idle window.
    assert cfg.ping_interval_s == 120.0
    assert '"keepalivePing"' in cfg.ping_message

    assert subscribe_messages("BTCUSD") == [{
        "jsonrpc": "2.0", "type": "command", "method": "subscribe",
        "params": {"topic": "l2Orderbook", "symbol": "BTCUSD"}, "id": "1",
    }]
    # Resync forces a fresh snapshot by unsubscribe-then-subscribe (in-band, no REST).
    resub = resubscribe_messages("BTCUSD")
    assert resub[0]["method"] == "unsubscribe"
    assert resub[0]["params"] == {"topic": "l2Orderbook", "symbol": "BTCUSD"}
    assert resub[1]["method"] == "subscribe"


def test_snapshot_seed_apply_and_best_bid_ask():
    msgs = load_fixture("bullish", "l2.jsonl")
    p = BullishL2Processor()

    # msgs[0] subscribe ack (has ``result``, no ``dataType``): not a book frame.
    assert p.process(msgs[0], T0) == []
    assert not p.synced and not p.gaps

    # msgs[1] SNAPSHOT (full book, flat arrays) → COMPLETE book, anchors ``last_seq``.
    assert p.process(msgs[1], T0 + 1) == [{"kind": "snapshot"}]
    assert p.synced and not p.gaps and p.last_seq == 1000
    assert p.book.levels(BID) == [(Decimal("100.00"), Decimal("1.0")),
                                  (Decimal("99.00"), Decimal("2.0"))]
    assert p.book.levels(ASK) == [(Decimal("101.00"), Decimal("1.5")),
                                  (Decimal("102.00"), Decimal("2.0"))]
    assert p.book.best_bid() == Decimal("100.00")
    assert p.book.best_ask() == Decimal("101.00")

    # msgs[2] SNAPSHOT chaining (first=1001 == prev.last+1): full REPLACE → best bid 100.50, 99 gone.
    assert p.process(msgs[2], T0 + 2) == [{"kind": "snapshot"}]
    assert p.book.best_bid() == Decimal("100.50")
    assert Decimal("99.00") not in p.book.bids
    assert p.book.asks[Decimal("101.00")] == Decimal("2.0")
    assert p.last_seq == 1003 and not p.gaps


def test_ack_and_keepalive_pong_are_not_data():
    """The subscribe ack and the ``keepalivePing`` pong carry ``result``/no ``dataType``; they are
    control, not book frames, and the data-classifier must NOT count them as DATA."""
    msgs = load_fixture("bullish", "l2.jsonl")
    p = BullishL2Processor()
    p.process(msgs[1], T0)                            # snapshot baseline
    best_before = (p.book.best_bid(), p.book.best_ask())

    pong = msgs[3]
    assert pong["result"]["message"] == "Keep alive pong"
    assert p.process(pong, T0 + 1) == []              # pong: not a book frame
    assert p.process(msgs[0], T0 + 2) == []           # subscribe ack: not a book frame
    assert (p.book.best_bid(), p.book.best_ask()) == best_before and not p.gaps

    clf = data_classifier("bullish")
    assert clf(pong) is False                         # keepalive pong is liveness, NOT data
    assert clf(msgs[0]) is False                      # subscribe ack is control, NOT data
    assert clf(msgs[1]) is True                       # a V1TALevel2 book frame IS data


def test_sequence_gap_on_snapshot_logged_but_self_heals():
    """Each L2 frame is a FULL snapshot, so a ``sequenceNumberRange`` chain break is LOGGED (never
    silent) yet the book self-heals from that same frame — no resync is armed."""
    msgs = load_fixture("bullish", "l2.jsonl")
    p = BullishL2Processor()
    for i in (1, 2):                                  # snapshot, chaining snapshot
        p.process(msgs[i], T0 + i)
    assert p.synced and not p.gaps and p.last_seq == 1003

    # msgs[4]: first=9999 != last_seq+1=1004 ⇒ a dropped-batch GAP on the VERIFIED seq chain.
    out = p.process(msgs[4], T0 + 4)
    assert out == [{"kind": "snapshot"}]              # still applied (snapshot rebuilds fully)
    assert len(p.gaps) == 1 and p.gaps[0].reason == "sequence gap (snapshot)"
    assert p.gaps[0].expected == 1004 and p.gaps[0].got == 9999
    assert p.needs_resnapshot is False                # self-healed: the book is whole, no resync
    assert p.synced is True and p.last_seq == 9999
    assert p.book.bids == {Decimal("200.00"): Decimal("4.0")}
    assert p.book.asks == {Decimal("201.00"): Decimal("5.0")}

    # msgs[5] chains (first=10000 == prev.last+1): applies, no new gap.
    assert p.process(msgs[5], T0 + 5) == [{"kind": "snapshot"}]
    assert p.book.bids == {Decimal("200.00"): Decimal("4.5")}
    assert len(p.gaps) == 1


def _snap(seq0, seq1, bids, asks, ts="1"):
    return {"type": "snapshot", "dataType": "V1TALevel2",
            "data": {"symbol": "BTCUSD", "bids": bids, "asks": asks,
                     "sequenceNumberRange": [seq0, seq1], "timestamp": ts}}


def _update(seq0, seq1, bids, asks):
    return {"type": "update", "dataType": "V1TALevel2",
            "data": {"symbol": "BTCUSD", "bids": bids, "asks": asks,
                     "sequenceNumberRange": [seq0, seq1], "timestamp": "1"}}


def test_update_path_incremental_apply_removal_and_seq_gap():
    """UNVERIFIED defensive incremental path (``type:"update"``, never observed on the wire): an
    absolute-SET per level with ``qty "0"`` removal, chained on the seq range, and a chain break that
    arms the in-band resync and drops stale updates until a fresh snapshot re-anchors."""
    p = BullishL2Processor()
    p.process(_snap(1000, 1000, ["100.0", "1.0"], ["101.0", "1.5"]), T0)
    assert p.synced and not p.gaps and p.last_seq == 1000

    # Chaining update (first=1001): add bid 100.5, and qty "0" REMOVES the ask at 101.0.
    assert p.process(_update(1001, 1001, ["100.5", "0.3"], ["101.0", "0"]), T0 + 1) \
        == [{"kind": "book", "seq": 1001}]
    assert p.book.bids[Decimal("100.5")] == Decimal("0.3")
    assert Decimal("101.0") not in p.book.asks
    assert p.last_seq == 1001 and not p.gaps

    # Update with a seq break (first=5000 != 1002) ⇒ gap, resync armed, update DROPPED, book frozen.
    bids_before = dict(p.book.bids)
    assert p.process(_update(5000, 5000, ["200.0", "9.0"], []), T0 + 2) == []
    assert len(p.gaps) == 1 and p.gaps[0].reason == "sequence gap (update)"
    assert p.gaps[0].expected == 1002 and p.gaps[0].got == 5000
    assert p.needs_resnapshot is True and p.awaiting_snapshot is True
    assert dict(p.book.bids) == bids_before           # the gap update was NOT applied

    # A stale in-flight update while awaiting the reseed is DROPPED, logs nothing more.
    assert p.process(_update(5001, 5001, ["300.0", "9.0"], []), T0 + 3) == []
    assert len(p.gaps) == 1 and p.awaiting_snapshot is True

    # The fresh reseed SNAPSHOT rebuilds the book and clears every flag, silently.
    assert p.process(_snap(6000, 6000, ["400.0", "4.0"], ["401.0", "5.0"]), T0 + 4) \
        == [{"kind": "snapshot"}]
    assert p.synced and p.needs_resnapshot is False and p.awaiting_snapshot is False
    assert p.book.bids == {Decimal("400.0"): Decimal("4.0")} and p.last_seq == 6000
    assert len(p.gaps) == 1


def test_begin_resync_drops_stale_update_then_reseed():
    """``begin_resync`` (the IO layer) clears the book, resets the chain, and arms awaiting-snapshot;
    a stale pre-reseed update is DROPPED and the fresh snapshot rebuilds."""
    p = BullishL2Processor()
    p.process(_snap(1000, 1000, ["100.0", "1.0"], ["101.0", "1.0"]), T0)
    assert p.synced

    p.begin_resync()
    assert p.awaiting_snapshot is True and not p.book.bids and not p.book.asks and p.last_seq is None

    assert p.process(_update(1001, 1001, ["99.0", "1.0"], []), T0 + 1) == []
    assert not p.book.bids and not p.gaps

    p.process(_snap(2000, 2000, ["200.0", "2.0"], ["201.0", "2.0"]), T0 + 2)
    assert p.synced and p.awaiting_snapshot is False and p.needs_resnapshot is False
    assert p.book.best_bid() == Decimal("200.0") and p.book.best_ask() == Decimal("201.0")


def test_foreign_symbol_frame_ignored():
    """A frame for a different symbol on the multiplexed socket is ignored (no book change)."""
    p = BullishL2Processor()
    p.process(_snap(1000, 1000, ["100.0", "1.0"], ["101.0", "1.0"]), T0)
    other = _snap(1001, 1001, ["1.0", "1.0"], ["2.0", "1.0"])
    other["data"]["symbol"] = "ETHUSD"
    assert p.process(other, T0 + 1) == []
    assert p.book.best_bid() == Decimal("100.0") and p.last_seq == 1000


def test_make_processor_wired():
    proc, prime = make_processor("bullish", "l2_btcusd")
    assert isinstance(proc, BullishL2Processor) and prime is None


def test_normalizer_completeness_and_exch_ts():
    """End-to-end replay: the normalizer emits a COMPLETE (synced=True) book throughout (every frame
    is a full snapshot, so the one seq-chain break self-heals), carries the ``timestamp``-derived
    ``exch_ts_ns``, and emits NO trades (the orderbook channel has no trade tape)."""
    msgs = load_fixture("bullish", "l2.jsonl")
    records = [(T0 + i, m) for i, m in enumerate(msgs)]
    res = pipeline.normalize_crypto("bullish", "l2_btcusd", records, levels=10)

    assert res.books, "expected emitted book snapshots"
    assert all(r["synced"] is True for r in res.books)       # full snapshots keep the book complete
    assert all(r["venue"] == "bullish" and r["product"] == "BTCUSD" for r in res.books)
    assert res.books[0]["exch_ts_ns"] == 1771761038000 * 1_000_000   # from the frame's ``timestamp``
    assert res.trades == []                                  # orderbook channel: no trade tape
