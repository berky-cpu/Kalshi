"""Tests for the RestAtTouchPolicy pull rules (kbtc.backtest.policy)."""
from decimal import Decimal
from statistics import NormalDist

from kbtc.backtest.policy import MarketState, RestAtTouchPolicy

D = Decimal


def state(tau_s, prob_up):
    return MarketState(
        recv_ts_ns=0, ticker="T", tau_s=tau_s,
        best_yes_price=D("0.40"), best_yes_size=D("100"),
        best_no_price=D("0.55"), best_no_size=D("50"),
        index_level=81000.0, fair_prob_up=prob_up, delta_x=0.0, sigma=1e-5, inventory=0,
    )


def test_quotes_both_sides_at_touch_when_safe():
    p = RestAtTouchPolicy(size=D(1))
    q = p.quotes(state(tau_s=600.0, prob_up=0.5))  # far from close
    sides = {(i.side, i.price) for i in q}
    assert ("yes", D("0.40")) in sides and ("no", D("0.55")) in sides


def test_pulls_near_strike_in_endgame():
    p = RestAtTouchPolicy(size=D(1), pull_tau_s=180.0, pull_z=1.0)
    # tau < 180 and |z| < 1 (prob 0.5 -> z 0) -> pull everything.
    assert p.quotes(state(tau_s=120.0, prob_up=0.5)) == []


def test_quotes_in_endgame_when_far_from_strike():
    p = RestAtTouchPolicy(size=D(1), pull_tau_s=180.0, pull_z=1.0)
    prob = NormalDist().cdf(2.0)  # |z| = 2 > 1 -> safe even near close
    assert len(p.quotes(state(tau_s=120.0, prob_up=prob))) == 2


def test_conservative_pull_when_fair_value_unavailable_near_close():
    p = RestAtTouchPolicy(size=D(1))
    assert p.quotes(state(tau_s=120.0, prob_up=None)) == []  # cannot confirm safe -> pull
    # but far from close with no FV we still quote (endgame rule only bites inside pull_tau_s)
    assert len(p.quotes(state(tau_s=600.0, prob_up=None))) == 2
