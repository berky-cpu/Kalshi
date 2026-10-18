from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.kalshi import KalshiProcessor

# The orderbook WS schema is VERIFIED (docs.kalshi.com/websockets/orderbook-updates, 2026-10-17):
# envelope type/sid/seq; snapshot yes_dollars_fp/no_dollars_fp (optional [price_dollars,count_fp]
# string pairs); delta price_dollars + SIGNED delta_fp + side yes/no. STILL UNVERIFIED pending a
# live capture (Kalshi WS needs auth; box has no creds): the ``msg``-wrapper (flat vs nested) and
# the seq scope/reset semantics — the adapter parses robustly to both wrapper shapes.


def test_orderbook_delta_trade_lifecycle_and_seeded_seq_gap():
    msgs = load_fixture("kalshi", "orderbook.jsonl")
    p = KalshiProcessor(product="KXBTC15M-26OCT16")
    ts = 1_760_000_000_000_000_000
    for i, m in enumerate(msgs):
        p.process(m, ts + i)

    book = p.book_for("KXBTC15M-26OCT16")
    # snapshot yes{0.40:100, 0.41:250} no{0.58:120}; +50 on yes 0.40 -> 150; -250 removes yes 0.41.
    assert book["yes"] == {Decimal("0.40"): Decimal("150.00")}
    assert Decimal("0.41") not in book["yes"]
    # no side stays 120: the seq-5 'no' delta is dropped by the seq gap below (never silently applied).
    assert book["no"] == {Decimal("0.58"): Decimal("120.00")}
    # The trade (sid 9, VERIFIED schema) and lifecycle were recorded raw.
    assert p.trades and p.trades[0]["count_fp"] == "5.00"
    assert p.trades[0]["yes_price_dollars"] == "0.4100" and p.trades[0]["taker_side"] == "yes"
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
    assert p.book_for("KXBTC15M-26OCT16")["no"] == {Decimal("0.58"): Decimal("120.00")}


def test_signed_delta_to_zero_removes_level():
    """A SIGNED delta_fp that brings a level to <= 0 drops it (never a negative resting size)."""
    p = KalshiProcessor(product="T")
    p.process({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "market_ticker": "T",
               "yes_dollars_fp": [["0.30", "75.00"]]}, 1)
    assert p.book_for("T")["yes"] == {Decimal("0.30"): Decimal("75.00")}
    # +25 then -100 (signed) -> 0 -> removed.
    p.process({"type": "orderbook_delta", "sid": 1, "seq": 2, "market_ticker": "T",
               "price_dollars": "0.30", "delta_fp": "25.00", "side": "yes"}, 2)
    assert p.book_for("T")["yes"] == {Decimal("0.30"): Decimal("100.00")}
    p.process({"type": "orderbook_delta", "sid": 1, "seq": 3, "market_ticker": "T",
               "price_dollars": "0.30", "delta_fp": "-100.00", "side": "yes"}, 3)
    assert Decimal("0.30") not in p.book_for("T")["yes"]


def test_robust_to_msg_wrapper_and_flat_payload():
    """UNVERIFIED msg-wrapper (1.1b): the adapter must parse BOTH the FLAT payload (official event-
    markets example) and the margin-ws ``msg``-nested payload to the SAME book."""
    flat = {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "market_ticker": "T",
            "yes_dollars_fp": [["0.08", "300.00"]], "no_dollars_fp": [["0.92", "10.00"]]}
    nested = {"type": "orderbook_snapshot", "sid": 2, "seq": 1,
              "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.08", "300.00"]],
                      "no_dollars_fp": [["0.92", "10.00"]]}}
    pf = KalshiProcessor(product="T"); pf.process(flat, 1)
    pn = KalshiProcessor(product="T"); pn.process(nested, 1)
    expected = {"yes": {Decimal("0.08"): Decimal("300.00")},
                "no": {Decimal("0.92"): Decimal("10.00")}}
    assert pf.book_for("T") == expected
    assert pn.book_for("T") == expected


def test_snapshot_optional_sides_absent():
    """yes_dollars_fp / no_dollars_fp are OPTIONAL (absent when a side has no offers)."""
    p = KalshiProcessor(product="T")
    p.process({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "market_ticker": "T",
               "no_dollars_fp": [["0.95", "5.00"]]}, 1)  # no yes side at all
    book = p.book_for("T")
    assert "yes" not in book
    assert book["no"] == {Decimal("0.95"): Decimal("5.00")}
