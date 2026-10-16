import math

import pytest

from kbtc.pricing import (
    SettlementWindow,
    digital_up,
    n_future_samples,
    settlement_moments,
    sigma_per_sqrt_second,
)

SIG = sigma_per_sqrt_second(0.50)
FINE = SettlementWindow(n_samples=6000, spacing_s=0.01)  # ~continuous 60 s window
W = 60.0


def test_verified_window_defaults():
    # VERIFIED 2026-10-16 (docs/facts.md / ADR-001): 60 one-second BRTI prints.
    w = SettlementWindow()
    assert w.n_samples == 60
    assert w.spacing_s == 1.0
    assert w.length_s == 60.0


def test_tie_resolves_yes_at_expiry():
    # ">=" rule: expected settlement average EXACTLY equal to strike resolves YES.
    strike = 60_000.0
    n = 60
    exact = n * math.log(strike)  # mean == log(strike) -> gap == 0, var == 0
    at = digital_up(strike, strike, 0.0, SIG, observed_log_sum=exact, n_observed=n)
    assert at.prob_up == 1.0
    # Just below the strike resolves NO.
    below = math.exp(exact / n) * (1 - 1e-6)
    just_below = digital_up(below, strike, 0.0, SIG, observed_log_sum=n * math.log(below), n_observed=n)
    assert just_below.prob_up == 0.0


def test_before_window_matches_continuous_limit():
    tau = 900.0
    mom = settlement_moments(0.0, tau, SIG, window=FINE)
    assert mom.var == pytest.approx(SIG**2 * (tau - 2 * W / 3), rel=1e-3)
    assert mom.weight_on_spot == 1.0


def test_inside_window_matches_cubic_limit():
    tau = 30.0
    m = n_future_samples(tau, FINE)
    mom = settlement_moments(0.0, tau, SIG, observed_log_sum=0.0, n_observed=FINE.n_samples - m, window=FINE)
    assert mom.var == pytest.approx(SIG**2 * tau**3 / (3 * W**2), rel=2e-3)


def test_discrete_60_samples_close_to_continuous():
    mom = settlement_moments(0.0, 900.0, SIG)
    assert mom.var == pytest.approx(SIG**2 * (900 - 40), rel=2e-3)


def test_after_expiry_is_deterministic():
    mom = settlement_moments(math.log(100), 0.0, SIG, observed_log_sum=60 * math.log(101), n_observed=60)
    assert mom.var == 0.0
    assert mom.mean == pytest.approx(math.log(101))
    assert digital_up(100, 100.5, 0.0, SIG, 60 * math.log(101), 60).prob_up == 1.0


def test_inconsistent_observation_count_raises():
    with pytest.raises(ValueError):
        settlement_moments(0.0, 30.0, SIG, n_observed=0)


def test_atm_is_half_and_delta_peaks_atm():
    atm = digital_up(60_000, 60_000, 900, SIG)
    assert atm.prob_up == pytest.approx(0.5, abs=1e-12)
    otm = digital_up(60_000, 60_300, 900, SIG)
    assert otm.prob_up < 0.5
    assert atm.delta_x > otm.delta_x > 0


def test_averaging_makes_late_prices_more_decisive_than_naive():
    # 90 s left, spot 0.1% above strike: averaged variance (50 s) < naive (90 s) -> higher prob
    tau, s, k = 90.0, 60_060.0, 60_000.0
    p = digital_up(s, k, tau, SIG).prob_up
    naive = 0.5 * (1 + math.erf(math.log(s / k) / (SIG * math.sqrt(tau)) / math.sqrt(2)))
    assert p > naive
