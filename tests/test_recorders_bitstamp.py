from decimal import Decimal

from conftest import load_fixture

import logging

from kbtc.recorders.base import PeriodicReseedTimer
from kbtc.recorders.bitstamp import (
    MAX_RECENT_DELETES,
    PERIODIC_RESEED_S,
    RESEED_DELETE_WINDOW_US,
    BitstampProcessor,
)

S = 1_000_000_000  # ns per second


def _evt(event, eid, pre, oid, otype, price, amount, micro):
    return {
        "event": event, "channel": "live_orders_btcusd", "event_id": eid, "pre_event_id": pre,
        "data": {"id": oid, "order_type": otype, "price": price, "amount": amount,
                 "microtimestamp": str(micro)},
    }


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


# ---------------------------------------------------------------------------------------
# Reseed-resurrection guard (study-2.2 crossed-book root cause). Bitstamp's REST /order_book/
# lags the WS stream, so a reseed snapshot (micro S) can list an order the stream already
# DELETED at micro > S. Blindly re-adding it resurrects a phantom that self-crosses the book.
# ---------------------------------------------------------------------------------------
def test_reseed_does_not_resurrect_stream_deleted_order():
    """The exact traced incident: create -> delete (micro > S) on the live stream, then a stale
    REST reseed at micro S < delete-micro that still lists the order. It must NOT be resurrected,
    and the reseeded top-of-book must stay UNCROSSED."""
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})  # fresh connect seed (no micro -> no floor)

    # Live stream: order 999 is created as a bid at 105 (would cross a 101 ask) then deleted.
    p.process(_evt("order_created", "c1", "c0", 999, 0, "105.00", "0.5", micro=2000), 1)
    p.process(_evt("order_deleted", "c2", "c1", 999, 0, "105.00", "0.5", micro=3000), 2)
    assert "999" not in p.book.orders  # deleted live

    # Reseed: the lagging REST snapshot (micro S=2500 < delete-micro 3000) STILL lists 999 resting,
    # alongside the true touch (bid 100 / ask 101).
    p.apply_snapshot({
        "microtimestamp": "2500",
        "bids": [["105.00", "0.5", "999"], ["100.00", "1.0", "10"]],
        "asks": [["101.00", "1.0", "20"]],
    })

    assert "999" not in p.book.orders  # stale resurrection suppressed
    assert p.resurrections_suppressed == 1
    assert p.book.best_bid() == Decimal("100.00")  # the real touch, not the phantom 105
    assert p.book.best_ask() == Decimal("101.00")
    assert p.book.best_bid() < p.book.best_ask()  # UNCROSSED
    assert p.synced is True  # synced only after a now-consistent reseed


def test_reseed_readds_order_with_no_newer_delete():
    """Normal reseed: a snapshot order we never saw deleted — or deleted at micro <= S — re-adds
    normally (the guard only rejects deletes NEWER than the snapshot)."""
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})

    # Order 30 deleted at micro 2000, which is <= the coming snapshot micro S=2500 -> NOT stale.
    p.process(_evt("order_created", "c1", "c0", 30, 0, "99.00", "1.0", micro=1500), 1)
    p.process(_evt("order_deleted", "c2", "c1", 30, 0, "99.00", "1.0", micro=2000), 2)

    p.apply_snapshot({
        "microtimestamp": "2500",
        "bids": [["100.00", "1.0", "30"], ["99.50", "1.0", "11"]],  # 30 re-listed, deleted<=S
        "asks": [["101.00", "1.0", "21"]],
    })
    assert "30" in p.book.orders  # delete micro (2000) <= S (2500): trust the snapshot, re-add
    assert "11" in p.book.orders  # never deleted: re-adds
    assert p.resurrections_suppressed == 0
    assert p.book.best_bid() < p.book.best_ask()


