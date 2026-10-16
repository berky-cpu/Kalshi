"""Fair value for KXBTC15M as an Asian digital on the settlement-window average.

Derivation: docs/derivations/settlement-averaging.md

Model: log price X follows dX = sigma dW over the contract's life (drift dropped).
Settlement A = mean of n samples spaced `spacing_s` apart, the last at expiry T
(UNVERIFIED: 60 one-second BRTI prints — confirm in ROADMAP 0.1).
Units: tau in seconds, sigma in log-return per sqrt(second).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SECONDS_PER_YEAR = 365.0 * 24 * 3600


@dataclass(frozen=True)
class SettlementWindow:
    n_samples: int = 60  # UNVERIFIED
    spacing_s: float = 1.0  # UNVERIFIED

    @property
    def length_s(self) -> float:
        return self.n_samples * self.spacing_s

    def offsets_before_expiry(self) -> list[float]:
        """Seconds before expiry of each sample, oldest first; the last sample is at expiry."""
        n = self.n_samples
        return [(n - 1 - i) * self.spacing_s for i in range(n)]


DEFAULT_WINDOW = SettlementWindow()


def sigma_per_sqrt_second(annualized_vol: float) -> float:
    return annualized_vol / math.sqrt(SECONDS_PER_YEAR)


def n_future_samples(tau_s: float, window: SettlementWindow = DEFAULT_WINDOW) -> int:
    """Number of settlement samples strictly after now (offset < tau)."""
    return sum(1 for o in window.offsets_before_expiry() if o < tau_s)


@dataclass(frozen=True)
class SettlementMoments:
    mean: float  # E[A] in log price
    var: float  # Var[A]
    weight_on_spot: float  # dE[A]/dX_now = m / n


def settlement_moments(
    log_spot: float,
    tau_s: float,
    sigma: float,
    observed_log_sum: float = 0.0,
    n_observed: int = 0,
    window: SettlementWindow = DEFAULT_WINDOW,
) -> SettlementMoments:
    """Exact discrete mean/variance of the settlement average given what is already observed.

    observed_log_sum: sum of log prices of samples already printed (0 before the window).
    n_observed: how many samples are already printed; must equal n - (future samples).
    """
    if sigma < 0:
        raise ValueError("sigma must be non-negative")
    n = window.n_samples
    future = sorted(tau_s - o for o in window.offsets_before_expiry() if o < tau_s)
    m = len(future)
    if n_observed != n - m:
        raise ValueError(
            f"n_observed={n_observed} inconsistent with tau={tau_s}s: expected {n - m} observed samples"
        )
    mean = (observed_log_sum + m * log_spot) / n
    # sum_{k,l} min(u_k, u_l) for sorted u: each u_k is the min of 2(m-k)+1 ordered pairs (1-indexed k)
    s = sum(u * (2 * (m - k) - 1) for k, u in enumerate(future))  # k is 0-indexed here
    var = sigma * sigma * s / (n * n)
    return SettlementMoments(mean=mean, var=var, weight_on_spot=m / n)


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _Phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class DigitalValue:
    prob_up: float  # fair value of YES in dollars
    delta_x: float  # dP / d(log spot): local inventory sensitivity, peaks at the money
    z: float  # standardized distance of expected settlement above strike


def digital_up(
    spot: float,
    strike: float,
    tau_s: float,
    sigma: float,
    observed_log_sum: float = 0.0,
    n_observed: int = 0,
    window: SettlementWindow = DEFAULT_WINDOW,
) -> DigitalValue:
    """P(settlement average > strike) and its sensitivity to log spot."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    mom = settlement_moments(math.log(spot), tau_s, sigma, observed_log_sum, n_observed, window)
    gap = mom.mean - math.log(strike)
    if mom.var <= 0.0:
        p = 1.0 if gap > 0 else (0.0 if gap < 0 else 0.5)
        return DigitalValue(prob_up=p, delta_x=0.0, z=math.copysign(math.inf, gap) if gap else 0.0)
    sd = math.sqrt(mom.var)
    z = gap / sd
    return DigitalValue(prob_up=_Phi(z), delta_x=_phi(z) * mom.weight_on_spot / sd, z=z)


def naive_variance(tau_s: float, sigma: float) -> float:
    """Point-settlement variance sigma^2 * tau, for comparison only."""
    return sigma * sigma * max(tau_s, 0.0)
