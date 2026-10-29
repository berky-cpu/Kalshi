"""Tests for the 4.2 per-market loader (kbtc.backtest.loader)."""
import json

import pytest

from kbtc.backtest.loader import (
    RecvOrderError,
    cf_index_ticks,
    cf_value_from_msg,
    hour_files_covering,
    market_from_settlement_row,
    market_records_in_window,
)

NS = 1_000_000_000


def test_market_from_settlement_row():
    row = {"market_ticker": "KXBTC15M-26OCT201500-00", "result": "yes",
           "open_value": "81000.5", "close_value": "81010.0",
           "open_time_ns": 100 * NS, "close_time_ns": 1000 * NS}
    m = market_from_settlement_row(row)
    assert m.ticker == "KXBTC15M-26OCT201500-00" and m.result_yes is True
    assert float(m.strike) == 81000.5 and m.open_time_ns == 100 * NS


def test_market_from_settlement_row_rejects_unsettled():
    assert market_from_settlement_row({"result": "", "market_ticker": "X",
                                       "open_value": "1", "open_time_ns": 1, "close_time_ns": 2}) is None
    assert market_from_settlement_row({"result": "yes", "market_ticker": "X",
                                       "open_value": None, "open_time_ns": 1, "close_time_ns": 2}) is None


def _cf_frame(value, time_ms):
    return {"type": "cfbenchmarks_value", "sid": 1, "seq": 1,
            "msg": {"index_id": "BRTI", "received_at": time_ms,
                    "data": json.dumps({"type": "value", "time": time_ms, "id": "BRTI",
                                        "value": f"{value:.2f}"})}}


def test_cf_value_from_msg():
    assert cf_value_from_msg(_cf_frame(80775.34, 123)) == pytest.approx(80775.34)
    assert cf_value_from_msg({"type": "other"}) is None
    assert cf_value_from_msg(_cf_frame(80000.0, 1)) == pytest.approx(80000.0)


def test_cf_index_ticks_windowed_sorted_deduped():
    recs = [
        (50 * NS, _cf_frame(80000, 50)),   # before window -> dropped
        (200 * NS, _cf_frame(80002, 200)),
        (100 * NS, _cf_frame(80001, 100)),
        (100 * NS, _cf_frame(80005, 100)),  # dup ts -> keep last
        (5000 * NS, _cf_frame(80010, 5000)),  # after window -> dropped
    ]
    ticks = cf_index_ticks(recs, lo_ns=100 * NS, hi_ns=1000 * NS)
    assert [t.recv_ts_ns for t in ticks] == [100 * NS, 200 * NS]
    assert ticks[0].price == pytest.approx(80005)  # last at dup ts
    assert [t.recv_ts_ns for t in ticks] == sorted(t.recv_ts_ns for t in ticks)


def test_market_records_window_and_order_check():
    recs = [(10, "a"), (20, "b"), (30, "c"), (40, "d")]
    out = market_records_in_window(recs, 15, 35)
    assert out == [(20, "b"), (30, "c")]


def test_market_records_raises_on_backwards_ts():
    with pytest.raises(RecvOrderError):
        market_records_in_window([(10, "a"), (5, "b")], 0, 100)


def test_hour_files_covering_selects_overlap_only():
    paths = [
        "/d/date=2026-10-20/22_2000000000000000000.jsonl.zst",
        "/d/date=2026-10-20/23_2000003600000000000.jsonl.zst",
        "/d/date=2026-10-21/00_2000007200000000000.jsonl.zst",
    ]
    # window inside the 23:00 hour only
    lo, hi = 2000003700000000000, 2000003800000000000
    cover = hour_files_covering(paths, lo, hi)
    assert cover == [paths[1]]
    # window spanning the 23:00 -> 00:00 boundary picks both
    cover2 = hour_files_covering(paths, 2000006900000000000, 2000007300000000000)
    assert set(cover2) == {paths[1], paths[2]}
