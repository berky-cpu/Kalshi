"""Integration tests for the event loop: latency, no-look-ahead, partial fills, cancels,
trade-through mode, and settlement markout/P&L (kbtc.backtest.engine / markout)."""
from dataclasses import dataclass
from decimal import Decimal

from kbtc.backtest.engine import BacktestConfig, BacktestMarket, run_backtest
from kbtc.backtest.markout import markouts, net_settlement_pnl
from kbtc.backtest.policy import MarketState, QuoteIntent
from kbtc.backtest.queue import CancelPolicy, FillMode

D = Decimal
TICKER = "KXBTC15M-TEST"
T0 = 1_000_000_000
NS = 1_000_000_000


def snap(seq=1):
    return {"type": "orderbook_snapshot", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER,
                    "yes_dollars_fp": [["0.40", "100.00"]],
                    "no_dollars_fp": [["0.55", "50.00"]]}}


def trade(taker_side, yes_price, no_price, count, seq):
    return {"type": "trade", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER, "taker_side": taker_side,
                    "yes_price_dollars": yes_price, "no_price_dollars": no_price,
                    "count_fp": count, "ts_ms": 0}}


@dataclass
class FixedPolicy:
    intent: QuoteIntent

    def quotes(self, state: MarketState):
        return [self.intent]


@dataclass
class TimeGatedPolicy:
    """Wants the intent only while recv_ts_ns < cutoff (then withdraws it -> engine cancels)."""
    intent: QuoteIntent
    cutoff_ns: int

    def quotes(self, state: MarketState):
        return [self.intent] if state.recv_ts_ns < self.cutoff_ns else []


def market(result_yes=True):
    return BacktestMarket(ticker=TICKER, open_time_ns=T0 - 900 * NS, close_time_ns=T0 + 900 * NS,
                          strike=D("81000"), result_yes=result_yes)


BUY_YES = QuoteIntent(side="yes", price=D("0.40"), size=D("10"))


def test_latency_blocks_fill_before_effective_and_allows_after():
    cfg = BacktestConfig(submit_latency_ns=100_000_000, cancel_latency_ns=100_000_000)
    records = [
        (T0, snap()),
        (T0 + 50_000_000, trade("no", "0.40", "0.60", "120.00", 2)),   # before effective -> no fill
        (T0 + 200_000_000, trade("no", "0.40", "0.60", "120.00", 3)),  # after effective -> fill
    ]
    res = run_backtest(market(), FixedPolicy(BUY_YES), records, config=cfg)
    assert res.fill_count == 1
    f = res.fills[0]
    assert f.ts_ns == T0 + 200_000_000 and f.size == D("10") and f.book_side == "yes"
    assert f.fee == D("0.00")  # maker-free
    assert res.final_inventory == 10


def test_no_look_ahead_order_cannot_fill_at_its_own_effective_instant():
    # effective_ts == trade_ts: back of queue at the same instant -> no fill.
    cfg = BacktestConfig(submit_latency_ns=100_000_000)
    eff = T0 + 100_000_000
    records = [
        (T0, snap()),
        (eff, trade("no", "0.40", "0.60", "120.00", 2)),  # exactly at effective
    ]
    res = run_backtest(market(), FixedPolicy(BUY_YES), records, config=cfg)
    assert res.fill_count == 0


def test_partial_fills_accumulate():
    cfg = BacktestConfig(submit_latency_ns=10_000_000)
    buy30 = QuoteIntent(side="yes", price=D("0.40"), size=D("30"))
    records = [
        (T0, snap()),
        (T0 + 100_000_000, trade("no", "0.40", "0.60", "110.00", 2)),  # eats 100, partial 10
        (T0 + 200_000_000, trade("no", "0.40", "0.60", "25.00", 3)),   # queue 0 -> fill 20
    ]
    res = run_backtest(market(), FixedPolicy(buy30), records, config=cfg)
    assert [f.size for f in res.fills] == [D("10"), D("20")]
    assert res.final_inventory == 30


def test_cancel_removes_order_after_cancel_latency():
    cfg = BacktestConfig(submit_latency_ns=10_000_000, cancel_latency_ns=10_000_000)
    # Policy withdraws the quote at cutoff; a later trade must not fill the cancelled order.
    policy = TimeGatedPolicy(BUY_YES, cutoff_ns=T0 + 100_000_000)
    records = [
        (T0, snap()),
        (T0 + 100_000_000, trade("yes", "0.99", "0.01", "1.00", 2)),  # triggers a requote -> cancel
        (T0 + 300_000_000, trade("no", "0.40", "0.60", "500.00", 3)),  # after cancel effective
    ]
    res = run_backtest(market(), policy, records, config=cfg)
    assert res.fill_count == 0


def test_trade_through_mode_requires_through_trade():
    cfg = BacktestConfig(submit_latency_ns=10_000_000, fill_mode=FillMode.TRADE_THROUGH)
    records = [
        (T0, snap()),
        (T0 + 100_000_000, trade("no", "0.40", "0.60", "500.00", 2)),  # AT our price -> no fill
        (T0 + 200_000_000, trade("no", "0.30", "0.70", "1.00", 3)),    # THROUGH (worse) -> fill all
    ]
    res = run_backtest(market(), FixedPolicy(BUY_YES), records, config=cfg)
    assert res.fill_count == 1 and res.fills[0].size == D("10")
    assert res.fills[0].ts_ns == T0 + 200_000_000