def test_post_reseed_live_event_still_applies_and_floor_holds():
    """After a reseed, a legitimate live event (micro > floor) applies; a stale one (micro <= floor)
    is still dropped by the forward-only floor. The resurrection guard doesn't disturb either."""
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({
        "microtimestamp": "2500",
        "bids": [["100.00", "1.0", "10"]],
        "asks": [["101.00", "1.0", "20"]],
    })
    # Stale event at micro <= floor 2500 -> dropped (already in snapshot).
    assert p.process(_evt("order_created", "x1", "x0", 40, 0, "100.50", "1.0", micro=2400), 10) == []
    assert "40" not in p.book.orders
    # Fresh event past the floor applies.
    out = p.process(_evt("order_created", "x2", "x1", 41, 0, "100.50", "1.0", micro=2600), 11)
    assert out and "41" in p.book.orders


def test_recent_deletes_structure_is_bounded():
    """The delete-tracking map must not grow unboundedly: age-pruning keeps only a recent window."""
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})
    # Feed many deletes with steadily increasing micro spanning ~4x the age window (derived from the
    # constant so this holds whatever the window is tuned to), so age-pruning must kick in.
    base = 1_000_000_000_000
    n = 20_000
    step = max(1, (RESEED_DELETE_WINDOW_US * 4) // n)  # total span ≈ 4 * window
    for i in range(n):
        micro = base + i * step
        p.process(_evt("order_created", f"a{i}", f"a{i-1}" if i else "a-1",
                       i, 0, "1.00", "1.0", micro=micro), i)
        p.process(_evt("order_deleted", f"d{i}", f"a{i}", i, 0, "1.00", "1.0", micro=micro + 1), i)
    # Only deletes within the window of the newest delete survive -> bounded well under n.
    newest = base + (n - 1) * step + 1
    assert all(v >= newest - RESEED_DELETE_WINDOW_US for v in p._recent_deletes.values())
    assert len(p._recent_deletes) <= RESEED_DELETE_WINDOW_US // step + 5  # ~window worth, not n
    assert len(p._recent_deletes) < n

    # A delete aged out of the window can no longer suppress a reseed order. Order 0's delete was
    # pruned long ago, so a stale reseed floor < its delete micro must STILL re-add it (no recorded
    # delete -> the guard cannot, and must not, suppress it).
    assert "0" not in p._recent_deletes
    p.apply_snapshot({
        "microtimestamp": str(base - 1),  # floor older than order 0's long-pruned delete
        "bids": [["1.00", "1.0", "0"]],
        "asks": [["2.00", "1.0", "99991"]],
    })
    assert "0" in p.book.orders
    assert p.resurrections_suppressed == 0


def test_reseed_suppresses_stale_tie_delete_at_floor():
    """REV-026 F2: a delete at micro EXACTLY == S that a stale REST snapshot still lists must be
    SUPPRESSED (condition is delete_micro >= S, matching the floor's `micro <= S` convention).
    Re-adding this stale-tie order would cross the book."""
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})

    # Order 777 (a bid at 102 that would cross a 101 ask) created, then deleted at micro == S (2500).
    p.process(_evt("order_created", "c1", "c0", 777, 0, "102.00", "0.5", micro=2000), 1)
    p.process(_evt("order_deleted", "c2", "c1", 777, 0, "102.00", "0.5", micro=2500), 2)
    assert "777" not in p.book.orders

    # Stale REST reseed at micro S == 2500 (the tie) still lists 777 resting at 102.
    p.apply_snapshot({
        "microtimestamp": "2500",
        "bids": [["102.00", "0.5", "777"], ["100.00", "1.0", "10"]],
        "asks": [["101.00", "1.0", "20"]],
    })
    assert "777" not in p.book.orders  # stale-tie suppressed (>= S)
    assert p.resurrections_suppressed == 1
    assert p.book.best_bid() == Decimal("100.00")
    assert p.book.best_ask() == Decimal("101.00")
    assert p.book.best_bid() < p.book.best_ask()  # UNCROSSED


