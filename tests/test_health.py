"""Tests for the recorder health report (ROADMAP 1.4).

Fixture tree (committed under tests/fixtures/health/data/raw/, written by recorders/writer.py):

* coinbase/full          — a CLEAN run: 10 dense (1 msg/s) messages, contiguous Coinbase
                           ``sequence`` 101..110, one hourly file.  => ~100% uptime, 0 seq gaps.
* bitstamp/live_orders_btcusd — a BROKEN run: a pre_event_id chain break (one sequence gap) AND a
                           missing hour (coverage hole): events in hour 08 and hour 10, none in 09.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbtc.recorders.health import (
    CoverageGap,
    compute_feed_health,
    compute_health,
    data_classifier,
    discover_feeds,
    make_processor,
    render_table,
)
from kbtc.recorders.writer import RotatingZstdWriter

NS = 1_000_000_000
HOUR = 3600 * NS
T0 = 1_760_000_000 * NS
DATA_ROOT = Path(__file__).parent / "fixtures" / "health" / "data" / "raw"


def _disk_bytes(*parts: str) -> int:
    """Independent on-disk byte total for a feed, to check the module's disk aggregation exactly."""
    base = DATA_ROOT.joinpath(*parts)
    return sum(f.stat().st_size for f in base.rglob("*.jsonl.zst"))


def _bitstamp_order(eid: str, pre: str, oid: int, micro: int) -> str:
    return json.dumps({
        "event": "order_created", "channel": "live_orders_btcusd",
        "event_id": eid, "pre_event_id": pre,
        "data": {"id": oid, "order_type": 0, "price": "1", "amount": "1",
                 "microtimestamp": str(micro)},
    })


def _bitstamp_heartbeat() -> str:
    return json.dumps({"event": "bts:heartbeat", "channel": "live_orders_btcusd"})


def _coinbase_open(seq: int, oid: str) -> str:
    return json.dumps({"type": "open", "product_id": "BTC-USD", "sequence": seq,
                       "order_id": oid, "side": "buy", "price": "27000.00",
                       "remaining_size": "0.5"})


# --------------------------------------------------------------------------------------
# Discovery + processor mapping.
# --------------------------------------------------------------------------------------
def test_discover_feeds():
    assert discover_feeds(DATA_ROOT) == [
        ("bitstamp", "live_orders_btcusd"),
        ("coinbase", "full"),
    ]


def test_processor_mapping_covers_known_venues_and_marks_others_na():
    assert make_processor("coinbase", "full")[0].__class__.__name__ == "CoinbaseProcessor"
    assert make_processor("kraken", "level3")[0].__class__.__name__ == "KrakenProcessor"
    assert make_processor("binance", "depth")[0].__class__.__name__ == "BinanceDepthProcessor"
    assert make_processor("binance", "aggTrade")[0].__class__.__name__ == "BinanceAggTradeProcessor"
    assert make_processor("bitstamp", "live_orders_btcusd")[0].__class__.__name__ == "BitstampProcessor"
    assert make_processor("bybit", "orderbook.50.BTCUSDT")[0].__class__.__name__ == "BybitOrderbookProcessor"
    assert make_processor("bybit", "publicTrade.BTCUSDT")[0].__class__.__name__ == "BybitPublicTradeProcessor"
    assert make_processor("kalshi", "orderbook_delta")[0].__class__.__name__ == "KalshiProcessor"
    # An unknown venue has no processor -> sequence gaps are n/a.
    assert make_processor("mysteryvenue", "whatever") == (None, None)


