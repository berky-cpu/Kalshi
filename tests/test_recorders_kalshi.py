from conftest import load_fixture

from kbtc.recorders.kalshi import KalshiProcessor

# NOTE: the Kalshi fixture is SYNTHETIC and its schema is UNVERIFIED. ROADMAP 0.4 did not
# cover Kalshi's WS feed; endpoints/auth/subscribe/frame-shape are verified (docs.kalshi.com),
# but the KXBTC15M event-market payload schema (yes/no vs bid/ask, cents-int vs dollar-string)
# and the +1-per-sid seq semantics are NOT confirmed (the /websockets reference was unreachable
# on 2026-10-16). A green test here proves adapter internal consistency, NOT feed validation.
# Resolved by the Kalshi follow-up item (ROADMAP 1.1b) once /websockets is reachable.


def test_orderbook_delta_trade_lifecycle_and_seeded_seq_gap():
    msgs = load_fixture("kalshi", "orderbook.jsonl")
    p = KalshiProcessor(product="KXBTC15M-26OCT16")
    ts = 1_760_000_000_000_000_000
    for i, m in enumerate(msgs):
        p.process(m, ts + i)

    book = p.book_for("KXBTC15M-26OCT16")
    # snapshot yes{40:100,41:250} no{58:120}; +50 on yes40 -> 150; -250 removes yes41.
    assert book["yes"] == {40: 150}
    assert 41 not in book["yes"]
    # The trade (sid 9) and lifecycle were recorded.
    assert p.trades and p.trades[0]["count"] == 5
    assert p.lifecycle and p.lifecycle[0]["status"] == "active"

    # Seeded gap on the orderbook sid (sid 7): seq 4 skipped (2,3 then 5) -> gap + resync.
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "seq jump"
    assert p.gaps[0].expected == 4 and p.gaps[0].got == 5
    assert p.needs_resnapshot is True


def test_snapshot_rebaselines_seq_and_rebuilds():
    p = KalshiProcessor(product="KXBTC15M-26OCT16")
    msgs = load_fixture("kalshi", "orderbook.jsonl")
    # Apply the fresh snapshot as a resync; the seq baseline resets and the flag clears.
    p.apply_snapshot(msgs[0], 1)
    assert p.needs_resnapshot is False
    assert p.book_for("KXBTC15M-26OCT16")["no"] == {58: 120}
