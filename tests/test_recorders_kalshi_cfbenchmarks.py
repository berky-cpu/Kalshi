"""Replay + wiring tests for the Kalshi-relayed CF Benchmarks index-value recorder.

Schema is WIRE-VERIFIED (read-only authed prod probe 2026-10-24) + primary doc
``docs.kalshi.com/websockets/cfbenchmarks-value`` (read 2026-10-24): envelope top-level
``type``/``sid``/``seq``/``sending_ts_ms`` with the payload nested under ``msg``; the
``cfbenchmarks_value`` msg carries ``index_id``/``received_at``/``data`` + ``avg_60s_data`` (always)
and ``last_60s_windowed_average_15min`` (only in the final minute before a quarter-hour close); the
``cfbenchmarks_value_5hz`` msg carries ``value_usd``/``source_ts_ms``/``received_at``/``data`` and no
averages. Pass-through recorder — no book — so tests assert faithful field capture + seq-gap.
"""
from __future__ import annotations

from conftest import load_fixture

from kbtc.recorders import kalshi_cfbenchmarks as cfb
from kbtc.recorders.health import _is_data_kalshi, make_processor
from kbtc.recorders.kalshi_cfbenchmarks import KalshiCfbenchmarksProcessor


def test_subscribe_envelope_uses_index_ids_both_channels():
    subs = cfb.subscribe_messages()
    # One subscribe per channel (wire-verified envelope), BOTH cfbenchmarks channels, keyed by
    # index_ids (NOT market_tickers), for BRTI.
    assert [s["params"]["channels"][0] for s in subs] == ["cfbenchmarks_value",
                                                           "cfbenchmarks_value_5hz"]
    for s in subs:
        assert s["cmd"] == "subscribe"
        assert s["params"]["index_ids"] == ["BRTI"]
        assert "market_tickers" not in s["params"]
    # Distinct ids so each channel's ``subscribed`` ack/sid is unambiguous.
    assert len({s["id"] for s in subs}) == len(subs)


def test_parse_cfbenchmarks_value_captures_all_fields_and_both_averages():
    msgs = load_fixture("kalshi", "cfbenchmarks.jsonl")
    p = KalshiCfbenchmarksProcessor()
    ts = 1_761_300_000_000_000_000
    events = []
    for i, m in enumerate(msgs):
        events += p.process(m, ts + i)

    # subscribed acks captured channel -> sid; not recorded as values.
    assert p.sid_by_channel == {"cfbenchmarks_value": 5, "cfbenchmarks_value_5hz": 6}

    # First 1 Hz value frame: full msg captured verbatim incl. the raw CF ``data`` JSON string and
    # the always-present trailing avg (window_size recorded as-is, 0 — NOT interpreted).
    v0 = p.values[0]
    assert v0["index_id"] == "BRTI"
    assert v0["received_at"] == 1761300000012
    assert v0["data"] == '{"type":"value","time":1761300000000,"id":"BRTI","value":"68123.45"}'
    assert v0["avg_60s_data"]["value"] == "68120.11000000"
    assert v0["avg_60s_data"]["window_size"] == 0  # quirk recorded verbatim, not interpreted
    assert "last_60s_windowed_average_15min" not in v0  # not the final pre-close minute


def test_last_60s_windowed_average_15min_captured_when_present():
    """The FORMING SETTLEMENT AVERAGE field is captured verbatim on the final-minute frame."""
    msgs = load_fixture("kalshi", "cfbenchmarks.jsonl")
    p = KalshiCfbenchmarksProcessor()
    for i, m in enumerate(msgs):
        p.process(m, i)
    final = next(v for v in p.values if "last_60s_windowed_average_15min" in v)
    settle = final["last_60s_windowed_average_15min"]
    assert settle["value"] == "68145.33000000"
    assert settle["window_size"] == 60  # (close-60s, close] = 60 samples
    assert settle["window_end_ts_exclusive"] == 1761300840001


def test_parse_cfbenchmarks_value_5hz_fields():
    msgs = load_fixture("kalshi", "cfbenchmarks.jsonl")
    p = KalshiCfbenchmarksProcessor()
    for i, m in enumerate(msgs):
        p.process(m, i)
    tick = next(v for v in p.values if "value_usd" in v)
    assert tick["index_id"] == "BRTI"
    assert tick["value_usd"] == "68123.80000000"
    assert tick["source_ts_ms"] == 1761300000200
    assert "avg_60s_data" not in tick  # 5 Hz carries no averages


def test_seq_gap_detected_and_arms_resync():
    msgs = load_fixture("kalshi", "cfbenchmarks.jsonl")
    p = KalshiCfbenchmarksProcessor()
    for i, m in enumerate(msgs):
        p.process(m, i)
    # On sid 5 the 1 Hz stream jumps seq 2 -> 4 (seq 3 missed): one gap, resync armed, never silent.
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "seq jump"
    assert p.gaps[0].expected == 3 and p.gaps[0].got == 4
    assert p.needs_resnapshot is True


def test_duplicate_seq_ignored_no_gap():
    p = KalshiCfbenchmarksProcessor()
    base = {"type": "cfbenchmarks_value", "sid": 5,
            "msg": {"index_id": "BRTI", "value": "1"}}
    p.process({**base, "seq": 1}, 1)
    p.process({**base, "seq": 2}, 2)
    p.process({**base, "seq": 2}, 3)  # duplicate / out-of-order
    assert p.gaps == []
    assert len(p.values) == 2  # the duplicate is dropped, never double-recorded


def test_reset_subscriptions_clears_sids_and_seq():
    p = KalshiCfbenchmarksProcessor()
    p.process({"type": "subscribed", "sid": 5, "seq": 1,
               "msg": {"channel": "cfbenchmarks_value", "sid": 5}}, 1)
    p.process({"type": "cfbenchmarks_value", "sid": 5, "seq": 1, "msg": {"index_id": "BRTI"}}, 2)
    assert p.sid_by_channel and p._seq_by_sid
    p.reset_subscriptions()
    assert p.sid_by_channel == {} and p._seq_by_sid == {}


def test_data_classifier_value_is_data_control_is_not():
    # A cfbenchmarks value frame counts as DATA (keeps the staleness watchdog honest)...
    assert _is_data_kalshi({"type": "cfbenchmarks_value", "sid": 5, "seq": 1,
                            "msg": {"index_id": "BRTI"}}) is True
    assert _is_data_kalshi({"type": "cfbenchmarks_value_5hz", "sid": 6, "seq": 1,
                            "msg": {"index_id": "BRTI"}}) is True
    # ...while the subscribe ack / heartbeat do NOT (a heartbeat-only stall must look unhealthy).
    assert _is_data_kalshi({"type": "subscribed", "sid": 5,
                            "msg": {"channel": "cfbenchmarks_value", "sid": 5}}) is False
    assert _is_data_kalshi({"type": "heartbeat"}) is False
    # And the orderbook data types still classify as data (union did not break the market feed).
    assert _is_data_kalshi({"type": "orderbook_delta"}) is True


def test_health_make_processor_routes_cfbenchmarks_channel():
    proc, prime = make_processor("kalshi", "cfbenchmarks")
    assert isinstance(proc, KalshiCfbenchmarksProcessor)
    assert prime is None
