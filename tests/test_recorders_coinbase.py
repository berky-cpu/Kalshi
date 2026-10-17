from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.coinbase import CoinbaseProcessor


def test_full_channel_reconstruction_and_seeded_gap():
    msgs = load_fixture("coinbase", "full.jsonl")
    p = CoinbaseProcessor(product="BTC-USD", channel="full")
    # Fresh REST level=3 snapshot (sequence 100); live messages replay on top of it.
    p.apply_snapshot({"sequence": 100, "bids": [], "asks": []})

    ts = 1_760_000_000_000_000_000
    for i, m in enumerate(msgs):
        p.process(m, ts + i)
        if p.gaps:
            # Stop driving at the first gap, as the IO layer would before resnapshot.
            break

    # Clean portion (seq 101..104) reconstructed correctly before the seeded gap at 106.
    assert p.book.orders["o1"].qty == Decimal("0.25")  # change 0.50 -> 0.25
    assert p.book.orders["o2"].qty == Decimal("0.60")  # 1.00 filled 0.40 -> 0.60
    assert p.book.best_bid() == Decimal("27000.00")
    assert p.book.best_ask() == Decimal("27010.00")

    # Seeded gap (seq 105 skipped) is detected, logged, and triggers resnapshot — not silent.
    assert len(p.gaps) == 1
    g = p.gaps[0]
    assert g.reason == "sequence jump" and g.expected == 105 and g.got == 106
    assert p.needs_resnapshot is True


def test_level3_array_decoded_via_schema():
    p = CoinbaseProcessor(product="BTC-USD", channel="level3")
    schema = {"type": "level3", "schema": {
        "open": ["type", "product_id", "sequence", "order_id", "side", "price", "remaining_size", "time"],
    }}
    p.process(schema, 1)  # one-off schema object
    p.apply_snapshot({"sequence": 10, "bids": [], "asks": []})
    row = ["open", "BTC-USD", 11, "x1", "buy", "27000.00", "0.5", "2026-01-01T00:00:00Z"]
    p.process(row, 2)
    assert p.book.best_bid() == Decimal("27000.00")
    assert not p.gaps
