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
    is_batch_dataset,
    main,
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


# --------------------------------------------------------------------------------------
# (a) Batch dataset exclusion — settlement is a 5-min batch job, NOT a streaming feed.
# --------------------------------------------------------------------------------------
def test_settlement_batch_dataset_excluded_from_streaming_health(tmp_path):
    """``kalshi/settlement`` is written by a 5-minute BATCH job: a few records every 5 min. The
    long quiet intervals between batches are not downtime, so the streaming health report must
    NOT discover it as a feed, must not emit a per-feed row for it, and must not let its (false)
    0% uptime drag the overall ``healthy`` verdict."""
    root = tmp_path / "raw"
    # A real streaming feed: dense Coinbase book events, 1/s.
    with RotatingZstdWriter(root, "coinbase", "full") as w:
        for i, seq in enumerate(range(101, 111)):
            w.write(_coinbase_open(seq, f"o{i}"), recv_ts_ns=T0 + i * NS)
    # The settlement batch dataset: a handful of records, one burst every 5 minutes.
    with RotatingZstdWriter(root, "kalshi", "settlement") as w:
        for i in range(3):
            w.write(json.dumps({"type": "settlement", "market": "KXBTC", "i": i}),
                    recv_ts_ns=T0 + i * 300 * NS)

    # The batch classifier flags settlement; discover_feeds still lists it (the normalizer reuses
    # discover_feeds and must see settlement), but the HEALTH REPORT excludes it.
    assert is_batch_dataset("kalshi", "settlement") is True
    assert is_batch_dataset("coinbase", "full") is False
    assert ("kalshi", "settlement") in discover_feeds(root)  # shared discovery still lists it

    report = compute_health(root, T0, T0 + 10 * NS, max_quiet_s=2.0)
    # No settlement row, and the lone streaming feed is healthy -> overall healthy.
    assert [(f.venue, f.channel) for f in report.feeds] == [("coinbase", "full")]
    assert "kalshi/settlement" not in report.unhealthy_feeds
    assert report.all_healthy is True


# --------------------------------------------------------------------------------------
# (b) Own-window rule — a feed is judged only over the period it was actually recording.
# --------------------------------------------------------------------------------------
def test_own_window_clamp_counts_mid_and_tail_gaps_but_not_lead_in(tmp_path):
    """F5 (single-feed view): a feed is judged over its OWN running window. The pre-first-data
    lead-in is dropped (not downtime) and surfaced as ``late_start_s``, but a genuine mid-stream
    hole WITHIN its span still counts. The cross-feed outage check is exercised separately."""
    root = tmp_path / "raw"
    with RotatingZstdWriter(root, "coinbase", "full") as w:
        seq = 101
        secs = list(range(50, 141, 2)) + list(range(180, 199, 2))  # start +50s, hole +140..+180
        for s in secs:
            w.write(_coinbase_open(seq, f"c{seq}"), recv_ts_ns=T0 + s * NS)
            seq += 1

    fh = compute_feed_health(root, "coinbase", "full", T0, T0 + 200 * NS, max_quiet_s=5.0)

    assert fh.first_ts_ns == T0 + 50 * NS
    assert fh.window_start_ns == T0 + 50 * NS       # coverage clamped to own first data (F5)
    assert fh.late_start_s == pytest.approx(50.0)   # the dropped lead-in is still surfaced
    assert fh.down_from_start is False              # single-feed compute never cross-flags

    # Exactly one mid-stream hole; NO lead-in gap from T0.
    assert len(fh.coverage_gaps) == 1
    assert fh.coverage_gaps[0].start_ns == T0 + 140 * NS
    assert fh.coverage_gaps[0].end_ns == T0 + 180 * NS
    assert fh.coverage_gaps[0].duration_s == pytest.approx(40.0)
    assert fh.downtime_ns == 40 * NS
    # Gap fraction is over the OWN window (150s), not the full 200s window.
    assert fh.gap_time_fraction == pytest.approx(40 * NS / (fh.window_end_ns - fh.window_start_ns))


def test_dead_from_start_feed_flagged_when_another_feed_covered_the_lead_in(tmp_path):
    """F1 (the dangerous direction red-team flagged): one feed is dead for nearly the whole
    recording period while another records throughout. The own-window clamp ALONE would read the
    dead feed ~100% uptime / healthy (the mask); the cross-feed discriminator must flag it
    (``down_from_start``, ``healthy=False``) so ``all_healthy`` is False and the CLI exits non-zero."""
    root = tmp_path / "raw"
    # Feed Y records throughout [T0 .. T0+599s], 1/s -> proves the box was live and recording.
    with RotatingZstdWriter(root, "coinbase", "full") as w:
        for i, seq in enumerate(range(101, 701)):
            w.write(_coinbase_open(seq, f"c{i}"), recv_ts_ns=T0 + i * NS)
    # Feed X has NO data until the last 5 seconds of that period.
    with RotatingZstdWriter(root, "bitstamp", "live_orders_btcusd") as w:
        prev = "e0"
        for i, s in enumerate(range(595, 600)):
            eid = f"e{i + 1}"
            w.write(_bitstamp_order(eid, prev, i, 1000 + i), recv_ts_ns=T0 + s * NS)
            prev = eid

    report = compute_health(root)  # auto window = [T0 .. T0+599s + 1]
    feeds = {(f.venue, f.channel): f for f in report.feeds}
    x = feeds[("bitstamp", "live_orders_btcusd")]
    y = feeds[("coinbase", "full")]

    # The MASK that F5 alone would produce: X reads ~100% up over its own 5-second window...
    assert x.uptime_fraction == pytest.approx(1.0)
    # ...but the cross-feed check catches the real outage and gates it.
    assert x.down_from_start is True
    assert x.healthy is False
    assert x.late_start_s == pytest.approx(595.0)
    assert "bitstamp/live_orders_btcusd" in report.unhealthy_feeds

    # Y recorded throughout -> a legitimate early feed, not flagged.
    assert y.down_from_start is False
    assert y.late_start_s == pytest.approx(0.0)

    # Overall verdict fails and the CLI exits non-zero so a cron/systemd alarm fires.
    assert report.all_healthy is False
    assert main(["--root", str(root)]) == 1

    # The table surfaces the late start so a 99%-dead feed cannot hide as "yes / 100.000".
    table = render_table(report)
    assert "LATE-START" in table
    assert "595s!" in table


