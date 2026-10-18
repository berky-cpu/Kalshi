"""Trade-tape normalization to the one shared schema (ROADMAP 2.1).

Fixtures cover Coinbase ``match`` and Kalshi ``trade``; Binance ``aggTrade`` / Bybit
``publicTrade`` are not in the book fixtures, so those parsers are exercised directly on
representative frames (field layout per ``docs/feeds.md``; Binance aggTrade UNVERIFIED there).
"""
from __future__ import annotations

from decimal import Decimal

from conftest import load_fixture

from kbtc.normalize import extract, pipeline

T0 = 1_760_000_000_000_000_000


def _records(venue, name):
    for i, msg in enumerate(load_fixture(venue, name)):
        yield T0 + i, msg


def test_coinbase_match_taker_side_is_opposite_of_maker():
    res = pipeline.normalize_crypto("coinbase", "full", _records("coinbase", "full.jsonl"))
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["venue"] == "coinbase"
    assert t["price"] == Decimal("27010.00")
    assert t["size"] == Decimal("0.40")
    # the fixture match carries no maker 'side' → taker side is unknown (null), never guessed.
    assert t["side"] is None
    assert t["recv_ts_ns"] == T0 + 4


def test_coinbase_match_taker_side_derived_when_maker_side_present():
    frame = {"type": "match", "product_id": "BTC-USD", "trade_id": 55,
             "maker_order_id": "o2", "side": "sell", "price": "27010.00", "size": "0.40",
             "time": "2026-10-17T00:00:01.5Z"}
    rows = extract.trade_rows("coinbase", "full", frame)
    assert rows[0]["side"] == "buy"  # maker sold ⇒ taker bought
    assert rows[0]["trade_id"] == "55"
    assert rows[0]["exch_ts_ns"] == extract._iso_ns("2026-10-17T00:00:01.5Z")
    assert rows[0]["exch_ts_ns"] is not None


def test_binance_aggtrade_side_from_buyer_is_maker_flag():
    buy = {"e": "aggTrade", "s": "BTCUSDT", "a": 12, "p": "27000.1", "q": "0.3",
           "T": 1700000000000, "m": False}
    sell = {**buy, "a": 13, "m": True}
    rb = extract.trade_rows("binance", "perp", buy)[0]
    rs = extract.trade_rows("binance", "perp", sell)[0]
    assert rb["side"] == "buy" and rs["side"] == "sell"  # m=False ⇒ taker buy; m=True ⇒ taker sell
    assert rb["price"] == Decimal("27000.1") and rb["size"] == Decimal("0.3")
    assert rb["trade_id"] == "12"
    assert rb["exch_ts_ns"] == 1700000000000 * 1_000_000


def test_binance_aggtrade_in_combined_stream_envelope():
    env = {"stream": "btcusdt@aggTrade",
           "data": {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "1.0", "q": "2.0",
                    "T": 1, "m": False}}
    rows = extract.trade_rows("binance", "perp", env)
    assert rows and rows[0]["price"] == Decimal("1.0")


def test_bybit_publictrade_taker_side_and_fields():
    frame = {"topic": "publicTrade.BTCUSDT", "type": "snapshot",
             "data": [{"s": "BTCUSDT", "S": "Buy", "p": "27001.5", "v": "0.01",
                       "i": "abc-1", "T": 1700000000123}]}
    rows = extract.trade_rows("bybit", "perp", frame)
    assert len(rows) == 1
    t = rows[0]
    assert t["side"] == "buy"
    assert t["price"] == Decimal("27001.5") and t["size"] == Decimal("0.01")
    assert t["trade_id"] == "abc-1"
    assert t["exch_ts_ns"] == 1700000000123 * 1_000_000


def test_kalshi_trade_normalized():
    res = pipeline.normalize_kalshi("market", _records("kalshi", "orderbook.jsonl"))
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["venue"] == "kalshi"
    assert t["side"] == "yes"                      # taker_side (aggressor)
    assert t["price"] == Decimal("0.4100")         # yes_price_dollars (dollars; no_* is complement)
    assert t["size"] == Decimal("5.00")            # count_fp (contracts)
    assert t["trade_id"] == "d290f1ee-6c54-4b01-90e6-d701748f0851"
    assert t["exch_ts_ns"] == 1760000000500 * 1_000_000   # ts_ms → ns


def test_iso_ns_is_exact_integer_nanoseconds_no_float_loss():
    # red-team #6: microsecond precision must survive as exact integer ns (int(ts*1e9) would drift).
    base = extract._iso_ns("2026-10-17T12:34:56Z")
    assert isinstance(base, int)
    v = extract._iso_ns("2026-10-17T12:34:56.789012Z")
    assert v == base + 789012 * 1000          # exact: .789012 s → 789012000 ns
    assert v % 1_000_000_000 == 789012000       # no sub-microsecond float noise


def test_non_trade_frames_yield_no_rows():
    assert extract.trade_rows("kraken", "level3", {"channel": "level3", "type": "update"}) == []
    assert extract.trade_rows("bitstamp", "live_orders_btcusd",
                              {"event": "order_created", "data": {}}) == []
