from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.bitstamp import BitstampProcessor


def test_default_config_no_false_gaps_and_microtimestamp_regression():
    """DEFAULT config (event_id_field=None): the clean fixture must produce ZERO gaps, and the
    microtimestamp-regression check must still fire. (Regression guard for the bug where the
    default +1 rule on a microsecond clock false-fired a gap on every message.)"""
    msgs = load_fixture("bitstamp", "live_orders.jsonl")
    p = BitstampProcessor(product="btcusd")  # no override -> default gap rule
    assert p.event_id_field is None
    p.apply_snapshot({"bids": [], "asks": []})

    ts = 1_760_000_000_000_000_000
    for i, m in enumerate(msgs):
        p.process(m, ts + i)

    # No false gaps on a clean, monotonic-microtimestamp stream (the event_id 3->5 skip in the
    # fixture is correctly IGNORED by default — IDs are not a confirmed +1 counter).
    assert p.gaps == []
    assert p.needs_resnapshot is False
    # Book still reconstructs from the order events.
    assert p.book.orders["101"].qty == Decimal("0.25")
    assert "102" not in p.book.orders
    assert p.book.orders["103"].side == "bid"

    # A backward microtimestamp IS the live-stream gap signal and must fire.
    p.process({"event": "order_created", "channel": "live_orders_btcusd",
               "data": {"id": 999, "order_type": 0, "price": "1", "amount": "1",
                        "microtimestamp": "500"}}, ts + 100)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "microtimestamp regression"
    assert p.needs_resnapshot is True


def test_optional_marketeventid_contiguity_when_enabled():
    """Opt-in path: IF a real contiguous MarketEventID WS field is ever confirmed, enabling it
    detects a skip. This is NOT the default and requires closing feeds.md F-1 first."""
    msgs = load_fixture("bitstamp", "live_orders.jsonl")
    p = BitstampProcessor(product="btcusd", event_id_field="event_id")
    p.apply_snapshot({"bids": [], "asks": []})
    ts = 1_760_000_000_000_000_000
    for i, m in enumerate(msgs):
        p.process(m, ts + i)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "MarketEventID skip"
    assert p.gaps[0].expected == 4 and p.gaps[0].got == 5
    assert p.needs_resnapshot is True
