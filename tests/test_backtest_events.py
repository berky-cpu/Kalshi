"""Tests for raw Kalshi parsing, trade->level mapping, and the no-look-ahead merge."""
from decimal import Decimal

from kbtc.backtest.events import (
    BookDelta,
    BookSnapshot,
    IndexTick,
    Trade,
    iter_kalshi_events,
    merge_events,
    parse_kalshi_record,
)

D = Decimal
TICKER = "KXBTC15M-TEST"


def snap(seq=1):
    return {"type": "orderbook_snapshot", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER,
                    "yes_dollars_fp": [["0.40", "100.00"]],
                    "no_dollars_fp": [["0.55", "50.00"]]}}


def delta(side, price, d, seq):
    return {"type": "orderbook_delta", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER, "side": side, "price_dollars": price, "delta_fp": d}}


def trade(taker_side, yes_price, no_price, count, seq, ts_ms=0):
    return {"type": "trade", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER, "taker_side": taker_side,
                    "yes_price_dollars": yes_price, "no_price_dollars": no_price,
                    "count_fp": count, "ts_ms": ts_ms}}


def test_parse_snapshot_delta_trade():
    assert parse_kalshi_record(1, snap()) == [BookSnapshot(1, TICKER)]
    (d,) = parse_kalshi_record(2, delta("yes", "0.40", "-50.00", 2))
    assert isinstance(d, BookDelta) and d.side == "yes" and d.price == D("0.40") and d.delta == D("-50.00")
    (t,) = parse_kalshi_record(3, trade("no", "0.40", "0.60", "30.00", 3))
    assert isinstance(t, Trade) and t.size == D("30.00")


def test_trade_consumed_level_mapping():
    # taker buys NO -> lifts resting YES at the YES price.
    (t_no,) = parse_kalshi_record(1, trade("no", "0.40", "0.60", "10.00", 1))
    assert t_no.consumed_side == "yes" and t_no.consumed_price == D("0.40")
    # taker buys YES -> lifts resting NO at the NO price.
    (t_yes,) = parse_kalshi_record(1, trade("yes", "0.40", "0.60", "10.00", 1))
    assert t_yes.consumed_side == "no" and t_yes.consumed_price == D("0.60")


def test_no_price_defaults_to_complement():
    frame = {"type": "trade", "sid": 1, "seq": 1,
             "msg": {"market_ticker": TICKER, "taker_side": "yes",
                     "yes_price_dollars": "0.40", "count_fp": "5.00"}}
    (t,) = parse_kalshi_record(1, frame)
    assert t.no_price == D("0.60")  # 1 - 0.40


def test_non_book_frames_ignored():
    assert parse_kalshi_record(1, {"type": "subscribed", "msg": {}}) == []
    assert parse_kalshi_record(1, {"type": "heartbeat"}) == []


def test_merge_is_time_ordered_with_type_tiebreak():
    mkt = list(iter_kalshi_events([
        (100, snap()),
        (300, trade("no", "0.40", "0.60", "5.00", 2)),
    ]))
    idx = [IndexTick(200, 81000.0), IndexTick(100, 80990.0)]
    merged = merge_events(mkt, idx)
    ts = [e.recv_ts_ns for e in merged]
    assert ts == sorted(ts)  # strictly non-decreasing: no look-ahead
    # At ts=100 the index tick sorts before the snapshot (type tiebreak).
    assert isinstance(merged[0], IndexTick) and merged[0].recv_ts_ns == 100
    assert isinstance(merged[1], BookSnapshot)