def test_frictionless_mode_captures_at_level_trade_ignoring_queue():
    # FRICTIONLESS fills the passive side of an at-level trade regardless of queue ahead.
    cfg = BacktestConfig(submit_latency_ns=10_000_000, fill_mode=FillMode.FRICTIONLESS)
    records = [
        (T0, snap()),  # yes@0.40 has queue 100 ahead
        (T0 + 100_000_000, trade("no", "0.40", "0.60", "3.00", 2)),  # only 3 lots trade
    ]
    res = run_backtest(market(), FixedPolicy(BUY_YES), records, config=cfg)
    # QUEUE mode would NOT fill (3 < queue 100); FRICTIONLESS fills min(3,10)=3.
    assert res.fill_count == 1 and res.fills[0].size == D("3")


def test_queue_mode_same_trade_does_not_fill_behind_queue():
    cfg = BacktestConfig(submit_latency_ns=10_000_000, fill_mode=FillMode.QUEUE)
    records = [
        (T0, snap()),
        (T0 + 100_000_000, trade("no", "0.40", "0.60", "3.00", 2)),
    ]
    res = run_backtest(market(), FixedPolicy(BUY_YES), records, config=cfg)
    assert res.fill_count == 0  # 3 < queue 100


def test_queue_mode_through_trade_also_fills():
    cfg = BacktestConfig(submit_latency_ns=10_000_000, fill_mode=FillMode.QUEUE)
    records = [
        (T0, snap()),
        (T0 + 200_000_000, trade("no", "0.30", "0.70", "1.00", 2)),  # through our 0.40 level
    ]
    res = run_backtest(market(), FixedPolicy(BUY_YES), records, config=cfg)
    assert res.fill_count == 1 and res.fills[0].size == D("10")


def test_settlement_pnl_and_markout():
    cfg = BacktestConfig(submit_latency_ns=10_000_000)
    records = [
        (T0, snap()),
        (T0 + 100_000_000, trade("no", "0.40", "0.60", "120.00", 2)),
    ]
    res = run_backtest(market(result_yes=True), FixedPolicy(BUY_YES), records, config=cfg)
    # YES buy at 0.40, resolves YES -> payoff 1 -> per-contract 0.60, total 6.00 (maker-free).
    assert net_settlement_pnl(res) == D("6.00")
    (mo,) = markouts(res)
    assert mo.settle_pnl_per_contract == D("0.60")
    # mid at snapshot = (0.40 + (1-0.55))/2 = 0.425; markout_5s = 0.425 - 0.40 = 0.025.
    assert mo.markout_5s == D("0.025")


def _frame_for(ticker, frame):
    f = dict(frame)
    f["msg"] = {**frame["msg"], "market_ticker": ticker}
    return f


def test_cross_ticker_events_never_fill_our_order():
    # SEV-1: the multi-ticker stream shares the per-sid seq AND price levels. A foreign market's
    # trade/delta at our exact (side, price) must NOT fill or advance our order.
    other = "KXBTC15M-OTHER"
    cfg = BacktestConfig(submit_latency_ns=10_000_000)
    records = [
        (T0, snap()),  # our snapshot: yes@0.40 size 100
        (T0 + 5_000_000, _frame_for(other, snap(seq=2))),  # foreign snapshot, same levels
        # Foreign trades that WOULD sweep + trade-through our 0.40 level if mis-attributed:
        (T0 + 100_000_000, _frame_for(other, trade("no", "0.40", "0.60", "5000.00", 3))),
        (T0 + 150_000_000, _frame_for(other, trade("no", "0.30", "0.70", "5000.00", 4))),
    ]
    res = run_backtest(market(), FixedPolicy(BUY_YES), records, config=cfg)
    assert res.fill_count == 0
    assert res.final_inventory == 0


def test_our_ticker_still_fills_amid_foreign_traffic():
    # The same sweep on OUR ticker fills; interleaved foreign traffic does not block it.
    other = "KXBTC15M-OTHER"
    cfg = BacktestConfig(submit_latency_ns=10_000_000)
    records = [
        (T0, snap()),
        (T0 + 5_000_000, _frame_for(other, snap(seq=2))),
        (T0 + 90_000_000, _frame_for(other, trade("no", "0.40", "0.60", "9000.00", 3))),  # foreign
        (T0 + 100_000_000, trade("no", "0.40", "0.60", "120.00", 4)),  # OURS -> fills 10
    ]
    res = run_backtest(market(), FixedPolicy(BUY_YES), records, config=cfg)
    assert res.fill_count == 1 and res.final_inventory == 10


def test_no_side_fill_shorts_yes_and_settles():
    cfg = BacktestConfig(submit_latency_ns=10_000_000)
    buy_no = QuoteIntent(side="no", price=D("0.55"), size=D("4"))
    records = [
        (T0, snap()),
        # taker buys YES -> lifts resting NO at no_price 0.55; need > queue(50) to fill.
        (T0 + 100_000_000, trade("yes", "0.45", "0.55", "60.00", 2)),
    ]
    res = run_backtest(market(result_yes=False), FixedPolicy(buy_no), records, config=cfg)
    assert res.fill_count == 1 and res.final_inventory == -4  # long NO = short YES
    # NO buy at 0.55, resolves NO -> payoff 1 -> per-contract 0.45, total 1.80.
    assert net_settlement_pnl(res) == D("1.80")
