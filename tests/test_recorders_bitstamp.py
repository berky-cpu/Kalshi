from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.bitstamp import BitstampProcessor


def test_live_orders_event_id_chain_reconstruction_and_seeded_gap():
    """VERIFIED gap mechanism: pre_event_id -> event_id chain. A clean chain yields no gaps;
    a seeded pre_event_id mismatch yields exactly one 'event_id chain break' + resync."""
    msgs = load_fixture("bitstamp", "live_orders.jsonl")
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})  # REST order-book seed (empty for the test)

    ts = 1_760_000_000_000_000_000
    for i, m in enumerate(msgs):
        p.process(m, ts + i)

    # Book reconstructed from order_created / order_changed / order_deleted.
    assert p.book.orders["101"].qty == Decimal("0.25")  # changed 0.50 -> 0.25
    assert "102" not in p.book.orders  # deleted
    assert p.book.orders["103"].side == "bid"

    # Seeded gap: event e5 arrives with pre_event_id e4 but last seen event_id was e3.
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "event_id chain break"
    assert p.gaps[0].expected == "e3" and p.gaps[0].got == "e4"
    assert p.needs_resnapshot is True


def test_clean_chain_produces_no_gaps():
    """The first 4 events (e1..e3 chain intact) must produce zero gaps."""
    msgs = load_fixture("bitstamp", "live_orders.jsonl")
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})
    for i, m in enumerate(msgs[:4]):  # subscription ack + e1, e2, e3
        p.process(m, i)
    assert p.gaps == []
    assert p.needs_resnapshot is False


def test_missing_pre_event_id_after_anchor_is_a_gap():
    """Once anchored, an event with no pre_event_id must flag a gap (no silent re-anchor)."""
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})
    base = {"event": "order_created", "channel": "live_orders_btcusd"}
    p.process({**base, "event_id": "a1", "pre_event_id": "a0",
               "data": {"id": 1, "order_type": 0, "price": "1", "amount": "1"}}, 1)
    p.process({**base, "event_id": "a2",  # no pre_event_id
               "data": {"id": 2, "order_type": 0, "price": "1", "amount": "1"}}, 2)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "missing pre_event_id"
    assert p.needs_resnapshot is True


def test_microtimestamp_regression_is_secondary_signal():
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})
    base = {"event": "order_created", "channel": "live_orders_btcusd"}
    p.process({**base, "event_id": "a1", "pre_event_id": "a0",
               "data": {"id": 1, "order_type": 0, "price": "1", "amount": "1",
                        "microtimestamp": "2000"}}, 1)
    # Chain intact (pre a1 == last a1) but microtimestamp goes backward -> secondary gap fires.
    p.process({**base, "event_id": "a2", "pre_event_id": "a1",
               "data": {"id": 2, "order_type": 0, "price": "1", "amount": "1",
                        "microtimestamp": "1999"}}, 2)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "microtimestamp regression"
