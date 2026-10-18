"""Unit tests for the data-staleness watchdog decision logic (base.DataStalenessMonitor).

The self-heal for the 2026-10-18 "connected but no data for 11h" incident. All pure / socket-free;
the ``run_recorder`` wiring that closes the ws is the live loop (pragma-no-cover). "DATA" is defined
by the same per-venue classifier the health report uses (``health.data_classifier``), so these
tests drive the monitor with REAL classified frames (heartbeats must NOT reset the clock).
"""
from __future__ import annotations

from kbtc.recorders.base import DataStalenessMonitor
from kbtc.recorders.health import data_classifier

S = 1_000_000_000  # ns per second

# Kraken frames via the real health classifier: data = level3 snapshot/update; others are control.
KRAKEN = data_classifier("kraken")
DATA = {"channel": "level3", "type": "update", "data": [{"bids": []}]}
HEARTBEAT = {"channel": "heartbeat"}
STATUS = {"channel": "status", "type": "update", "data": [{"system": "online"}]}
PONG = {"method": "pong", "req_id": 1}


def _mon(timeout=10.0):
    return DataStalenessMonitor(timeout_s=timeout, classifier=KRAKEN)


def test_trips_after_timeout_with_only_heartbeats():
    m = _mon(10.0)
    m.on_connect(now_ns := 1000 * S)
    # Only control/heartbeat frames arrive — the data clock never advances.
    for i, frame in enumerate((HEARTBEAT, STATUS, PONG, HEARTBEAT)):
        assert m.observe(frame, now_ns + i * S) is False  # not counted as data
    assert m.last_data_ns == 0
    assert m.is_stale(now_ns + 9 * S) is False   # within grace/timeout from connect
    assert m.is_stale(now_ns + 10 * S) is True    # 10s of no data while "connected" → stale


def test_data_frame_resets_the_clock():
    m = _mon(10.0)
    t0 = 1000 * S
    m.on_connect(t0)
    m.observe(DATA, t0 + 5 * S)          # real data at +5s
    assert m.last_data_ns == t0 + 5 * S
    assert m.is_stale(t0 + 14 * S) is False  # only 9s since last data
    assert m.is_stale(t0 + 15 * S) is True   # 10s since last data


def test_continuous_data_never_trips():
    m = _mon(10.0)
    t0 = 1000 * S
    m.on_connect(t0)
    # A data frame every 2s for a long run: never stale.
    for k in range(1, 100):
        t = t0 + 2 * k * S
        m.observe(DATA, t)
        assert m.is_stale(t) is False


def test_control_frames_do_not_advance_timer_only_data_does():
    m = _mon(10.0)
    t0 = 1000 * S
    m.on_connect(t0)
    m.observe(DATA, t0 + 1 * S)           # data
    assert m.observe(HEARTBEAT, t0 + 8 * S) is False
    assert m.observe(PONG, t0 + 9 * S) is False
    assert m.last_data_ns == t0 + 1 * S   # unchanged by the two control frames
    assert m.is_stale(t0 + 11 * S) is True  # 10s since the single data frame at +1s


def test_baseline_resets_on_reconnect():
    m = _mon(10.0)
    t0 = 1000 * S
    m.on_connect(t0)
    m.observe(DATA, t0 + 3 * S)
    # Reconnect at a much later time: the fresh connection gets the FULL timeout before it can trip,
    # even though the previous connection's last data was long ago.
    t1 = t0 + 10_000 * S
    m.on_connect(t1)
    assert m.last_data_ns == 0
    assert m.is_stale(t1 + 1 * S) is False     # fresh connect — not instantly stale
    assert m.is_stale(t1 + 10 * S) is True      # full timeout later with no data


def test_timeout_none_disables():
    m = DataStalenessMonitor(timeout_s=None, classifier=KRAKEN)
    m.on_connect(1000 * S)
    # Never stale, no matter how long with no data.
    assert m.is_stale(1000 * S + 10_000 * S) is False


def test_not_stale_before_connect():
    m = _mon(10.0)
    # No on_connect(), no data → no baseline → cannot be stale.
    assert m.is_stale(10_000 * S) is False


def test_none_classifier_counts_every_frame():
    # Health's fallback for an unclassified venue: with no classifier every frame counts as data,
    # so even heartbeats reset the clock (documented behaviour).
    m = DataStalenessMonitor(timeout_s=10.0, classifier=None)
    t0 = 1000 * S
    m.on_connect(t0)
    assert m.observe(HEARTBEAT, t0 + 5 * S) is True
    assert m.last_data_ns == t0 + 5 * S
