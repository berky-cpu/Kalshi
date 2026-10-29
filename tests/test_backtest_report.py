"""Tests for the aggregation/reporting layer (kbtc.backtest.report)."""
from dataclasses import dataclass
from decimal import Decimal

from kbtc.backtest.engine import BacktestConfig, BacktestMarket, run_backtest
from kbtc.backtest.policy import MarketState, QuoteIntent
from kbtc.backtest.queue import CancelPolicy, FillMode
from kbtc.backtest.report import (
    MarketOutcome,
    StudyReport,
    bootstrap_pnl_ci,
    day_block_bootstrap_mean,
    daily_pnl_totals,
    sensitivity_table,
    summarize_market,
)

D = Decimal
TICKER = "KXBTC15M-TEST"
T0 = 1_000_000_000
NS = 1_000_000_000


def snap(seq=1):
    return {"type": "orderbook_snapshot", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER,
                    "yes_dollars_fp": [["0.40", "100.00"]],
                    "no_dollars_fp": [["0.55", "50.00"]]}}


def trade(count, seq):
    return {"type": "trade", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER, "taker_side": "no",
                    "yes_price_dollars": "0.40", "no_price_dollars": "0.60",
                    "count_fp": count, "ts_ms": 0}}


@dataclass
class FixedPolicy:
    def quotes(self, state: MarketState):
        return [QuoteIntent(side="yes", price=D("0.40"), size=D("10"))]


def mk(result_yes=True):
    return BacktestMarket(ticker=TICKER, open_time_ns=T0 - 900 * NS, close_time_ns=T0 + 900 * NS,
                          result_yes=result_yes)


def _records():
    return [(T0, snap()), (T0 + 100_000_000, trade("120.00", 2))]


def test_summarize_and_study_aggregates():
    report = StudyReport()
    res1 = run_backtest(mk(True), FixedPolicy(), _records(), config=BacktestConfig(submit_latency_ns=10_000_000))
    res2 = run_backtest(mk(False), FixedPolicy(), _records(), config=BacktestConfig(submit_latency_ns=10_000_000))
    report.outcomes.append(summarize_market(res1, "2026-10-20"))
    report.outcomes.append(summarize_market(res2, "2026-10-21"))
    assert report.n_markets == 2
    assert report.total_fills == 2
    # YES fill 0.40 resolves YES -> +6.00; resolves NO -> -4.00. Total +2.00.
    assert abs(report.total_net_pnl() - 2.0) < 1e-9
    pd = report.per_day_pnl()
    assert abs(pd["2026-10-20"] - 6.0) < 1e-9 and abs(pd["2026-10-21"] + 4.0) < 1e-9
    assert report.inventory_distribution() == {10: 2}


def _multiday_outcomes():
    # 8 days, 2 markets each; per-day totals = 0,2,4,...,14 (mean daily 7, total 56).
    out = []
    for d in range(8):
        day = f"2026-10-{20 + d:02d}"
        out.append(MarketOutcome(f"{day}-a", day, 1, float(d), 0.0, 0, None, None))
        out.append(MarketOutcome(f"{day}-b", day, 1, float(d), 0.0, 0, None, None))
    return out


def test_daily_pnl_totals_sum_markets_within_day():
    totals = dict(daily_pnl_totals(_multiday_outcomes()))
    assert abs(totals["2026-10-20"] - 0.0) < 1e-9
    assert abs(totals["2026-10-27"] - 14.0) < 1e-9  # day 7: 7+7


def test_bootstrap_ci_is_day_level_and_brackets_point():
    outcomes = _multiday_outcomes()
    point, lo, hi = bootstrap_pnl_ci(outcomes, n_boot=2000, seed=1, statistic="total")
    assert abs(point - 56.0) < 1e-9  # sum of daily totals 0..14
    assert lo <= point <= hi
    mpoint, mlo, mhi = bootstrap_pnl_ci(outcomes, n_boot=2000, seed=1, statistic="mean")
    assert abs(mpoint - 7.0) < 1e-9  # mean DAILY total, not per-market
    assert mlo <= mpoint <= mhi


def test_day_block_bootstrap_mean_pools_and_brackets():
    # 4 days; within-day per-fill values. Pooled mean over all values is the point estimate.
    vbd = {"d1": [1.0, 1.0], "d2": [2.0], "d3": [3.0, 3.0, 3.0], "d4": [0.0]}
    point, n, lo, hi = day_block_bootstrap_mean(vbd, n_boot=2000, seed=3)
    all_vals = [1, 1, 2, 3, 3, 3, 0]
    assert n == len(all_vals)
    assert abs(point - sum(all_vals) / len(all_vals)) < 1e-9
    assert lo <= point <= hi


def test_day_block_bootstrap_empty():
    point, n, lo, hi = day_block_bootstrap_mean({})
    assert n == 0 and point != point  # NaN


def test_bootstrap_handles_no_settled_markets():
    outcomes = [MarketOutcome("m0", "d", 0, None, 0.0, 0, None, None)]
    point, lo, hi = bootstrap_pnl_ci(outcomes)
    assert point != point  # NaN


def test_sensitivity_table_varies_config():
    markets = [(mk(True), "2026-10-20", _records(), [])]
    configs = [
        BacktestConfig(submit_latency_ns=10_000_000, fill_mode=FillMode.QUEUE),
        BacktestConfig(submit_latency_ns=10_000_000, fill_mode=FillMode.TRADE_THROUGH),
    ]
    cells = sensitivity_table(markets, FixedPolicy, configs)
    assert len(cells) == 2
    # QUEUE mode fills (at-level trade) -> +6.00; TRADE_THROUGH needs a through trade -> no fill.
    assert abs(cells[0].total_net_pnl - 6.0) < 1e-9
    assert cells[1].total_fills == 0