def test_guard_disable_paths_are_counted_and_warned(caplog):
    """REV-026 F3: the two silent guard-disable paths must be counted + WARNINGed so residual
    crossing is attributable. (a) order_deleted with no microtimestamp; (b) reseed snapshot with
    no usable floor S (and non-empty rows)."""
    p = BitstampProcessor(product="btcusd")
    # Seed WITH a floor so snapshots_without_floor stays 0 for the delete-path assertion.
    p.apply_snapshot({"microtimestamp": "1000", "bids": [], "asks": []})
    assert p.snapshots_without_floor == 0

    # (a) order_deleted with NO microtimestamp -> delete unrecorded, counter + warning.
    with caplog.at_level(logging.WARNING, logger="kbtc.recorders"):
        p.process({"event": "order_deleted", "channel": "live_orders_btcusd",
                   "event_id": "d1", "pre_event_id": "d0",
                   "data": {"id": 55, "order_type": 0, "price": "1", "amount": "1"}}, 1)
    assert p.deletes_missing_micro == 1
    assert "55" not in p._recent_deletes  # not recorded -> guard disabled for this id
    assert any("no microtimestamp" in r.getMessage() for r in caplog.records)

    # (b) reseed snapshot with NO floor (no microtimestamp/timestamp) but real rows -> counter + warning.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kbtc.recorders"):
        p.apply_snapshot({"bids": [["100.00", "1.0", "60"]], "asks": [["101.00", "1.0", "61"]]})
    assert p.snapshots_without_floor == 1
    assert "60" in p.book.orders and "61" in p.book.orders  # guard skipped -> all rows re-added
    assert any("no microtimestamp/timestamp floor" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------------------
# Periodic forced reseed (2026-10-23 lost-delete defect). ``live_orders`` has NO in-band snapshot,
# so the book self-heals ONLY via a REST reseed. A lost ``order_deleted`` is a wire loss with NO
# chain break, so gap detection never fires; the traced incident left a stale order resting for 2+
# days (no reseed fired) until BTC fell below its price and the book showed a persistent gross cross.
# A timer-forced reseed clears it: apply_snapshot's clear()+rebuild drops any order absent from the
# fresh snapshot. (This is NOT the REV-026 resurrection class — there is no delete to key on and no
# stale-snapshot lag involved; the dead order is simply gone from the next snapshot.)
# ---------------------------------------------------------------------------------------
def test_periodic_reseed_clears_lost_delete_stale_order():
    """Reproduce the exact defect: order created, its ``order_deleted`` NEVER transmitted (wire
    loss), no gap detected → the order rests forever and crosses once BTC falls below its price. A
    periodic forced reseed (timer due) runs apply_snapshot with a fresh snapshot that OMITS the dead
    order → it is cleared and the book is uncrossed."""
    p = BitstampProcessor(product="btcusd")
    # Initial seed: the stale order is resting (bid at 81945.48, the traced oid), book consistent.
    stale_oid = "2063684624891906"
    p.apply_snapshot({
        "microtimestamp": "1000000",
        "bids": [[ "81945.48", "0.5", stale_oid], ["81900.00", "1.0", "10"]],
        "asks": [["81950.00", "1.0", "20"]],
    })
    assert stale_oid in p.book.orders

    # Live stream over the next period: BTC falls. The ask side drops BELOW the stale bid 81945.48,
    # which would cross — but there is NO delete for the stale order (the delete was lost on the
    # wire), and no chain break, so no gap/resync is ever triggered.
    p.process(_evt("order_created", "e1", None, 21, 1, "81940.00", "1.0", micro=1_000_100), 2)
    assert p.needs_resnapshot is False  # nothing detected this as a problem — that's the defect
    assert p.book.best_bid() == Decimal("81945.48")  # stale bid now ABOVE the new ask → crossed
    assert p.book.best_ask() == Decimal("81940.00")
    assert p.book.best_bid() > p.book.best_ask()  # POISONED: gross cross, undetectable in-band

    # ---- Periodic forced reseed fires (timer due) ----
    timer = PeriodicReseedTimer(interval_s=PERIODIC_RESEED_S)
    t0 = 1000 * S
    timer.on_reseed(t0)  # baseline at connect
    assert timer.due(t0 + PERIODIC_RESEED_S * S) is True  # a full interval later → the loop reseeds
    # The loop's action when due: it would set needs_resnapshot and run apply_snapshot inline. The
    # fresh REST snapshot simply OMITS the long-dead order (only the real resting book is returned).
    p.apply_snapshot({
        "microtimestamp": "2000000",
        "bids": [["81900.00", "1.0", "10"]],
        "asks": [["81940.00", "1.0", "21"]],
    })

    assert stale_oid not in p.book.orders  # the lost-delete stale order is gone
    assert p.book.best_bid() == Decimal("81900.00")
    assert p.book.best_ask() == Decimal("81940.00")
    assert p.book.best_bid() < p.book.best_ask()  # UNCROSSED — self-healed
    assert p.resurrections_suppressed == 0  # no resurrection involved — pure clear()+rebuild


def test_periodic_reseed_path_still_honours_rev026_suppression():
    """A periodic reseed reuses the SAME apply_snapshot path as a gap reseed, so the REV-026
    resurrection guard still protects it: a snapshot order with a recorded delete at micro >= S (the
    lagging-REST-snapshot case) is STILL suppressed on a periodic reseed, so a slightly-lagging
    periodic snapshot can never reintroduce resurrection."""
    p = BitstampProcessor(product="btcusd")
    p.apply_snapshot({"bids": [], "asks": []})

    # A normal create+delete on the live stream (delete micro 3000).
    p.process(_evt("order_created", "c1", "c0", 888, 0, "102.00", "0.5", micro=2000), 1)
    p.process(_evt("order_deleted", "c2", "c1", 888, 0, "102.00", "0.5", micro=3000), 2)
    assert "888" not in p.book.orders

    # Periodic reseed is due; its REST snapshot lags (micro S=2500 < delete 3000) and still lists 888.
    timer = PeriodicReseedTimer(interval_s=PERIODIC_RESEED_S)
    t0 = 1000 * S
    timer.on_reseed(t0)
    assert timer.due(t0 + PERIODIC_RESEED_S * S) is True
    p.apply_snapshot({
        "microtimestamp": "2500",
        "bids": [["102.00", "0.5", "888"], ["100.00", "1.0", "10"]],
        "asks": [["101.00", "1.0", "20"]],
    })

    assert "888" not in p.book.orders  # REV-026 guard fired on the periodic path too
    assert p.resurrections_suppressed == 1
    assert p.book.best_bid() < p.book.best_ask()  # UNCROSSED


def test_hard_cap_bounds_map_under_microtimestamp_regression():
    """REV-026 F6: a microtimestamp-regression burst (here: non-advancing micro, so age-pruning
    can never evict) exceeding MAX_RECENT_DELETES must leave the map capped at MAX_RECENT_DELETES
    and evict the OLDEST entries (popitem(last=False)), not the newest."""
    p = BitstampProcessor(product="btcusd")
    m = 5_000_000  # constant micro: cutoff = m - WINDOW < m, so age-prune never fires
    overflow = 100
    n = MAX_RECENT_DELETES + overflow
    for i in range(n):
        p._note_delete(f"d{i}", m)  # drive the map directly to exercise the cap deterministically
    assert len(p._recent_deletes) == MAX_RECENT_DELETES  # capped, did not grow to n
    # Oldest `overflow` ids evicted; the newest survive (FIFO eviction from the front).
    assert "d0" not in p._recent_deletes
    assert f"d{overflow - 1}" not in p._recent_deletes
    assert f"d{overflow}" in p._recent_deletes          # first surviving id
    assert f"d{n - 1}" in p._recent_deletes             # newest id retained
