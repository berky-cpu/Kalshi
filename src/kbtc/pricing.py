"""Fair value for KXBTC15M as an Asian digital on the settlement-window average.

Derivation: docs/derivations/settlement-averaging.md
Contract facts: docs/facts.md / docs/decisions/ADR-001-settlement-model.md

Contract (KXBTC15M): an UP/DOWN binary, not a fixed-strike digital. It resolves YES
iff mean(last 60 s of BRTI before CLOSE) >= mean(60 s of BRTI before OPEN), with ties
(equality under the "at least" rule) resolving YES. Settlement uses CF Benchmarks'
Bitcoin Real-Time Index (BRTI), a once-per-second index; the settlement value is the
simple average of the 60 one-second prints in the final minute, rounded to the nearest
$0.01 (VERIFIED 2026-10-16, see docs/facts.md / ADR-001).

Here `strike` is the open-window 60 s average, fixed (realized, in the past) at market
open — so there is no look-ahead. It is known for the ~14 min of trading after the first
60 s, but we observe it only through our BRTI *replica*: the strike we price against is
our estimate of the official open-window average, with error bounded by replica tracking
error (Phase 2), not zero. Limitation: during the opening 60 s the reference is still
forming (both endpoints random); this model assumes the strike is already realized and
prices only the closing-window average against it.

Model: log price X follows dX = sigma dW over the contract's life (drift dropped).
Settlement A = mean of n samples spaced `spacing_s` apart, the last at expiry T.
n=60 is VERIFIED 2026-10-16 ("60 RTI prices are collected"); the 1 s spacing and the
exact window endpoint are INFERRED from "BRTI is a 1 Hz index" (PARTIAL — see
docs/facts.md / ADR-001). The near-expiry variance correction depends on this placement.
Units: tau in seconds, sigma in log-return per sqrt(second).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SECONDS_PER_YEAR = 365.0 * 24 * 3600


@dataclass(frozen=True)
class SettlementWindow:
    n_samples: int = 60  # VERIFIED 2026-10-16, see docs/facts.md / ADR-001
    spacing_s: float = 1.0  # VERIFIED 2026-10-16, see docs/facts.md / ADR-001

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
    """P(settlement average >= strike) and its sensitivity to log spot.

    Uses the verified "at least" (>=) rule: equality resolves YES (ties -> YES).
    The continuous branch below is unchanged because P(A >= K) = P(A > K) for the
    continuous settlement average, so the tie convention has zero fair-value impact
    except at the realized at-expiry equality boundary. In reality exact ties arise
    only from the $0.01 penny-rounding of both averages (not modeled here); the ">="
    rule simply assigns that measure-zero / rounding mass to YES, as verified.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    mom = settlement_moments(math.log(spot), tau_s, sigma, observed_log_sum, n_observed, window)
    gap = mom.mean - math.log(strike)
    if mom.var <= 0.0:
        # Degenerate (settlement fully realized): equality resolves YES under ">=".
        # z is +inf at a tie to stay consistent with prob_up=1.0 (not a 50/50 signal).
        p = 1.0 if gap >= 0 else 0.0
        return DigitalValue(prob_up=p, delta_x=0.0, z=math.copysign(math.inf, 1.0 if gap >= 0 else -1.0))
    sd = math.sqrt(mom.var)
    z = gap / sd
    return DigitalValue(prob_up=_Phi(z), delta_x=_phi(z) * mom.weight_on_spot / sd, z=z)


def naive_variance(tau_s: float, sigma: float) -> float:
    """Point-settlement variance sigma^2 * tau, for comparison only."""
    return sigma * sigma * max(tau_s, 0.0)