# --------------------------------------------------------------------------------------
# Clean feed: ~100% uptime, zero sequence gaps, exact rate/disk.
# --------------------------------------------------------------------------------------
def test_clean_feed_full_uptime_no_gaps_exact_rate_and_disk():
    # Window tight around the dense data: first msg at T0, last at T0+9s.
    fh = compute_feed_health(DATA_ROOT, "coinbase", "full", T0, T0 + 10 * NS, max_quiet_s=2.0)

    # Volume / rate: 10 known messages over an exact 9s span -> exact msgs/sec.
    assert fh.n_messages == 10
    assert fh.first_ts_ns == T0 and fh.last_ts_ns == T0 + 9 * NS
    assert fh.msgs_per_s == pytest.approx(10 / 9)
    assert fh.per_hour_counts == {"2025-10-09T08": 10}

    # Coverage: dense (1s < max_quiet 2s) and window hugs the data -> full uptime, no gaps.
    assert fh.uptime_fraction == pytest.approx(1.0)
    assert fh.gap_time_fraction == pytest.approx(0.0)
    assert fh.downtime_ns == 0
    assert fh.coverage_gaps == []

    # All 10 frames are book data (no acks/heartbeats in this fixture).
    assert fh.data_classified is True
    assert fh.n_control_frames == 0

    # Sequence gaps: contiguous Coinbase sequence -> processor emits none; count is exact.
    assert fh.sequence_gaps_supported is True
    assert fh.n_sequence_gaps == 0
    assert fh.sequence_gap_reasons == {}
    assert fh.sequence_gaps_exact is True
    assert fh.sequence_gap_events == []

    # Health gate: dense + zero gap time + rate above min -> healthy.
    assert fh.healthy is True

    # Disk: exact on-disk byte total (one hourly file).
    assert fh.n_files == 1
    assert fh.disk_bytes == _disk_bytes("coinbase", "full")
    assert fh.disk_bytes > 0
    assert sum(fh.disk_bytes_per_day.values()) == fh.disk_bytes


# --------------------------------------------------------------------------------------
# Broken feed: sequence gap AND coverage hole; uptime drops; everything reported.
# --------------------------------------------------------------------------------------
def test_broken_feed_surfaces_sequence_gap_and_coverage_hole():
    window = 3 * HOUR
    fh = compute_feed_health(DATA_ROOT, "bitstamp", "live_orders_btcusd",
                             T0, T0 + window, max_quiet_s=60.0)

    # --- Sequence gap (processor-detected, NOT re-implemented here). ---
    assert fh.sequence_gaps_supported is True
    assert fh.n_sequence_gaps == 1
    assert fh.sequence_gap_reasons == {"event_id chain break": 1}
    assert fh.sequence_gaps_exact is True  # Bitstamp re-anchors cleanly (not an upper bound)
    assert fh.sequence_gap_events == [
        {"reason": "event_id chain break", "expected": "e3", "got": "e4",
         "recv_ts_ns": T0 + 2 * HOUR}
    ]

    # --- Coverage hole: missing hour 09 is surfaced as the largest no-message gap. ---
    # Messages: hour08 at +0,+1,+2s ; hour10 at +7200,+7201s. The hole from +2s to +7200s
    # (7198s) and the tail from +7201s to window end (3599s) exceed max_quiet (60s).
    assert len(fh.coverage_gaps) == 2
    biggest = fh.coverage_gaps[0]
    assert isinstance(biggest, CoverageGap)
    assert biggest.start_ns == T0 + 2 * NS
    assert biggest.end_ns == T0 + 2 * HOUR
    assert biggest.duration_s == pytest.approx(7198.0)
    assert fh.coverage_gaps[1].duration_s == pytest.approx(3599.0)

    # Per-hour counts show the quiet vs active periods (no hour 09 key at all).
    assert fh.per_hour_counts == {"2025-10-09T08": 3, "2025-10-09T10": 2}

    # --- Uptime drops correctly and the gap-time fraction is reported (1.3's <0.1% check). ---
    downtime_s = 7198.0 + 3599.0
    assert fh.downtime_ns == int(downtime_s) * NS
    assert fh.gap_time_fraction == pytest.approx(downtime_s / (window / NS))
    assert fh.uptime_fraction == pytest.approx(1.0 - fh.gap_time_fraction)
    assert fh.uptime_fraction < 0.01  # dropped far below the 1.3 target
    assert fh.healthy is False  # fails the gap-time clause of the 1.3 gate

    # --- Rate + disk still computed on the broken feed. ---
    assert fh.n_messages == 5
    assert fh.n_control_frames == 0 and fh.data_classified is True
    span_s = (fh.last_ts_ns - fh.first_ts_ns) / NS  # (7201)s
    assert span_s == pytest.approx(7201.0)
    assert fh.msgs_per_s == pytest.approx(5 / 7201)
    assert fh.n_files == 2  # hour 08 + hour 10 (hour 09 absent)
    assert fh.disk_bytes == _disk_bytes("bitstamp", "live_orders_btcusd")
    assert fh.disk_bytes > 0


