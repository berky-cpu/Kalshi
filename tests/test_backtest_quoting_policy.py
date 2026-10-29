"""Smoke test: the real kbtc.quoting policy drives the engine via a causal fair-value provider.

This exercises the policy<->engine seam (state -> intents -> submits -> fills) end to end; it is not
a research result. Fair value is computed from the index level with pricing.digital_up so the path
mirrors how 4.2 will wire the replica + vol + fair value.
"""
from decimal import Decimal

from kbtc.backtest.engine import BacktestConfig, BacktestMarket, run_backtest
from kbtc.backtest.events import IndexTick
from kbtc.backtest.policy import MarketState, QuotingPolicy
from kbtc.pricing import digital_up, sigma_per_sqrt_second

D = Decimal
TICKER = "KXBTC15M-TEST"
T0 = 1_000_000_000
NS = 1_000_000_000
SIG = sigma_per_sqrt_second(0.50)


def snap(seq=1):
    return {"type": "orderbook_snapshot", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER,
                    "yes_dollars_fp": [["0.40", "100.00"], ["0.45", "80.00"]],
                    "no_dollars_fp": [["0.52", "90.00"], ["0.55", "50.00"]]}}


def trade(taker_side, yes_price, no_price, count, seq):
    return {"type": "trade", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TICKER, "taker_side": taker_side,
                    "yes_price_dollars": yes_price, "no_price_dollars": no_price,
                    "count_fp": count, "ts_ms": 0}}


def fair_value_fn(state: MarketState):
    if state.index_level is None:
        return (None, None, None)
    v = digital_up(state.index_level, 81_000.0, max(state.tau_s, 1.0), SIG)
    return (v.prob_up, v.delta_x, SIG)


def test_quoting_policy_quotes_and_fills_through_engine():
    market = BacktestMarket(ticker=TICKER, open_time_ns=T0 - 450 * NS,
                            close_time_ns=T0 + 450 * NS, strike=D("81000"), result_yes=True)
    cfg = BacktestConfig(submit_latency_ns=10_000_000)
    policy = QuotingPolicy(size=D("5"), use_endgame_pull=False)
    index = [IndexTick(T0 + 1, 81_050.0)]  # above strike -> fair prob > 0.5, quotes a YES bid
    records = [
        (T0, snap()),
        # A big NO-aggressor trade sweeps resting YES liquidity -> can reach our bid level.
        (T0 + 100_000_000, trade("no", "0.40", "0.60", "5000.00", 2)),
        (T0 + 150_000_000, trade("no", "0.30", "0.70", "5000.00", 3)),  # trade-through our bid
    ]
    res = run_backtest(market, policy, records, index_ticks=index, config=cfg,
                       fair_value_fn=fair_value_fn)
    # The policy produced resting quotes and at least one filled on the sweep/through.
    assert res.fill_count >= 1
    assert all(f.fee == D("0.00") for f in res.fills)  # maker-free
