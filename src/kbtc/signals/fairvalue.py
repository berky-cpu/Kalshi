"""KXBTC15M digital fair value: P(up) = P(A_close >= strike | F_t)  (ROADMAP 3.2).

This module wires the verified contract mechanics (ADR-001, docs/facts.md) and the
settlement-averaging variance compression (docs/derivations/settlement-averaging.md,
implemented in ``kbtc.pricing``) into a single causal fair-value function, plus the
window-state bookkeeping that the function needs inside the closing 60 s window.

Contract recap (VERIFIED 2026-10-16, ADR-001). KXBTC15M is an UP/DOWN binary. It resolves
**YES iff the close-window 60 s BRTI average >= the open-window 60 s BRTI average**, with ties
(equality under "at least") -> YES. The "strike" is the open-window average, **fixed at open**:
a realized, past quantity (observable as the active market's ``floor_strike``, == the feed's
``avg_60s_data`` at the open instant per F-19). So for the ~14 min after the opening minute the
payoff is exactly a one-sided Asian digital on the close-window average ``A_close`` vs. the
known constant ``strike`` (derivation addendum §2). The opening 60 s (strike still forming) is
out of scope — both endpoints random, needs a joint model (not built).

**Fair value** at decision time t:  P(up) = P(A_close >= strike | F_t) = Phi(z),
with z, E[A_close], Var[A_close] from ``pricing.settlement_moments`` given:
  * the current index level Y_t  (from the published BRTI feed where available, else the replica),
  * the realized ``strike`` (floor_strike / open-window average),
  * time-to-close ``tau_s`` and the window sampling grid,
  * the **forming-close-window state**: how many of the 60 close-window prints are already
    observed and their partial log-sum (0 before the window opens),
  * ``sigma`` from the 3.1 realized-vol estimator (``kbtc.signals.vol``), per sqrt(second).

**Drift assumption (stated explicitly).** We price under a zero-drift (martingale) index:
``dX = sigma dW``, no drift term. Over a 15-minute contract the risk-neutral drift of a near-
driftless, no-carry index is ~1e-7 in log terms (derivation §2) — negligible vs. the diffusion
term and vs. replica/strike error. A non-zero short-horizon drift (perp lead / order-flow
imbalance) is a **3.3 signal**, deliberately OUT OF SCOPE here: if 3.3 finds a predictive drift
``mu`` it enters as ``E[A_close] += mu * (future-sample-time weighting)`` and must be justified by
incremental Brier on walk-forward splits, not assumed. This module exposes ``drift_per_s=0.0`` as
an explicit, documented default so the assumption is visible, not hidden.

Floats throughout (probability math; no money crosses this layer — CLAUDE.md allows floats for
the probability math, Decimal only at the price/fee boundary which lives in fees.py / the quoter).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from kbtc.pricing import (
    DEFAULT_WINDOW,
    DigitalValue,
    SettlementWindow,
    n_future_samples,
    settlement_moments,
)

NS_PER_S = 1_000_000_000


# =========================================================================================
# Window-state bookkeeping.
# =========================================================================================
def expected_n_observed(tau_s: float, window: SettlementWindow = DEFAULT_WINDOW) -> int:
    """How many of the ``n`` close-window prints are ALREADY observed at time-to-close ``tau_s``.

    A sample at offset ``o`` seconds before close is observed iff its sample time ``T - o <= t``,
    i.e. ``o >= tau_s``; future iff ``o < tau_s``. This is exactly the complement of
    ``pricing.n_future_samples`` — kept consistent so the count fed to ``settlement_moments``
    always satisfies its ``n_observed == n - m`` invariant.
    """
    return window.n_samples - n_future_samples(tau_s, window)


@dataclass(frozen=True)
class CloseWindowState:
    """Observed state of the forming close window at decision time t.

    ``observed_log_sum`` is the sum of ``ln(price)`` over the close-window prints already seen;
    ``n_observed`` is their count. Before the window opens both are 0. These are exactly the two
    numbers ``pricing.settlement_moments`` / ``digital_up`` consume (``observed_log_sum``,
    ``n_observed``).
    """

    observed_log_sum: float
    n_observed: int

    @classmethod
    def pre_window(cls) -> "CloseWindowState":
        """The state before any close-window print has landed (tau_s >= window length)."""
        return cls(observed_log_sum=0.0, n_observed=0)


def close_window_state(
    log_price_at: Callable[[int], float],
    close_time_ns: int,
    t_ns: int,
    window: SettlementWindow = DEFAULT_WINDOW,
) -> CloseWindowState:
    """Build the :class:`CloseWindowState` at ``t_ns`` from a causal index log-price sampler.

    ``log_price_at(ts_ns)`` must return the natural log of the index level as of ``ts_ns`` using
    only information available at ``ts_ns`` (last-observation-carried-forward; ``NaN`` if truly no
    prior data). The observed close-window samples are those with offset ``o >= tau_s`` (sample
    time ``<= t_ns``) — the OLDEST ``expected_n_observed`` offsets — so the returned ``n_observed``
    satisfies ``settlement_moments``'s consistency invariant by construction.

    Raises ``ValueError`` if an observed sample cannot be formed (sampler returns ``NaN`` where a
    print is required) — a genuine index gap inside the forming window; the caller (harness) skips.
    """
    tau_s = (close_time_ns - t_ns) / NS_PER_S
    offsets = window.offsets_before_expiry()  # oldest first (largest offset first)
    observed_offsets = [o for o in offsets if o >= tau_s]
    total = 0.0
    for o in observed_offsets:
        ts = close_time_ns - int(round(o * NS_PER_S))
        lp = log_price_at(ts)
        if lp is None or not math.isfinite(lp):
            raise ValueError(
                f"index gap inside forming close window at offset {o}s (t_ns={t_ns}); cannot price"
            )
        total += lp
    return CloseWindowState(observed_log_sum=total, n_observed=len(observed_offsets))


# =========================================================================================
# Fair value.
# =========================================================================================
def fair_value_up(
    index_level: float,
    strike: float,
    tau_s: float,
    sigma: float,
    *,
    state: CloseWindowState | None = None,
    window: SettlementWindow = DEFAULT_WINDOW,
    drift_per_s: float = 0.0,
) -> DigitalValue:
    """P(A_close >= strike | F_t) and its sensitivity, for KXBTC15M.

    Parameters
    ----------
    index_level : Y_t, the current BRTI level (feed ``cfbenchmarks_value`` where available, else
        the replica). Must be > 0.
    strike : the realized open-window 60 s average (``floor_strike`` / ``avg_60s_data`` at open).
        A known past constant for the priced regime (derivation §2). Must be > 0.
    tau_s : seconds to close (``T - t``).
    sigma : per-sqrt-second log-vol from ``kbtc.signals.vol`` (same units pricing consumes).
    state : the forming-close-window observations. ``None`` is only valid BEFORE the window opens
        (``tau_s`` >= window length, i.e. ``expected_n_observed == 0``); inside the window the
        caller MUST pass the observed state (use :func:`close_window_state`). This prevents the
        silent error of pricing inside the window as if nothing were realized.
    drift_per_s : log-drift per second. **Default 0.0 (martingale)** — see the module docstring;
        a non-zero value is a 3.3 concern and is applied as ``E[A_close] += drift_per_s * (mean
        future sample time)``, left here only so the assumption is explicit.

    Regimes (both handled):
      * ``tau_s`` >= window length: all ``n`` samples future, ``n_observed == 0`` — the plain
        Asian-digital of the derivation §1-§4.
      * ``tau_s`` inside the window: part realized (``state``), part future — ``settlement_moments``
        combines the observed partial sum with the conditional law of the remaining prints.
      * ``tau_s == 0`` (close) / ``var == 0`` (fully realized): degenerate; the verified ">=" tie
        rule assigns exact equality to YES (``pricing.digital_up``'s ``var <= 0`` branch).

    The ">=" / ties->YES rule is inherited from ``pricing.digital_up`` (continuous mass at a point
    is 0, so Phi(z) is unchanged except at the degenerate at-expiry equality, which resolves YES).
    """
    if index_level <= 0 or strike <= 0:
        raise ValueError("index_level and strike must be positive")
    exp_obs = expected_n_observed(tau_s, window)
    if state is None:
        if exp_obs != 0:
            raise ValueError(
                f"tau_s={tau_s}s is inside the close window (expected {exp_obs} observed prints); "
                "pass the forming-window state (see close_window_state)"
            )
        state = CloseWindowState.pre_window()
    elif state.n_observed != exp_obs:
        raise ValueError(
            f"state.n_observed={state.n_observed} inconsistent with tau_s={tau_s}s "
            f"(expected {exp_obs})"
        )

    observed_log_sum = state.observed_log_sum
    if drift_per_s != 0.0:
        # Explicit, documented, OFF by default. settlement_moments models the FUTURE samples as
        # X_t + sigma W_u (zero drift); a non-zero drift shifts each future sample's mean by
        # drift_per_s * u. We fold that into E[A] by adding the drift contribution of the future
        # samples to observed_log_sum-equivalent mean. Because settlement_moments puts weight m/n on
        # X_t, we add drift_per_s * (sum of future sample times) / n via an effective spot bump is
        # NOT exact; instead price via a direct moments adjustment below.
        return _fair_value_with_drift(
            index_level, strike, tau_s, sigma, observed_log_sum, state.n_observed, window, drift_per_s
        )

    from kbtc.pricing import digital_up

    return digital_up(
        spot=index_level,
        strike=strike,
        tau_s=tau_s,
        sigma=sigma,
        observed_log_sum=observed_log_sum,
        n_observed=state.n_observed,
        window=window,
    )


def _fair_value_with_drift(
    index_level: float,
    strike: float,
    tau_s: float,
    sigma: float,
    observed_log_sum: float,
    n_observed: int,
    window: SettlementWindow,
    drift_per_s: float,
) -> DigitalValue:
    """Fair value with an explicit constant log-drift (3.3 hook; off by default).

    E[A] gains ``drift_per_s * (mean future-sample time) * (m/n)`` relative to the martingale case:
    each future sample k has mean ``X_t + drift_per_s * u_k`` so E[A] += (drift/n) * sum_k u_k.
    Variance is unchanged (drift is deterministic).
    """
    log_spot = math.log(index_level)
    mom = settlement_moments(log_spot, tau_s, sigma, observed_log_sum, n_observed, window)
    future_times = sorted(tau_s - o for o in window.offsets_before_expiry() if o < tau_s)
    drift_shift = drift_per_s * sum(future_times) / window.n_samples
    mean = mom.mean + drift_shift
    gap = mean - math.log(strike)
    if mom.var <= 0.0:
        p = 1.0 if gap >= 0 else 0.0
        return DigitalValue(prob_up=p, delta_x=0.0,
                            z=math.copysign(math.inf, 1.0 if gap >= 0 else -1.0))
    sd = math.sqrt(mom.var)
    z = gap / sd
    phi = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    return DigitalValue(prob_up=0.5 * (1.0 + math.erf(z / math.sqrt(2.0))),
                        delta_x=phi * mom.weight_on_spot / sd, z=z)


# =========================================================================================
# Causal index-level sampler (convenience for the harness; LOCF as-of a time).
# =========================================================================================
@dataclass
class IndexSeries:
    """A causal as-of index series: ``price_at(t)`` / ``log_price_at(t)`` = last print <= t.

    ``ts_ns`` must be non-decreasing. ``max_stale_s`` (None = unbounded) drops a carried value
    that is older than the cap (a feed gap) to ``NaN`` so it is never carried stale across a hole.
    Built from the BRTI replica (1 Hz reconstruction) for the backtest, or from the published
    ``cfbenchmarks_value`` feed once >1 day of it exists.
    """

    ts_ns: np.ndarray
    prices: np.ndarray
    max_stale_s: float | None = None

    def __post_init__(self) -> None:
        self.ts_ns = np.asarray(self.ts_ns, dtype=np.int64)
        self.prices = np.asarray(self.prices, dtype=np.float64)
        if self.ts_ns.shape != self.prices.shape:
            raise ValueError("ts_ns and prices must have the same shape")
        if self.ts_ns.size and np.any(np.diff(self.ts_ns) < 0):
            raise ValueError("ts_ns must be non-decreasing")

    def price_at(self, t_ns: int) -> float:
        if self.ts_ns.size == 0:
            return float("nan")
        idx = int(np.searchsorted(self.ts_ns, t_ns, side="right")) - 1
        if idx < 0:
            return float("nan")
        if self.max_stale_s is not None and (t_ns - int(self.ts_ns[idx])) > self.max_stale_s * NS_PER_S:
            return float("nan")
        return float(self.prices[idx])

    def log_price_at(self, t_ns: int) -> float:
        p = self.price_at(t_ns)
        return math.log(p) if (math.isfinite(p) and p > 0) else float("nan")