# --------------------------------------------------------------------------------------
# Full report + summary + rendering + JSON.
# --------------------------------------------------------------------------------------
def test_compute_health_report_and_summary():
    report = compute_health(DATA_ROOT, T0, T0 + 3 * HOUR, max_quiet_s=60.0)

    assert [(f.venue, f.channel) for f in report.feeds] == [
        ("bitstamp", "live_orders_btcusd"),
        ("coinbase", "full"),
    ]
    # Overall aggregation.
    assert report.total_messages == 15  # 5 bitstamp + 10 coinbase
    assert report.total_disk_bytes == _disk_bytes("bitstamp", "live_orders_btcusd") + \
        _disk_bytes("coinbase", "full")
    assert report.total_sequence_gaps == 1
    assert report.min_uptime_fraction < 0.01
    assert report.max_gap_time_fraction > 0.99
    # Over this wide window both tiny feeds stall -> not healthy.
    assert report.all_healthy is False
    assert set(report.unhealthy_feeds) == {"bitstamp/live_orders_btcusd", "coinbase/full"}


def test_auto_window_defaults_to_data_bounds():
    # No explicit window -> spans min..max recv_ts across all feeds.
    report = compute_health(DATA_ROOT, max_quiet_s=60.0)
    assert report.window_start_ns == T0  # earliest record
    assert report.window_end_ns == T0 + 2 * HOUR + 1 * NS + 1  # latest record + 1
    assert report.total_messages == 15


def test_render_table_and_json_are_consistent():
    report = compute_health(DATA_ROOT, T0, T0 + 3 * HOUR, max_quiet_s=60.0)

    table = render_table(report)
    assert "UPTIME%" in table and "SEQ-GAPS" in table and "GAP-TIME%" in table
    assert "HEALTHY" in table
    assert "bitstamp" in table and "coinbase" in table
    assert "OVERALL" in table and "UNHEALTHY" in table  # both feeds stalled over this window

    blob = json.loads(json.dumps(report.to_dict()))  # must be JSON-serializable
    assert blob["summary"]["total_messages"] == 15
    assert blob["summary"]["total_sequence_gaps"] == 1
    assert blob["summary"]["all_healthy"] is False
    bits = next(f for f in blob["feeds"] if f["venue"] == "bitstamp")
    assert bits["n_sequence_gaps"] == 1
    assert bits["sequence_gaps_exact"] is True
    assert bits["sequence_gap_reasons"] == {"event_id chain break": 1}
    assert bits["sequence_gap_events"][0]["expected"] == "e3"
    assert bits["healthy"] is False
    assert len(bits["coverage_gaps"]) == 2


def test_empty_window_raises():
    with pytest.raises(ValueError):
        compute_feed_health(DATA_ROOT, "coinbase", "full", T0, T0)


# --------------------------------------------------------------------------------------
# F1 regression — a heartbeat-only stall must NOT pass: it is downtime, not liveness.
# --------------------------------------------------------------------------------------
def test_classifier_strips_control_frames():
    is_data = data_classifier("bitstamp")
    assert is_data is not None
    assert is_data(_bitstamp_order("e1", "e0", 1, 1000)) is True
    assert is_data(_bitstamp_heartbeat()) is False
    # Coinbase: book events are data; subscription ack + heartbeat are control.
    cb = data_classifier("coinbase")
    assert cb(_coinbase_open(101, "o1")) is True
    assert cb(json.dumps({"type": "subscriptions", "channels": []})) is False
    assert cb(json.dumps({"type": "heartbeat", "sequence": 5})) is False
    # Unknown venue -> no classifier (caller counts all frames).
    assert data_classifier("mysteryvenue") is None


