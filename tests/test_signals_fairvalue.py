"""Tests for the KXBTC15M digital fair value (ROADMAP 3.2).

Covers: window-state regimes (pre-window / inside-window / at-close), the >=/ties->YES rule,
monotonicity in (Y_t - strike), the tau->0 limit, and Monte-Carlo calibration against the known
true probability (which validates the settlement-averaging variance formula, not just the Gaussian
CDF)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from kbtc.pricing import DEFAULT_WINDOW, SettlementWindow
from kbtc.signals.fairvalue import (
    CloseWindowState,
    IndexSeries,
    close_window_state,
    expected_n_observed,
    fair_value_up,
)

NS = 1_000_000_000
SIG = 0.60 / math.sqrt(365.0 * 24 * 3600)  # 60% annualized -> per-sqrt-second


# ----------------------------------------------------------------- window-state bookkeeping
def test_expected_n_observed_regimes():
    w = DEFAULT_WINDOW  # 60 x 1 s
    assert expected_n_observed(900.0, w) == 0      # pre-window (tau >> 60)
    assert expected_n_observed(60.0, w) == 0       # exactly at window open: nothing observed yet
    assert expected_n_observed(59.5, w) == 0       # offset-59 print (at close-59) still 0.5 s away
    assert expected_n_observed(59.0, w) == 1       # offset-59 print now exactly observed (o >= tau)
    assert expected_n_observed(30.0, w) == 30      # halfway through the window
    assert expected_n_observed(0.5, w) == 59       # only the at-close print still outstanding
    assert expected_n_observed(0.0, w) == 60       # at close: all 60 observed


def test_close_window_state_consistent_with_expected():
    w = DEFAULT_WINDOW
    close = 900 * NS
    # constant index at 82000 -> observed_log_sum = n_observed * ln(82000)
    def lp_at(ts: int) -> float:
        return math.log(82000.0)
    for tau in (900.0, 60.0, 45.0, 30.0, 1.0, 0.0):
        t = close - int(round(tau * NS))
        st = close_window_state(lp_at, close, t, w)
        assert st.n_observed == expected_n_observed(tau, w)
        assert st.observed_log_sum == pytest.approx(st.n_observed * math.log(82000.0))


def test_close_window_state_raises_on_gap():
    close = 900 * NS
    def lp_gap(ts: int) -> float:
        return float("nan")
    # tau=30 -> 30 observed prints required; sampler gaps -> cannot price
    with pytest.raises(ValueError):
        close_window_state(lp_gap, close, close - 30 * NS, DEFAULT_WINDOW)


# ----------------------------------------------------------------- fair_value_up guards & regimes
def test_fair_value_requires_state_inside_window():
    # tau inside the window with no state is a hard error (not silently priced as un-formed)
    with pytest.raises(ValueError):
        fair_value_up(82000.0, 82000.0, 30.0, SIG)


def test_fair_value_rejects_inconsistent_state():
    bad = CloseWindowState(observed_log_sum=math.log(82000.0), n_observed=1)
    with pytest.raises(ValueError):
        fair_value_up(82000.0, 82000.0, 900.0, SIG, state=bad)  # expected 0 observed


def test_fair_value_positive_inputs():
    with pytest.raises(ValueError):
        fair_value_up(-1.0, 82000.0, 900.0, SIG)
    with pytest.raises(ValueError):
        fair_value_up(82000.0, 0.0, 900.0, SIG)


def test_at_the_money_is_half_pre_window():
    dv = fair_value_up(82000.0, 82000.0, 600.0, SIG)
    assert dv.prob_up == pytest.approx(0.5, abs=1e-9)
    assert dv.z == pytest.approx(0.0, abs=1e-9)


# ----------------------------------------------------------------- >= / ties -> YES, tau->0
def test_tau_zero_tie_resolves_yes():
    # at close, all 60 observed, average exactly == strike -> degenerate var=0 -> YES (prob 1)
    w = DEFAULT_WINDOW
    st = CloseWindowState(observed_log_sum=w.n_samples * math.log(82000.0), n_observed=w.n_samples)
    dv = fair_value_up(82000.0, 82000.0, 0.0, SIG, state=st)
    assert dv.prob_up == 1.0
    assert dv.z == math.inf


def test_tau_zero_below_strike_is_no():
    w = DEFAULT_WINDOW
    st = CloseWindowState(observed_log_sum=w.n_samples * math.log(81999.0), n_observed=w.n_samples)
    dv = fair_value_up(82000.0, 82000.0, 0.0, SIG, state=st)
    assert dv.prob_up == 0.0
    assert dv.z == -math.inf


def test_tau_limit_sharpens_to_step():
    # as tau shrinks (pre-window, same small positive moneyness), P(up) moves AWAY from 0.5
    # toward 1 (variance compresses, the in-the-money gap dominates)
    spot, strike = 82050.0, 82000.0
    ps = [fair_value_up(spot, strike, tau, SIG).prob_up for tau in (900.0, 300.0, 120.0, 61.0)]
    assert all(p > 0.5 for p in ps)
    assert ps == sorted(ps)  # monotonically sharper toward YES as tau falls


# ----------------------------------------------------------------- monotonicity in (Y_t - strike)
def test_monotone_in_spot_minus_strike():
    strike = 82000.0
    spots = np.linspace(81000.0, 83000.0, 41)
    ps = [fair_value_up(s, strike, 300.0, SIG).prob_up for s in spots]
    assert all(b >= a - 1e-12 for a, b in zip(ps, ps[1:]))  # non-decreasing in spot
    assert ps[0] < 0.1 and ps[-1] > 0.9


def test_delta_peaks_at_the_money():
    strike = 82000.0
    d_atm = fair_value_up(82000.0, strike, 300.0, SIG).delta_x
    d_otm = fair_value_up(83000.0, strike, 300.0, SIG).delta_x
    d_itm = fair_value_up(81000.0, strike, 300.0, SIG).delta_x
    assert d_atm > d_otm and d_atm > d_itm  # ATM-peaked delta (binary inventory risk)


# ----------------------------------------------------------------- Monte-Carlo calibration
def _mc_prob_up(spot, strike, tau_s, sigma, n_observed, observed_value, window, n_paths, seed):
    """Empirical P(mean-of-log-prices >= ln strike) by simulating the Brownian settlement average.

    A_log = (observed_log_sum + sum_k [ln spot + sigma W(u_k)]) / n, with observed prints fixed at
    ``observed_value``. This is exactly the quantity settlement_moments models, so the empirical
    frequency must match fair_value_up.prob_up (validates the min(u_k,u_l) variance sum)."""
    rng = np.random.default_rng(seed)
    offsets = window.offsets_before_expiry()
    u = sorted(tau_s - o for o in offsets if o < tau_s)  # future sample times, ascending
    u = np.asarray(u)
    m = u.size
    n = window.n_samples
    obs_sum = n_observed * math.log(observed_value)
    log_spot = math.log(spot)
    # Brownian at the (ascending) future times: W(u_k) via cumulative independent increments.
    du = np.diff(np.concatenate(([0.0], u)))
    incr = rng.standard_normal(size=(n_paths, m)) * np.sqrt(du)[None, :]
    w_paths = np.cumsum(incr, axis=1)              # W(u_k) per path, (n_paths, m)
    x_future = log_spot + sigma * w_paths          # future log-prices
    a_log = (obs_sum + x_future.sum(axis=1)) / n   # mean of all n log-prices
    return float(np.mean(a_log >= math.log(strike)))


@pytest.mark.parametrize("tau,n_obs", [(900.0, 0), (300.0, 0), (45.0, 15), (30.0, 30)])
def test_mc_calibration_matches_model(tau, n_obs):
    spot, strike, obs = 82000.0, 82120.0, 82000.0
    w = DEFAULT_WINDOW
    st = CloseWindowState(observed_log_sum=n_obs * math.log(obs), n_observed=n_obs)
    dv = fair_value_up(spot, strike, tau, SIG, state=st if n_obs else None)
    mc = _mc_prob_up(spot, strike, tau, SIG, n_obs, obs, w, n_paths=400_000, seed=7)
    assert dv.prob_up == pytest.approx(mc, abs=0.01)


def test_mc_calibration_short_window_custom():
    # a non-default window (10 x 1 s) also calibrates -> the formula is window-generic
    w = SettlementWindow(n_samples=10, spacing_s=1.0)
    spot, strike = 82000.0, 82040.0
    dv = fair_value_up(spot, strike, 20.0, SIG, window=w)
    mc = _mc_prob_up(spot, strike, 20.0, SIG, 0, spot, w, n_paths=400_000, seed=11)
    assert dv.prob_up == pytest.approx(mc, abs=0.012)


# ----------------------------------------------------------------- explicit drift hook (3.3)
def test_positive_drift_raises_prob_up():
    base = fair_value_up(82000.0, 82000.0, 600.0, SIG).prob_up
    up = fair_value_up(82000.0, 82000.0, 600.0, SIG, drift_per_s=1e-6).prob_up
    assert up > base  # a positive drift pushes P(up) above the martingale value
    assert base == pytest.approx(0.5, abs=1e-9)


# ----------------------------------------------------------------- IndexSeries causal sampler
def test_index_series_causal_locf_and_staleness():
    ts = np.array([0, 10 * NS, 20 * NS], dtype=np.int64)
    px = np.array([100.0, 101.0, 102.0])
    idx = IndexSeries(ts, px, max_stale_s=5.0)
    assert idx.price_at(10 * NS) == 101.0          # exact
    assert idx.price_at(12 * NS) == 101.0          # LOCF within staleness
    assert math.isnan(idx.price_at(18 * NS))       # 8 s stale > 5 s cap -> NaN
    assert math.isnan(idx.price_at(-1))            # before any data
    assert idx.log_price_at(20 * NS) == pytest.approx(math.log(102.0))


def test_index_series_rejects_unsorted():
    with pytest.raises(ValueError):
        IndexSeries(np.array([10, 5], dtype=np.int64), np.array([1.0, 2.0]))