def test_shared_late_start_with_uncovered_lead_in_is_not_flagged(tmp_path):
    """F1 (the legitimate direction): both feeds come up together after the window start (e.g. the
    box booted then) — NO other feed covered the lead-in — so neither is flagged and both read
    healthy over their own windows. The late-start fix must NOT fire here (no false alarm)."""
    root = tmp_path / "raw"
    with RotatingZstdWriter(root, "coinbase", "full") as w:
        for i, seq in enumerate(range(101, 302)):          # +100..+300s, 1/s
            w.write(_coinbase_open(seq, f"c{i}"), recv_ts_ns=T0 + (100 + i) * NS)
    with RotatingZstdWriter(root, "bitstamp", "live_orders_btcusd") as w:
        prev = "e0"
        for i in range(201):                                # +100..+300s, 1/s
            eid = f"e{i + 1}"
            w.write(_bitstamp_order(eid, prev, i, 1000 + i), recv_ts_ns=T0 + (100 + i) * NS)
            prev = eid

    # Explicit window STARTS before both feeds (T0), yet nothing recorded in [T0 .. +100s].
    report = compute_health(root, T0, T0 + 301 * NS, max_quiet_s=5.0)
    feeds = {(f.venue, f.channel): f for f in report.feeds}
    for key in (("coinbase", "full"), ("bitstamp", "live_orders_btcusd")):
        f = feeds[key]
        assert f.first_ts_ns == T0 + 100 * NS
        assert f.late_start_s == pytest.approx(100.0)   # a material lead-in...
        assert f.down_from_start is False               # ...but UNCOVERED -> not a flagged outage
        assert f.uptime_fraction == pytest.approx(1.0)  # judged over its own window only
        assert f.healthy is True
    assert report.all_healthy is True
    assert main(["--root", str(root), "--start", str(T0), "--end", str(T0 + 301 * NS)]) == 0


def test_feeds_booting_seconds_apart_under_explicit_start_are_not_flagged(tmp_path):
    """F1 refinement: with an operator-supplied ``--start`` predating the feeds, two feeds that
    come up within < max_quiet_s of each other must NOT trip the outage gate. Gating is on the
    DEMONSTRABLE cross-feed silent span (here 3s < max_quiet 5s), not on lateness vs the requested
    start (both are ~100s late relative to --start)."""
    root = tmp_path / "raw"
    # Feed A up at +100s; feed B up at +103s (3s later). Both dense 1/s to +300s.
    with RotatingZstdWriter(root, "coinbase", "full") as w:
        for i, seq in enumerate(range(101, 302)):          # +100..+300s
            w.write(_coinbase_open(seq, f"c{i}"), recv_ts_ns=T0 + (100 + i) * NS)
    with RotatingZstdWriter(root, "bitstamp", "live_orders_btcusd") as w:
        prev = "e0"
        for i in range(198):                                # +103..+300s
            eid = f"e{i + 1}"
            w.write(_bitstamp_order(eid, prev, i, 1000 + i), recv_ts_ns=T0 + (103 + i) * NS)
            prev = eid

    report = compute_health(root, T0, T0 + 301 * NS, max_quiet_s=5.0)
    feeds = {(f.venue, f.channel): f for f in report.feeds}
    late = feeds[("bitstamp", "live_orders_btcusd")]

    # B is late vs the requested --start (its display column shows ~103s)...
    assert late.first_ts_ns == T0 + 103 * NS
    assert late.late_start_s == pytest.approx(103.0)
    # ...but the provable silent span vs the earliest feed is only 3s (< max_quiet) -> NOT flagged.
    assert late.down_from_start is False
    assert late.healthy is True
    assert report.all_healthy is True


def test_kraken_sequence_gap_count_marked_as_upper_bound():
    """F4: Kraken's in-band CRC resync makes its replayed gap count an upper bound -> exact=False."""
    proc, _ = make_processor("kraken", "level3")
    assert proc is not None
    # No data needed: the exactness flag is a per-venue property surfaced to JSON consumers.
    from kbtc.recorders.health import _UPPER_BOUND_GAP_VENUES
    assert "kraken" in _UPPER_BOUND_GAP_VENUES
    assert "coinbase" not in _UPPER_BOUND_GAP_VENUES
    assert "bitstamp" not in _UPPER_BOUND_GAP_VENUES
