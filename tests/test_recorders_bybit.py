from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.bybit import BybitOrderbookProcessor, BybitPublicTradeProcessor


def test_orderbook_snapshot_delta_then_forced_snapshot_reset():
    msgs = load_fixture("bybit", "orderbook.jsonl")
    p = BybitOrderbookProcessor(product="BTCUSDT", depth=50)
    ts = 1_760_000_000_000_000_000

    p.process(msgs[0], ts)  # first snapshot
    p.process(msgs[1], ts + 1)  # delta
    p.process(msgs[2], ts + 2)  # delta
    assert not p.gaps
    assert p.book.bids[Decimal("27000.0")] == Decimal("0.5")
    assert p.book.bids[Decimal("26980.0")] == Decimal("4.0")
    assert Decimal("27010.0") not in p.book.asks
    assert p.book.asks[Decimal("27020.0")] == Decimal("3.0")

    # A mid-stream snapshot = Bybit-initiated resync: logged as a gap and the book is reset.
    p.process(msgs[3], ts + 3)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "snapshot reset"
    assert p.book.bids == {Decimal("27005.0"): Decimal("9.0")}
    assert p.book.asks == {Decimal("27015.0"): Decimal("1.0")}
    assert p.needs_resnapshot is False  # resync is in-band (the snapshot itself)


def test_delta_before_snapshot_is_a_gap():
    p = BybitOrderbookProcessor(product="BTCUSDT", depth=50)
    p.process({"topic": "orderbook.50.BTCUSDT", "type": "delta",
               "data": {"u": 5, "seq": 5, "b": [["1.0", "1.0"]], "a": []}}, 1)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "delta before snapshot"
    assert p.needs_resnapshot is True


def test_public_trade_tape():
    p = BybitPublicTradeProcessor(product="BTCUSDT")
    out = p.process({"topic": "publicTrade.BTCUSDT", "ts": 1,
                     "data": [{"T": 1, "S": "Buy", "v": "0.1", "p": "27000", "i": "t1"},
                              {"T": 1, "S": "Sell", "v": "0.2", "p": "27001", "i": "t2"}]}, 1)
    assert [t["trade_id"] for t in out] == ["t1", "t2"]
    assert p.last_trade_id == "t2"


def test_public_trade_gap_is_log_only():
    """A non-monotonic trade id logs a GapDetected but must NOT force a book resync (log-only)."""
    p = BybitPublicTradeProcessor(product="BTCUSDT")
    p.process({"topic": "publicTrade.BTCUSDT", "ts": 1,
               "data": [{"T": 1, "S": "Buy", "v": "0.1", "p": "27000", "i": "100"}]}, 1)
    p.process({"topic": "publicTrade.BTCUSDT", "ts": 2,
               "data": [{"T": 2, "S": "Sell", "v": "0.2", "p": "27001", "i": "101"}]}, 2)
    assert not p.gaps
    # Seeded out-of-order / duplicate trade id (101 again, not > last).
    p.process({"topic": "publicTrade.BTCUSDT", "ts": 3,
               "data": [{"T": 3, "S": "Buy", "v": "0.3", "p": "27002", "i": "101"}]}, 3)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "trade id non-monotonic"
    assert p.needs_resnapshot is False  # log-only: no book to rebuild
