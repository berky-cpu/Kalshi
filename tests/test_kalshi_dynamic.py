"""Unit tests for Kalshi dynamic ticker management (ROADMAP 1.3).

Everything here is pure / socket-free: the open-set diff, sid capture from a ``subscribed`` ack,
``update_subscription`` message building, applied-state transitions, and REST discovery (driven by
a FAKE fetcher). The live poller (``run_poller``) needs a socket and is validated on the box.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from kbtc.recorders import kalshi, kalshi_dynamic
from kbtc.recorders.kalshi import KalshiProcessor
from kbtc.recorders.kalshi_dynamic import (
    DynamicSubscriptionManager,
    diff_tickers,
    discover_open_tickers,
)

T0 = "KXBTC15M-26OCT161500-B50000"
T1 = "KXBTC15M-26OCT161515-B50000"
T2 = "KXBTC15M-26OCT161530-B50000"


def iso_epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------------------------------
# Pure diff.
# --------------------------------------------------------------------------------------
def test_diff_tickers_add_remove_and_sorted():
    to_add, to_remove = diff_tickers({T0, T1}, {T1, T2})
    assert to_add == [T2]  # newly open
    assert to_remove == [T0]  # no longer open


def test_diff_tickers_no_change():
    assert diff_tickers({T0, T1}, {T0, T1}) == ([], [])


def test_diff_tickers_sorted_deterministic():
    to_add, _ = diff_tickers(set(), {T2, T0, T1})
    assert to_add == sorted([T0, T1, T2])  # deterministic order for stable frames


# --------------------------------------------------------------------------------------
# sid capture from the `subscribed` ack (VERIFIED body {channel, sid}).
# --------------------------------------------------------------------------------------
def test_sid_capture_nested_msg():
    p = KalshiProcessor()
    p.process({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 7}}, 1)
    p.process({"type": "subscribed", "id": 1, "msg": {"channel": "trade", "sid": 8}}, 2)
    assert p.sid_by_channel == {"orderbook_delta": 7, "trade": 8}


def test_sid_capture_flat_payload():
    p = KalshiProcessor()
    # Flat ack (no msg wrapper); sid at top level alongside channel.
    p.process({"type": "subscribed", "channel": "orderbook_delta", "sid": 42}, 1)
    assert p.sid_by_channel == {"orderbook_delta": 42}


def test_sid_capture_falls_back_to_top_level_sid():
    p = KalshiProcessor()
    # channel only in body, sid only top-level.
    p.process({"type": "subscribed", "sid": 99, "msg": {"channel": "trade"}}, 1)
    assert p.sid_by_channel == {"trade": 99}


def test_subscribed_ack_emits_no_events_and_no_gap():
    p = KalshiProcessor()
    out = p.process({"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}}, 1)
    assert out == []
    assert p.gaps == [] and p.needs_resnapshot is False


# --------------------------------------------------------------------------------------
# F1 — surface subscription-rejection error frames (never silent); no crash, no false gap.
# --------------------------------------------------------------------------------------
def test_error_frame_code_26_logs_warning_flat(caplog):
    p = KalshiProcessor()
    with caplog.at_level(logging.WARNING, logger="kbtc.recorders"):
        out = p.process({"type": "error", "code": 26, "msg": "too many markets"}, 1)
    assert out == []
    assert p.gaps == [] and p.needs_resnapshot is False  # app-level reject, NOT a book gap
    rec = next(r for r in caplog.records if r.__dict__.get("event") == "kalshi_ws_error")
    assert "code=26" in rec.getMessage() and rec.__dict__.get("code") == 26


def test_error_frame_code_27_logs_warning_nested(caplog):
    p = KalshiProcessor()
    with caplog.at_level(logging.WARNING, logger="kbtc.recorders"):
        p.process({"type": "error", "msg": {"code": 27, "msg": "rate limited"}}, 1)
    rec = next(r for r in caplog.records if r.__dict__.get("event") == "kalshi_ws_error")
    assert "code=27" in rec.getMessage() and "rate limited" in rec.getMessage()


def test_error_frame_does_not_crash_on_missing_fields():
    p = KalshiProcessor()
    assert p.process({"type": "error"}, 1) == []  # bare error frame: no code/msg, must not raise


# --------------------------------------------------------------------------------------
# F2 — clear stale per-channel sids on (re)connect so the poller never targets a DEAD sid.
# --------------------------------------------------------------------------------------
def test_reset_subscriptions_clears_sids():
    p = KalshiProcessor()
    p.process({"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}}, 1)
    p.process({"type": "subscribed", "msg": {"channel": "trade", "sid": 2}}, 2)
    assert p.sid_by_channel == {"orderbook_delta": 1, "trade": 2}
    p.reset_subscriptions()
    assert p.sid_by_channel == {}


def test_reconnect_partial_reack_never_targets_old_sid():
    # Connection 1: both channels acked (ob=1, trade=2); manager ready.
    p = KalshiProcessor()
    p.process({"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}}, 1)
    p.process({"type": "subscribed", "msg": {"channel": "trade", "sid": 2}}, 2)
    m = DynamicSubscriptionManager(channels=("orderbook_delta", "trade"))
    m.reset_for_connect([T0])
    m.capture_sids(p.sid_by_channel)
    assert m.ready()

    # RECONNECT: processor sids cleared (F2) + manager re-seeded; only orderbook_delta re-acks (with
    # a NEW sid=5), trade's fresh ack is delayed/lost.
    p.reset_subscriptions()
    m.reset_for_connect([T0, T1])
    p.process({"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 5}}, 3)
    m.capture_sids(p.sid_by_channel)

    # ready() must stay False until BOTH fresh acks arrive — so the poller sends nothing yet.
    assert m.ready() is False
    # And even if a build were attempted, it must use the FRESH sid (5), never the stale 1 or the
    # un-reacked trade sid 2.
    msgs = m.build_update_messages([T1], [])
    assert all(x["params"]["sids"] == [5] for x in msgs)
    assert all(x["params"]["sids"] != [1] and x["params"]["sids"] != [2] for x in msgs)


# --------------------------------------------------------------------------------------
# update_subscription message envelope (VERIFIED).
# --------------------------------------------------------------------------------------
def test_update_subscription_message_envelope():
    msg = kalshi.update_subscription_message(7, [T1, T2], "add_markets", 1000)
    assert msg == {
        "id": 1000,
        "cmd": "update_subscription",
        "params": {"sids": [7], "market_tickers": [T1, T2], "action": "add_markets"},
    }


def test_update_subscription_rejects_bad_action():
    with pytest.raises(ValueError):
        kalshi.update_subscription_message(7, [T1], "subscribe", 1)


def test_update_subscription_sids_is_single_element_array():
    msg = kalshi.update_subscription_message(7, [T1], "delete_markets", 5)
    assert msg["params"]["sids"] == [7]  # exactly one sid, as an array (never singular + array)
    assert "sid" not in msg["params"]


# --------------------------------------------------------------------------------------
# Manager: ready gating, build, apply.
# --------------------------------------------------------------------------------------
def _ready_manager(grace=0.0):
    # Default grace=0 so these message-building tests see removals immediately; the grace behaviour
    # has its own dedicated tests below.
    m = DynamicSubscriptionManager(channels=("orderbook_delta", "trade"), remove_grace_s=grace)
    m.reset_for_connect([T0, T1])
    m.capture_sids({"orderbook_delta": 7, "trade": 8})
    return m


def test_manager_not_ready_until_all_channel_sids_acked():
    m = DynamicSubscriptionManager(channels=("orderbook_delta", "trade"))
    assert m.ready() is False
    m.capture_sids({"orderbook_delta": 7})
    assert m.ready() is False  # trade sid still missing
    m.capture_sids({"trade": 8})
    assert m.ready() is True


def test_build_update_messages_add_and_delete_per_channel_sid():
    m = _ready_manager()
    # Open set rolls: T0 closes, T2 opens; T1 stays. (grace=0 → immediate removal)
    to_add, to_remove = m.plan_update([T1, T2], now_s=0.0)
    assert to_add == [T2] and to_remove == [T0]
    msgs = m.build_update_messages(to_add, to_remove)
    # Channel-major, add before delete: ob add, ob delete, trade add, trade delete.
    assert msgs == [
        {"id": 1000, "cmd": "update_subscription",
         "params": {"sids": [7], "market_tickers": [T2], "action": "add_markets"}},
        {"id": 1001, "cmd": "update_subscription",
         "params": {"sids": [7], "market_tickers": [T0], "action": "delete_markets"}},
        {"id": 1002, "cmd": "update_subscription",
         "params": {"sids": [8], "market_tickers": [T2], "action": "add_markets"}},
        {"id": 1003, "cmd": "update_subscription",
         "params": {"sids": [8], "market_tickers": [T0], "action": "delete_markets"}},
    ]


def test_build_update_messages_add_only():
    m = _ready_manager()
    to_add, to_remove = m.plan_update([T0, T1, T2], now_s=0.0)  # only T2 is new
    assert to_add == [T2] and to_remove == []
    msgs = m.build_update_messages(to_add, to_remove)
    # One add frame per channel, no delete frames.
    assert [x["params"]["action"] for x in msgs] == ["add_markets", "add_markets"]
    assert [x["params"]["sids"] for x in msgs] == [[7], [8]]


def test_build_update_messages_skips_channel_without_sid():
    m = DynamicSubscriptionManager(channels=("orderbook_delta", "trade"))
    m.reset_for_connect([T0])
    m.capture_sids({"orderbook_delta": 7})  # trade sid NOT acked
    msgs = m.build_update_messages([T1], [])
    assert len(msgs) == 1 and msgs[0]["params"]["sids"] == [7]  # only the acked channel


def test_apply_update_moves_applied_state():
    m = _ready_manager()
    to_add, to_remove = m.plan_update([T1, T2], now_s=0.0)
    m.apply_update(to_add, to_remove)
    assert m.subscribed == {T1, T2}
    # Idempotent: a second diff against the same desired set yields nothing.
    assert m.plan_update([T1, T2], now_s=0.0) == ([], [])


def test_reset_for_connect_clears_sids_and_reseeds():
    m = _ready_manager()
    m.apply_update(*m.plan_update([T1, T2], now_s=0.0))
    m.reset_for_connect([T2])  # reconnect → fresh subscription
    assert m.subscribed == {T2}
    assert m.sid_by_channel == {}
    assert m.ready() is False


# --------------------------------------------------------------------------------------
# Pre-subscribe upcoming windows + grace-delayed removal (the roll-gap fix).
# --------------------------------------------------------------------------------------
def test_grace_delays_removal_of_just_closed_market():
    m = _ready_manager(grace=120.0)  # keep final frames for 120 s after close
    m.apply_update(*m.plan_update([T0, T1], now_s=0.0))  # start: both subscribed, nothing pending
    # T0 closes at t=1000: it leaves the desired set but must NOT be removed yet.
    to_add, to_remove = m.plan_update([T1], now_s=1000.0)
    assert to_remove == []  # grace not elapsed
    # Still within grace (t=1050): still kept.
    assert m.plan_update([T1], now_s=1050.0)[1] == []
    # Past grace (t=1000+120): now eligible for removal.
    to_add, to_remove = m.plan_update([T1], now_s=1121.0)
    assert to_remove == [T0]
    m.apply_update(to_add, to_remove)
    assert m.subscribed == {T1}


def test_market_reopening_within_grace_clears_removal_timer():
    m = _ready_manager(grace=120.0)
    m.apply_update(*m.plan_update([T0, T1], now_s=0.0))
    m.plan_update([T1], now_s=100.0)  # T0 absent → timer starts
    # T0 desired again before grace elapses (e.g. discovery blip) → timer cleared, never removed.
    to_add, to_remove = m.plan_update([T0, T1], now_s=150.0)
    assert to_remove == []
    assert m.plan_update([T1], now_s=160.0)[1] == []  # timer restarts from 160, not 100


def test_preadded_upcoming_is_idempotent():
    # Steady state: T0 open + T1 pre-subscribed (upcoming). Next poll, same target → no re-add.
    m = _ready_manager(grace=0.0)
    m.apply_update(*m.plan_update([T0, T1], now_s=0.0))
    assert m.subscribed == {T0, T1}
    to_add, to_remove = m.plan_update([T0, T1], now_s=10.0)  # T1 still upcoming/open
    assert to_add == [] and to_remove == []  # not re-added


def test_ids_strictly_increment_across_builds():
    m = _ready_manager()
    first = m.build_update_messages([T2], [])
    second = m.build_update_messages([], [T0])
    ids = [x["id"] for x in first + second]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)  # unique, monotonic


# --------------------------------------------------------------------------------------
# Market discovery (public REST) with an injected fetcher — no network.
# FIX 1: live markets are classified CLIENT-SIDE (status==active/open), never via the broken
# ``status=open`` server filter. The fake fetch returns the whole close-ts window (as the real API
# does) and the code classifies.
# --------------------------------------------------------------------------------------
def _all_markets_fetch(rows, calls=None):
    def fetch(url: str) -> dict:
        if calls is not None:
            calls.append(url)
        return {"markets": rows, "cursor": ""}
    return fetch


def test_discover_live_includes_active_excludes_finalized_and_initialized():
    # The key FIX-1 case: the tradeable market is status=active (NOT open).
    rows = [
        {"ticker": T0, "status": "active"},        # live
        {"ticker": "OPENY", "status": "open"},     # live (if it ever appears)
        {"ticker": T1, "status": "initialized"},   # upcoming, not live
        {"ticker": "DONE", "status": "finalized"}, # settled, not live
    ]
    calls: list[str] = []
    live = discover_open_tickers(
        _all_markets_fetch(rows, calls), series="KXBTC15M", base="https://x/v2", now_s=1000.0)
    assert live == [T0, "OPENY"]
    # Must NOT rely on the status=open server filter; queries a close-ts window instead.
    assert "status=open" not in calls[0]
    assert "series_ticker=KXBTC15M" in calls[0] and "min_close_ts=" in calls[0]


def test_discover_live_unknown_status_uses_time_window():
    rows = [
        {"ticker": "NOWLIVE", "status": "weird", "open_time": "2026-10-18T03:00:00Z",
         "close_time": "2026-10-18T03:15:00Z"},     # open<=now<close → live
        {"ticker": "FUTURE", "status": "weird", "open_time": "2026-10-18T09:00:00Z",
         "close_time": "2026-10-18T09:15:00Z"},     # now<open → not live
    ]
    now_s = iso_epoch("2026-10-18T03:07:00Z")
    live = discover_open_tickers(_all_markets_fetch(rows), base="https://x/v2", now_s=now_s)
    assert live == ["NOWLIVE"]


def test_discover_live_paginates_via_cursor():
    pages = {
        "page1": {"markets": [{"ticker": T0, "status": "active"}], "cursor": "CUR2"},
        "page2": {"markets": [{"ticker": T1, "status": "active"}], "cursor": ""},
    }
    calls: list[str] = []

    def fake_fetch(url: str) -> dict:
        calls.append(url)
        return pages["page2"] if "cursor=CUR2" in url else pages["page1"]

    assert discover_open_tickers(fake_fetch, base="https://x/v2", now_s=1000.0) == [T0, T1]
    assert any("cursor=CUR2" in u for u in calls)


def test_discover_live_empty():
    assert discover_open_tickers(_all_markets_fetch([]), base="https://x/v2", now_s=1000.0) == []


def test_discover_upcoming_picks_next_initialized_capped():
    rows = [
        {"ticker": T1, "status": "initialized", "close_time": "2026-10-18T03:15:00Z"},
        {"ticker": T2, "status": "initialized", "close_time": "2026-10-18T03:30:00Z"},
        {"ticker": "ACTIVE", "status": "active", "close_time": "2026-10-18T03:00:00Z"},  # not upcoming
    ]
    fetch = _all_markets_fetch(rows)
    # cap 1 → only the earliest-closing initialized window.
    assert kalshi_dynamic.discover_upcoming_tickers(
        fetch, series="KXBTC15M", base="https://x/v2", now_s=0.0, max_upcoming=1) == [T1]
    # cap 2 → the next two, earliest first.
    assert kalshi_dynamic.discover_upcoming_tickers(
        fetch, base="https://x/v2", now_s=0.0, max_upcoming=2) == [T1, T2]


def test_discover_upcoming_runaway_guard_hard_cap():
    rows = [{"ticker": f"K-{i}", "status": "initialized",
             "close_time": f"2026-10-18T0{i}:00:00Z"} for i in range(1, 6)]
    fetch = _all_markets_fetch(rows)
    # Even asking for 99, never more than the hard cap (2).
    out = kalshi_dynamic.discover_upcoming_tickers(
        fetch, base="https://x/v2", now_s=0.0, max_upcoming=99)
    assert len(out) == 2


def test_discover_target_is_live_plus_upcoming():
    # Mixed statuses in one window: the ACTIVE market + the next INITIALIZED window, finalized out.
    rows = [
        {"ticker": T0, "status": "active"},
        {"ticker": T1, "status": "initialized", "close_time": "2026-10-18T03:15:00Z"},
        {"ticker": "DONE", "status": "finalized", "close_time": "2026-10-18T02:45:00Z"},
    ]
    target = kalshi_dynamic.discover_target_tickers(
        _all_markets_fetch(rows), series="KXBTC15M", base="https://x/v2",
        now_s=1000.0, max_upcoming=1)
    assert target == [T0, T1]  # live first, then the pre-subscribed upcoming window


def test_discover_target_dedupes_live_and_upcoming():
    # A market classified both live and (defensively) upcoming is included once.
    rows = [
        {"ticker": T0, "status": "active"},
        {"ticker": T0, "status": "initialized", "close_time": "2026-10-18T03:15:00Z"},
        {"ticker": T1, "status": "initialized", "close_time": "2026-10-18T03:30:00Z"},
    ]
    target = kalshi_dynamic.discover_target_tickers(
        _all_markets_fetch(rows), base="https://x/v2", now_s=1000.0, max_upcoming=2)
    assert target == [T0, T1]


# --------------------------------------------------------------------------------------
# FIX 2 — bounded memory across many rolls (the 11h / 2.3 GB leak).
# --------------------------------------------------------------------------------------
def _snapshot(ticker, sid, seq=1):
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "market_ticker": ticker,
            "yes_dollars_fp": [["0.40", "100.00"]], "no_dollars_fp": [["0.58", "120.00"]]}


def test_books_dict_bounded_across_many_rolls():
    # Simulate N market rolls: each new market gets a snapshot (book created), then is evicted when
    # it rolls off (as the poller does after grace). The tracked-book count must stay O(1), not O(N).
    p = KalshiProcessor()
    live = None
    for i in range(500):
        tk = f"KXBTC15M-ROLL-{i}"
        p.process(_snapshot(tk, sid=1), i)       # new market book appears
        if live is not None:
            p.evict_market(live)                 # previous market rolled off → evicted
        live = tk
        assert len(p.books) == 1                 # only the current market is tracked
    assert len(p.books) == 1


def test_evict_market_is_idempotent_and_targeted():
    p = KalshiProcessor()
    p.process(_snapshot("A", sid=1), 1)
    p.process(_snapshot("B", sid=1, seq=2), 2)
    assert set(p.books) == {"A", "B"}
    p.evict_market("A")
    p.evict_market("A")  # idempotent
    p.evict_market("NOPE")  # unknown → no-op
    assert set(p.books) == {"B"}


def test_settled_lifecycle_frame_evicts_book():
    p = KalshiProcessor()
    p.process(_snapshot("A", sid=1), 1)
    assert "A" in p.books
    p.process({"type": "market_lifecycle_v2", "sid": 9, "market_ticker": "A",
               "status": "finalized"}, 2)
    assert "A" not in p.books  # settled → book freed immediately
    # A non-terminal lifecycle status does NOT evict.
    p.process(_snapshot("B", sid=1, seq=2), 3)
    p.process({"type": "market_lifecycle_v2", "sid": 9, "market_ticker": "B",
               "status": "active"}, 4)
    assert "B" in p.books


def test_trades_and_lifecycle_tails_are_bounded():
    # The live introspection tails must not grow without bound (the authoritative copy is the raw
    # written by writer.py). Push far more than the cap and assert the deque is capped.
    p = KalshiProcessor()
    n = p.TRADES_MAXLEN + 5000
    for i in range(n):
        p.process({"type": "trade", "sid": 9, "seq": i + 1,
                   "msg": {"trade_id": str(i), "count_fp": "1.00"}}, i)
    assert len(p.trades) == p.TRADES_MAXLEN       # bounded, not n
    assert p.trades[-1]["trade_id"] == str(n - 1)  # keeps the most recent tail

    for i in range(p.LIFECYCLE_MAXLEN + 2000):
        p.process({"type": "market_lifecycle_v2", "sid": 9,
                   "market_ticker": f"K{i}", "status": "active"}, i)
    assert len(p.lifecycle) == p.LIFECYCLE_MAXLEN


# --------------------------------------------------------------------------------------
# End-to-end pure flow: ack → discover → diff → build → apply (no socket).
# --------------------------------------------------------------------------------------
def test_full_roll_flow_add_new_drop_closed():
    # A fresh connection: subscribe to {T0, T1}; acks arrive on a processor; a roll opens T2 and
    # closes T0. The manager produces the exact add/delete frames and advances its state.
    p = KalshiProcessor()
    p.process({"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 3}}, 1)
    p.process({"type": "subscribed", "msg": {"channel": "trade", "sid": 4}}, 2)

    m = DynamicSubscriptionManager(channels=("orderbook_delta", "trade"), remove_grace_s=0.0)
    m.reset_for_connect([T0, T1])
    m.capture_sids(p.sid_by_channel)
    assert m.ready()

    to_add, to_remove = m.plan_update([T1, T2], now_s=0.0)
    msgs = m.build_update_messages(to_add, to_remove)
    actions = {(x["params"]["sids"][0], x["params"]["action"]): x["params"]["market_tickers"]
               for x in msgs}
    assert actions[(3, "add_markets")] == [T2]
    assert actions[(3, "delete_markets")] == [T0]
    assert actions[(4, "add_markets")] == [T2]
    assert actions[(4, "delete_markets")] == [T0]
    m.apply_update(to_add, to_remove)
    assert m.subscribed == {T1, T2}