def test_heartbeat_only_stall_counts_as_downtime_not_uptime(tmp_path):
    """THE key false-pass regression: a feed that stops delivering book data but keeps emitting
    heartbeats inside max_quiet_s must show the stall as a coverage gap + low data rate and be
    reported UNHEALTHY — never ~100% uptime."""
    root = tmp_path / "raw"
    # Data at +0s and +599s; ONLY heartbeats (every 10s) in between. max_quiet_s=30 > 10s, so if
    # heartbeats counted as data there would be NO gap at all — the exact false pass we guard.
    with RotatingZstdWriter(root, "bitstamp", "live_orders_btcusd") as w:
        w.write(_bitstamp_order("e1", "e0", 1, 1000), recv_ts_ns=T0 + 0 * NS)
        for i in range(1, 60):
            w.write(_bitstamp_heartbeat(), recv_ts_ns=T0 + i * 10 * NS)
        w.write(_bitstamp_order("e2", "e1", 2, 1001), recv_ts_ns=T0 + 599 * NS)

    fh = compute_feed_health(root, "bitstamp", "live_orders_btcusd",
                             T0, T0 + 600 * NS, max_quiet_s=30.0)

    # Only the 2 book events are data; the 59 heartbeats are excluded.
    assert fh.data_classified is True
    assert fh.n_messages == 2
    assert fh.n_control_frames == 59

    # Despite a heartbeat every 10s (< max_quiet 30s), the stall IS a coverage gap.
    assert len(fh.coverage_gaps) == 1
    assert fh.coverage_gaps[0].start_ns == T0 + 0 * NS
    assert fh.coverage_gaps[0].end_ns == T0 + 599 * NS
    assert fh.coverage_gaps[0].duration_s == pytest.approx(599.0)
    assert fh.gap_time_fraction == pytest.approx(599 / 600)

    # Data rate reflects the stall, and the feed is NOT healthy.
    assert fh.msgs_per_s == pytest.approx(2 / 599)
    assert fh.healthy is False


# --------------------------------------------------------------------------------------
# F2 regression — prime->baseline + re-seed-after-gap: one real jump = exactly ONE gap.
# --------------------------------------------------------------------------------------
def test_sequence_jump_is_one_gap_not_a_cascade(tmp_path):
    """Coinbase sequence 101,102,105,106 (103,104 dropped). The prime seeds the baseline from
    101, the jump at 105 emits ONE gap (expected 103, got 105), and the re-seed stops it
    cascading through 106."""
    root = tmp_path / "raw"
    with RotatingZstdWriter(root, "coinbase", "full") as w:
        for i, seq in enumerate((101, 102, 105, 106)):
            w.write(_coinbase_open(seq, f"g{i}"), recv_ts_ns=T0 + i * NS)

    fh = compute_feed_health(root, "coinbase", "full", T0, T0 + 5 * NS, max_quiet_s=2.0)

    assert fh.n_messages == 4  # all four are book-data frames
    assert fh.sequence_gaps_supported is True
    assert fh.sequence_gaps_exact is True
    assert fh.n_sequence_gaps == 1  # exactly one — no cascade through 106
    assert fh.sequence_gap_reasons == {"sequence jump": 1}
    assert fh.sequence_gap_events == [
        {"reason": "sequence jump", "expected": 103, "got": 105, "recv_ts_ns": T0 + 2 * NS}
    ]


def test_kraken_sequence_gap_count_marked_as_upper_bound():
    """F4: Kraken's in-band CRC resync makes its replayed gap count an upper bound -> exact=False."""
    proc, _ = make_processor("kraken", "level3")
    assert proc is not None
    # No data needed: the exactness flag is a per-venue property surfaced to JSON consumers.
    from kbtc.recorders.health import _UPPER_BOUND_GAP_VENUES
    assert "kraken" in _UPPER_BOUND_GAP_VENUES
    assert "coinbase" not in _UPPER_BOUND_GAP_VENUES
    assert "bitstamp" not in _UPPER_BOUND_GAP_VENUES
