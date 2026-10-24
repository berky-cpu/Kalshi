"""Microstructure-robust short-horizon realized-volatility estimator for the BTC index (3.1).

ROADMAP 3.1. Produces an estimate of the index's short-horizon diffusion coefficient
``sigma`` in **log-return per sqrt(second)** — exactly the ``sigma`` consumed by
``kbtc.pricing.settlement_moments`` / ``kbtc.pricing.digital_up`` (where the point-settlement
variance over ``tau`` seconds is ``sigma**2 * tau`` and the settlement-averaging corrections
``sigma**2 (tau - 2w/3)`` / ``sigma**2 tau**3/(3 w**2)`` apply — see
``docs/derivations/settlement-averaging.md``). The pricing layer needs ``sigma`` per sqrt(second);
we output that directly (``sigma_per_sqrt_second_from_var`` = sqrt(variance-per-second)), so no
annualization round-trip is required. ``annualized_from_var`` is provided only for reporting /
cross-checking against ``pricing.sigma_per_sqrt_second``.

Design (reasoned in notes/experiments/EXP-006):

* **Input** is a generic ``(recv_ts_ns, price)`` series — works on either the replica-reconstructed
  index (~1 Hz, build+eval now) or the published ``cfbenchmarks_value_5hz`` feed (5 Hz, preferred
  going forward). Only ``recv_ts_ns`` (availability time) is ever used, never ``exch_ts_ns``; every
  function is causal (an estimate at ``t`` uses only samples with ``ts <= t``).

* **Microstructure robustness.** Naive sum-of-squared high-frequency returns is biased: additive
  microstructure noise biases realized variance UP (the ``2 n sigma_u**2`` term that grows with the
  sampling frequency), while the BRTI's own depth-weighted-mid smoothing (a low-pass / MA filter)
  biases it DOWN at the finest scale. We handle both with (a) a signature plot + sparse sampling at
  a noise-robust interval and (b) the Two-Scale Realized Volatility estimator (TSRV,
  Zhang-Mykland-Ait-Sahalia 2005), which subtracts the noise bias and is consistent under additive
  noise. The online forecaster is a time-decay EWMA on sparsely-sampled (noise-suppressed) squared
  returns — the "robust EWMA on 1-5 s returns" of the roadmap.

* **Intraday seasonality.** BTC vol has a strong UTC time-of-day pattern (US/EU/Asia sessions). We
  fit a multiplicative per-bin seasonal factor on PAST days only, deseasonalize the squared returns
  before the EWMA (so the stochastic-vol level is estimated on a stationary series), and
  re-seasonalize when reading sigma out at a target time. Causal by construction.

* **Horizon.** The EWMA level is a per-second variance; ``sigma`` maps straight into the
  settlement-averaging formulas at any ``tau`` from 15 min down to seconds. For a forward horizon
  ``H`` the expected variance is ``sum`` of the (seasonally-scaled) per-second variance over
  ``[t, t+H]``; for ``H`` short relative to the diurnal cycle this is ~``sigma**2 * H`` at the
  current seasonal level (``expected_forward_variance`` integrates the seasonal curve when it isn't).

Evaluation (QLIKE, walk-forward by calendar day) lives in the same module (``walk_forward_eval``)
so the acceptance metric ships with the estimator. QLIKE is used because it is robust to a noisy
but conditionally-unbiased variance proxy (Patton 2011); the forward proxy is TSRV (unbiased under
noise). Baselines to beat: trailing-window realized variance and a previous-day-same-time
("random-walk") realized variance.

Floats throughout (vol math; no money crosses this module). Pure functions / frozen configs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

from kbtc.pricing import SettlementWindow, settlement_moments

NS_PER_S = 1_000_000_000
SECONDS_PER_DAY = 86_400
SECONDS_PER_YEAR = 365.0 * 24 * 3600  # matches kbtc.pricing.SECONDS_PER_YEAR


# =========================================================================================
# Unit helpers (variance-per-second <-> sigma <-> annualized), to interface with pricing.
# =========================================================================================
def sigma_per_sqrt_second_from_var(var_per_second: float) -> float:
    """``sigma`` (log-return per sqrt second) from a per-second variance. This is the number
    ``pricing.settlement_moments`` / ``pricing.digital_up`` take as ``sigma``."""
    if var_per_second < 0:
        raise ValueError("var_per_second must be non-negative")
    return math.sqrt(var_per_second)


def var_per_second_from_sigma(sigma: float) -> float:
    """Inverse of :func:`sigma_per_sqrt_second_from_var`."""
    return sigma * sigma


def annualized_from_var(var_per_second: float) -> float:
    """Annualized vol (for reporting only) from a per-second variance.

    Inverse of ``pricing.sigma_per_sqrt_second(annualized_vol)`` which does
    ``annualized / sqrt(SECONDS_PER_YEAR)``; here ``sigma * sqrt(SECONDS_PER_YEAR)``.
    """
    return sigma_per_sqrt_second_from_var(var_per_second) * math.sqrt(SECONDS_PER_YEAR)


# =========================================================================================
# Grid sampling (causal, gap-aware).
# =========================================================================================
def make_grid_ns(start_ns: int, end_ns: int, step_s: float) -> np.ndarray:
    """Regular integer-ns grid over ``[start_ns, end_ns]`` at spacing ``step_s`` seconds."""
    if step_s <= 0:
        raise ValueError("step_s must be positive")
    step_ns = int(round(step_s * NS_PER_S))
    if end_ns < start_ns:
        return np.empty(0, dtype=np.int64)
    n = (end_ns - start_ns) // step_ns
    return start_ns + step_ns * np.arange(n + 1, dtype=np.int64)


def sample_log_prices_on_grid(
    ts_ns: np.ndarray,
    prices: np.ndarray,
    grid_ns: np.ndarray,
    max_stale_s: float,
) -> np.ndarray:
    """Causal last-observation-carried-forward log-prices on ``grid_ns``.

    For each grid time ``g`` we take the most recent sample with ``ts <= g`` (no look-ahead). If
    that sample is older than ``max_stale_s`` (a gap in the feed) the grid point is ``NaN`` so it
    is excluded from any realized measure rather than carried stale across a hole. ``prices`` must
    be strictly positive; the return is the natural log of the carried price.
    """
    ts_ns = np.asarray(ts_ns, dtype=np.int64)
    prices = np.asarray(prices, dtype=np.float64)
    if ts_ns.shape != prices.shape:
        raise ValueError("ts_ns and prices must have the same shape")
    if np.any(np.diff(ts_ns) < 0):
        raise ValueError("ts_ns must be non-decreasing")
    if ts_ns.size and np.any(prices[~np.isnan(prices)] <= 0):
        raise ValueError("prices must be positive")
    out = np.full(grid_ns.shape, np.nan, dtype=np.float64)
    if ts_ns.size == 0:
        return out
    max_stale_ns = max_stale_s * NS_PER_S
    # idx-1 = last sample with ts <= g
    idx = np.searchsorted(ts_ns, grid_ns, side="right") - 1
    valid = idx >= 0
    gi = idx[valid]
    age = grid_ns[valid] - ts_ns[gi]
    fresh = age <= max_stale_ns
    sel = np.where(valid)[0][fresh]
    out[sel] = np.log(prices[gi[fresh]])
    return out


def log_returns(grid_log_prices: np.ndarray) -> np.ndarray:
    """One-step log returns of a grid log-price array; a return touching a ``NaN`` is ``NaN``."""
    return np.diff(np.asarray(grid_log_prices, dtype=np.float64))


# =========================================================================================
# Realized-variance estimators (pure, on clean 1-D log-price blocks).
# =========================================================================================
def realized_variance(block_log_prices: np.ndarray) -> float:
    """Naive realized variance = sum of squared one-step log returns over the block.

    NOT noise-robust (biased by ``2 n sigma_u**2`` under additive microstructure noise); kept for
    the signature plot and as the fast-scale leg of :func:`tsrv`.
    """
    r = np.diff(np.asarray(block_log_prices, dtype=np.float64))
    r = r[~np.isnan(r)]
    return float(np.dot(r, r))


def subsampled_rv(block_log_prices: np.ndarray, k: int) -> float:
    """Average of the ``k`` subgrid realized variances (the slow-scale leg of TSRV).

    Partition the block into ``k`` interleaved subgrids (stride ``k``); ``RV_avg`` is the mean of
    their realized variances. This subsamples to a coarser effective interval (``k x`` the base
    spacing), which already suppresses noise; TSRV then removes the residual bias analytically.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    p = np.asarray(block_log_prices, dtype=np.float64)
    if k == 1:
        return realized_variance(p)
    total = 0.0
    for start in range(k):
        sub = p[start::k]
        if sub.size >= 2:
            total += realized_variance(sub)
    return total / k


def tsrv(block_log_prices: np.ndarray, k: int) -> float:
    """Two-Scale Realized Variance (Zhang-Mykland-Ait-Sahalia 2005): noise-bias-corrected IV.

    ``TSRV = RV_avg - (n_bar / n) * RV_all`` with the small-sample factor ``(1 - n_bar/n)^-1``,
    where ``n`` is the number of base returns, ``RV_all`` the full-grid realized variance,
    ``RV_avg`` the average of the ``k`` subgrid realized variances, and ``n_bar = (n - k + 1)/k``
    the average returns per subgrid. Under additive iid microstructure noise ``E[RV_all] = IV +
    2 n sigma_u**2`` and ``E[RV_avg] = IV + 2 n_bar sigma_u**2``; the combination cancels the noise
    term and is a consistent estimator of the integrated variance over the block. Result clamped at
    0 (a small-sample TSRV can go slightly negative).
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    p = np.asarray(block_log_prices, dtype=np.float64)
    r = np.diff(p)
    n = int(np.sum(~np.isnan(r)))
    if n <= k:
        return realized_variance(p)
    rv_all = realized_variance(p)
    rv_avg = subsampled_rv(p, k)
    n_bar = (n - k + 1) / k
    est = (rv_avg - (n_bar / n) * rv_all) / (1.0 - n_bar / n)
    return max(est, 0.0)


def signature_plot(
    ts_ns: np.ndarray,
    prices: np.ndarray,
    steps_s: list[float],
    max_stale_mult: float = 3.0,
) -> dict[float, float]:
    """Realized variance **per second** vs sampling interval — the tool that justifies ``step_s``.

    For each candidate interval the series is resampled on a regular grid and the realized variance
    is normalized by the realized span so the values are comparable across intervals (``per-second``
    variance). Under pure diffusion the plot is flat; additive noise lifts the left end (small
    steps), smoothing depresses it. Pick ``step_s`` where it flattens. ``max_stale_mult`` sets the
    LOCF staleness cap at a multiple of the step.
    """
    ts_ns = np.asarray(ts_ns, dtype=np.int64)
    prices = np.asarray(prices, dtype=np.float64)
    out: dict[float, float] = {}
    if ts_ns.size < 2:
        return {s: float("nan") for s in steps_s}
    start, end = int(ts_ns[0]), int(ts_ns[-1])
    for step in steps_s:
        grid = make_grid_ns(start, end, step)
        lp = sample_log_prices_on_grid(ts_ns, prices, grid, max_stale_s=max_stale_mult * step)
        r = log_returns(lp)
        valid = r[~np.isnan(r)]
        if valid.size == 0:
            out[step] = float("nan")
        else:
            out[step] = float(np.dot(valid, valid) / (valid.size * step))
    return out


def realized_var_per_second(
    block_log_prices: np.ndarray, step_s: float, *, method: str = "tsrv", k: int = 5
) -> float:
    """Per-second variance over a block, gap-robust.

    ``method="naive"`` uses the mean squared return / step (robust to gaps via the valid count).
    ``method="tsrv"`` uses TSRV over the (interpolated-free) valid run normalized by the realized
    span; TSRV needs a clean block, so if the block has gaps we fall back to ``naive``.
    """
    if step_s <= 0:
        raise ValueError("step_s must be positive")
    p = np.asarray(block_log_prices, dtype=np.float64)
    r = np.diff(p)
    n_valid = int(np.sum(~np.isnan(r)))
    if n_valid == 0:
        return float("nan")
    if method == "naive" or np.any(np.isnan(p)):
        rr = r[~np.isnan(r)]
        return float(np.dot(rr, rr) / (n_valid * step_s))
    if method == "tsrv":
        iv = tsrv(p, k)
        span_s = n_valid * step_s
        return iv / span_s
    if method == "coarse_subsampled":
        raise ValueError("use coarse_subsampled_var_per_second (needs delta_star + fine grid)")
    raise ValueError(f"unknown method {method!r}")


def coarse_subsampled_var_per_second(
    fine_log_prices: np.ndarray, fine_step_s: float, delta_star_s: float
) -> float:
    """Per-second variance at the COARSE ``delta_star`` scale via OVERLAPPING returns on a fine grid.

    Fable's settlement-scale, model-free forward proxy: on the fine (e.g. 1 Hz) grid form every
    overlapping ``delta_star``-second log return ``r_i = p_{i+L} - p_i`` (``L = delta_star/fine_step``)
    and estimate the per-second variance as ``mean(r_i^2) / delta_star``. It is a DIFFERENT measure
    from the 5 s EWMA forecast (so QLIKE is not circular) and it is taken at the settlement scale, so
    under the index's smoothing it is not biased low the way the 5 s measure is. Overlapping (step-1)
    returns reduce the estimator's variance. Gap-robust via nan-mean. Returns ``NaN`` if no valid
    overlapping return exists.
    """
    if fine_step_s <= 0 or delta_star_s <= 0:
        raise ValueError("steps must be positive")
    lag = max(1, int(round(delta_star_s / fine_step_s)))
    p = np.asarray(fine_log_prices, dtype=np.float64)
    if p.size <= lag:
        return float("nan")
    r = p[lag:] - p[:-lag]
    r = r[~np.isnan(r)]
    if r.size == 0:
        return float("nan")
    return float(np.dot(r, r) / (r.size * delta_star_s))


def calibrate_scale_kappa(
    ts_ns: np.ndarray,
    prices: np.ndarray,
    *,
    delta_star_s: float,
    base_step_s: float,
    max_stale_s: float,
) -> float:
    """Causal LEVEL-correction scalar ``kappa = sig(delta_star) / sig(base_step)`` on training data.

    ``sig(s)`` is the per-second variance of the series sampled at interval ``s`` (the signature-plot
    value). Under the index's low-pass smoothing the fine (``base_step``) measure is biased LOW, so
    ``kappa > 1`` lifts the ``base_step`` EWMA level to the coarse ``delta_star`` (settlement) scale.
    MUST be called on TRAILING TRAINING data only (walk-forward). Returns 1.0 if either leg is
    unavailable/degenerate (no correction). This is the statistically-correct reading of Fable's
    ``[Σ r_dstar^2/dstar] / [Σ r_base^2/base]`` — a RATIO OF PER-SECOND VARIANCES (each summed
    squared return normalized by its own count), not the raw sums (which would carry a spurious
    ``base/dstar`` factor).
    """
    sig = signature_plot(ts_ns, prices, steps_s=[base_step_s, delta_star_s],
                         max_stale_mult=max(3.0, 2 * delta_star_s / base_step_s))
    base_v, coarse_v = sig.get(base_step_s), sig.get(delta_star_s)
    if base_v is None or coarse_v is None or not np.isfinite(base_v) or not np.isfinite(coarse_v):
        return 1.0
    if base_v <= 0 or coarse_v <= 0:
        return 1.0
    return float(coarse_v / base_v)


def kappa_by_vol_tercile(
    ts_ns: np.ndarray,
    prices: np.ndarray,
    *,
    delta_star_s: float,
    base_step_s: float,
    max_stale_s: float,
    window_s: float = 3600.0,
) -> dict[str, float]:
    """kappa computed within low/mid/high realized-vol terciles of ``window_s`` blocks (a stability
    probe). If kappa wanders >~15% across terciles the single-scalar assumption is suspect — reported
    as a caveat, not used in the forecast. Blocks are scored by their base-step per-second variance.
    """
    ts_ns = np.asarray(ts_ns, dtype=np.int64)
    prices = np.asarray(prices, dtype=np.float64)
    if ts_ns.size < 10:
        return {}
    win_ns = int(round(window_s * NS_PER_S))
    start, end = int(ts_ns[0]), int(ts_ns[-1])
    edges = np.arange(start, end + win_ns, win_ns)

    def _sumsq(lo: int, hi: int, step: float) -> tuple[float, int]:
        m = (ts_ns >= lo) & (ts_ns < hi)
        if int(np.sum(m)) < 5:
            return 0.0, 0
        g = make_grid_ns(int(lo), int(hi), step)
        lp = sample_log_prices_on_grid(ts_ns[m], prices[m], g, max_stale_s)
        r = log_returns(lp)
        r = r[~np.isnan(r)]
        return float(np.dot(r, r)), int(r.size)

    # score each block by its base-step per-second variance; carry its (sumsq, count) at BOTH scales
    blocks: list[tuple[float, float, int, float, int]] = []  # (score, base_ss, base_n, coarse_ss, coarse_n)
    for lo, hi in zip(edges[:-1], edges[1:]):
        bss, bn = _sumsq(int(lo), int(hi), base_step_s)
        css, cn = _sumsq(int(lo), int(hi), delta_star_s)
        if bn and cn:
            blocks.append((bss / (bn * base_step_s), bss, bn, css, cn))
    if len(blocks) < 3:
        return {}
    blocks.sort(key=lambda b: b[0])
    thirds = np.array_split(np.arange(len(blocks)), 3)
    out: dict[str, float] = {}
    for name, idxs in zip(("low", "mid", "high"), thirds):
        base_ss = sum(blocks[i][1] for i in idxs)
        base_n = sum(blocks[i][2] for i in idxs)
        coarse_ss = sum(blocks[i][3] for i in idxs)
        coarse_n = sum(blocks[i][4] for i in idxs)
        if base_n and coarse_n:
            base_v = base_ss / (base_n * base_step_s)
            coarse_v = coarse_ss / (coarse_n * delta_star_s)
            out[name] = float(coarse_v / base_v) if base_v > 0 else 1.0
    return out


# =========================================================================================
# Intraday seasonality (multiplicative, causal).
# =========================================================================================
def _seconds_of_day(ts_ns: np.ndarray) -> np.ndarray:
    return (np.asarray(ts_ns, dtype=np.int64) // NS_PER_S) % SECONDS_PER_DAY


def time_of_day_bin(ts_ns: np.ndarray, n_bins: int) -> np.ndarray:
    """UTC time-of-day bin index in ``[0, n_bins)`` for each timestamp."""
    sod = _seconds_of_day(ts_ns)
    b = (sod * n_bins) // SECONDS_PER_DAY
    return np.clip(b, 0, n_bins - 1).astype(np.int64)


@dataclass(frozen=True)
class IntradaySeasonality:
    """Multiplicative per-bin variance seasonal factor, normalized to a count-weighted mean of 1.

    ``factors[b]`` multiplies the deseasonalized per-second variance to recover the variance at a
    time in bin ``b``. Fit on PAST days only (``fit``); a bin with no training data gets factor 1
    (neutral). Shrinkage toward 1 (``shrinkage`` in [0,1]) stabilizes sparse bins on few days.
    """

    n_bins: int
    factors: np.ndarray

    @classmethod
    def uniform(cls, n_bins: int = 24) -> "IntradaySeasonality":
        return cls(n_bins=n_bins, factors=np.ones(n_bins, dtype=np.float64))

    @classmethod
    def fit(
        cls,
        ts_ns: np.ndarray,
        sq_returns: np.ndarray,
        n_bins: int = 24,
        shrinkage: float = 0.0,
    ) -> "IntradaySeasonality":
        """Fit from per-return timestamps and squared (per-second) returns.

        ``sq_returns[i]`` should be the per-second variance contribution of the return at
        ``ts_ns[i]`` (i.e. ``r_i**2 / step``), so factors are scale-free. The factor of a bin is its
        mean contribution divided by the global mean; the result is renormalized to a count-weighted
        mean of 1 so deseasonalizing then re-seasonalizing is level-preserving.
        """
        if not 0.0 <= shrinkage <= 1.0:
            raise ValueError("shrinkage must be in [0, 1]")
        ts_ns = np.asarray(ts_ns, dtype=np.int64)
        sq = np.asarray(sq_returns, dtype=np.float64)
        mask = np.isfinite(sq)
        ts_ns, sq = ts_ns[mask], sq[mask]
        factors = np.ones(n_bins, dtype=np.float64)
        counts = np.zeros(n_bins, dtype=np.int64)
        if sq.size == 0:
            return cls(n_bins=n_bins, factors=factors)
        bins = time_of_day_bin(ts_ns, n_bins)
        global_mean = float(np.mean(sq))
        if global_mean <= 0:
            return cls(n_bins=n_bins, factors=factors)
        for b in range(n_bins):
            sel = sq[bins == b]
            counts[b] = sel.size
            if sel.size:
                raw = float(np.mean(sel)) / global_mean
                factors[b] = (1.0 - shrinkage) * raw + shrinkage * 1.0
        # Renormalize to a count-weighted mean of 1 (level-preserving).
        wsum = float(np.sum(counts))
        if wsum > 0:
            wmean = float(np.sum(factors * counts) / wsum)
            if wmean > 0:
                factors = factors / wmean
        return cls(n_bins=n_bins, factors=factors)

    def factor_at(self, ts_ns: np.ndarray) -> np.ndarray:
        """Seasonal factor(s) for the given timestamp(s)."""
        scalar = np.isscalar(ts_ns)
        arr = np.atleast_1d(np.asarray(ts_ns, dtype=np.int64))
        f = self.factors[time_of_day_bin(arr, self.n_bins)]
        return float(f[0]) if scalar else f

    def deseasonalize(self, ts_ns: np.ndarray, var_per_s: np.ndarray) -> np.ndarray:
        """Divide a per-second variance series by its seasonal factor (-> stationary level)."""
        return np.asarray(var_per_s, dtype=np.float64) / self.factor_at(ts_ns)

    def reseasonalize(self, ts_ns: np.ndarray, deseas_var_per_s: np.ndarray) -> np.ndarray:
        """Multiply a deseasonalized level back up to the per-second variance at that time."""
        return np.asarray(deseas_var_per_s, dtype=np.float64) * self.factor_at(ts_ns)


# =========================================================================================
# Online EWMA estimator (causal, time-decay, deseasonalized).
# =========================================================================================
@dataclass(frozen=True)
class EwmaVolConfig:
    """Configuration for the realized-vol EWMA forecaster.

    ``step_s`` — sparse sampling interval (noise-robust; justify with the signature plot).
    ``halflife_s`` — EWMA half-life in seconds (responsiveness vs noise).
    ``max_stale_s`` — LOCF staleness cap; a gap longer than this produces no sample.
    ``n_seasonal_bins`` / ``seasonal_shrinkage`` — intraday seasonality resolution + shrinkage.
    ``var_floor`` — floor on per-second variance (avoid a zero/degenerate sigma near a flat patch).
    ``scale_kappa`` — multiplicative LEVEL correction applied to the per-second variance
        (``sigma^2 = kappa * EWMA(naive step_s r^2/step)``). The EWMA ingests NAIVE sparsely-sampled
        squared returns (no in-estimator noise/smoothing correction); ``kappa`` lifts that level to
        the settlement (coarse ``delta_star``) scale. ``kappa`` is NOT a free knob — it is calibrated
        causally on trailing TRAINING days by :func:`calibrate_scale_kappa` (ratio of the signature
        plot at ``delta_star`` to at ``step_s``). Default 1.0 = no correction (e.g. a flat signature).
    """

    step_s: float = 5.0
    halflife_s: float = 180.0
    max_stale_s: float = 30.0
    n_seasonal_bins: int = 24
    seasonal_shrinkage: float = 0.25
    var_floor: float = 1e-14
    scale_kappa: float = 1.0


@dataclass(frozen=True)
class VolPath:
    """Causal EWMA output on a grid: per-second variance (deseasonalized level & re-seasonalized)."""

    grid_ns: np.ndarray
    deseas_level: np.ndarray   # EWMA of deseasonalized per-second variance (stationary activity)
    var_per_s: np.ndarray      # kappa * re-seasonalized per-second variance at each grid time
    n_warm: int                # grid points before the EWMA has seen any return (level = NaN)
    scale_kappa: float = 1.0   # the LEVEL correction baked into var_per_s (see EwmaVolConfig)

    def sigma_at(self, grid_index: int) -> float:
        v = self.var_per_s[grid_index]
        return float("nan") if not np.isfinite(v) else math.sqrt(max(v, 0.0))


def _decay_per_step(step_s: float, halflife_s: float) -> float:
    """Per-step EWMA weight ``alpha`` on the new observation for a time half-life."""
    if halflife_s <= 0:
        raise ValueError("halflife_s must be positive")
    return 1.0 - 2.0 ** (-step_s / halflife_s)


def estimate_vol_path(
    ts_ns: np.ndarray,
    prices: np.ndarray,
    config: EwmaVolConfig,
    seasonality: IntradaySeasonality | None = None,
    grid_ns: np.ndarray | None = None,
) -> VolPath:
    """Run the causal deseasonalized EWMA over a price series; return the per-grid vol path.

    At each grid step the per-second variance contribution ``r**2 / step`` is deseasonalized by the
    (past-fit) seasonal factor, EWMA-updated into a stationary level, then re-seasonalized at the
    grid time. Gapped returns (``NaN`` from a stale/absent sample) do not update the EWMA (the level
    is held), so holes never inject spurious variance. The first ``n_warm`` grid points have no
    return yet and carry ``NaN``. ``seasonality=None`` means no deseasonalization (factor 1).
    """
    ts_ns = np.asarray(ts_ns, dtype=np.int64)
    prices = np.asarray(prices, dtype=np.float64)
    seas = seasonality or IntradaySeasonality.uniform(config.n_seasonal_bins)
    if grid_ns is None:
        if ts_ns.size < 2:
            grid_ns = np.empty(0, dtype=np.int64)
        else:
            grid_ns = make_grid_ns(int(ts_ns[0]), int(ts_ns[-1]), config.step_s)
    lp = sample_log_prices_on_grid(ts_ns, prices, grid_ns, config.max_stale_s)
    r = log_returns(lp)  # length len(grid)-1; r[i] is the return into grid point i+1
    alpha = _decay_per_step(config.step_s, config.halflife_s)
    factors = seas.factor_at(grid_ns) if grid_ns.size else np.empty(0)

    deseas_level = np.full(grid_ns.shape, np.nan, dtype=np.float64)
    level = float("nan")
    n_warm = 0
    for i in range(grid_ns.size):
        if i == 0:
            if not np.isnan(level):
                deseas_level[i] = level
            else:
                n_warm += 1
            continue
        ri = r[i - 1]
        if np.isfinite(ri):
            # per-second variance contribution, deseasonalized by the factor at this grid time
            fac = factors[i] if factors.size else 1.0
            fac = fac if fac > 0 else 1.0
            contrib = (ri * ri) / config.step_s / fac
            level = contrib if np.isnan(level) else (1.0 - alpha) * level + alpha * contrib
        if np.isnan(level):
            n_warm += 1
        deseas_level[i] = level
    var_per_s = np.where(
        np.isnan(deseas_level),
        np.nan,
        np.maximum(config.scale_kappa * deseas_level * (factors if factors.size else 1.0),
                   config.var_floor),
    )
    return VolPath(grid_ns=grid_ns, deseas_level=deseas_level, var_per_s=var_per_s,
                   n_warm=n_warm, scale_kappa=config.scale_kappa)


def expected_forward_variance(
    var_path: VolPath,
    origin_index: int,
    horizon_s: float,
    seasonality: IntradaySeasonality,
    step_s: float,
) -> float:
    """Expected total variance over ``[t, t+H]`` from the current deseasonalized level.

    ``= deseas_level(t) * integral of the seasonal factor over [t, t+H]`` (approximated on the grid).
    For ``H`` short vs the diurnal cycle this is ~``var_per_s(t) * H``; the seasonal integral makes
    it correct when the forward window spans a change in the time-of-day factor. Feeds the pricing
    ``tau`` directly (this is the ``sigma**2 * tau`` point-settlement variance before the
    averaging correction). Returns ``NaN`` if the level is not yet warm.
    """
    lvl = var_path.deseas_level[origin_index]
    if not np.isfinite(lvl):
        return float("nan")
    t0 = int(var_path.grid_ns[origin_index])
    step_ns = int(round(step_s * NS_PER_S))
    n_steps = max(1, int(round(horizon_s / step_s)))
    future = t0 + step_ns * np.arange(n_steps, dtype=np.int64)
    fac = seasonality.factor_at(future)
    return float(var_path.scale_kappa * lvl * np.sum(fac) * step_s)


# =========================================================================================
# QLIKE evaluation harness (walk-forward by calendar day).
# =========================================================================================
def qlike(realized: np.ndarray, forecast: np.ndarray) -> np.ndarray:
    """Per-observation QLIKE loss ``h/f - ln(h/f) - 1`` (>= 0, min at f == h).

    Robust to a noisy but conditionally-unbiased variance proxy ``realized`` (Patton 2011). Both
    args are variances (same units). Non-finite / non-positive entries yield ``NaN`` (dropped by
    :func:`qlike_mean`).
    """
    h = np.asarray(realized, dtype=np.float64)
    f = np.asarray(forecast, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = h / f
        loss = ratio - np.log(ratio) - 1.0
    bad = ~np.isfinite(loss) | (h <= 0) | (f <= 0)
    loss = np.where(bad, np.nan, loss)
    return loss


def qlike_mean(realized: np.ndarray, forecast: np.ndarray) -> float:
    """Mean QLIKE over the valid (finite, positive) observations."""
    loss = qlike(realized, forecast)
    loss = loss[np.isfinite(loss)]
    return float(np.mean(loss)) if loss.size else float("nan")


@dataclass(frozen=True)
class MincerZarnowitz:
    slope: float
    intercept: float
    r2: float
    n: int


def mincer_zarnowitz(realized: np.ndarray, forecast: np.ndarray) -> MincerZarnowitz:
    """Regress realized on forecast (``realized = a + b*forecast``). Ideal: a=0, b=1, high R^2.

    An auxiliary calibration check for the forecast (not the headline metric). Uses the valid
    (finite, positive) pairs.
    """
    h = np.asarray(realized, dtype=np.float64)
    f = np.asarray(forecast, dtype=np.float64)
    ok = np.isfinite(h) & np.isfinite(f) & (h > 0) & (f > 0)
    h, f = h[ok], f[ok]
    if h.size < 2 or np.allclose(f, f[0]):
        return MincerZarnowitz(float("nan"), float("nan"), float("nan"), int(h.size))
    b, a = np.polyfit(f, h, 1)
    pred = a + b * f
    ss_res = float(np.sum((h - pred) ** 2))
    ss_tot = float(np.sum((h - np.mean(h)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return MincerZarnowitz(slope=float(b), intercept=float(a), r2=r2, n=int(h.size))


def _utc_date(ns: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ns // NS_PER_S, tz=_dt.UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class ForecastRow:
    """One forecast origin: the day, the forward realized target, and each forecast's value."""

    date: str
    origin_ns: int
    realized: float
    forecasts: dict[str, float]


@dataclass(frozen=True)
class DayScore:
    date: str
    n: int
    qlike: dict[str, float]  # model-name -> mean QLIKE on this day


@dataclass(frozen=True)
class EvalReport:
    """Walk-forward QLIKE report: per-day scores, pooled scores, ratios, MZ, bootstrap CIs."""

    model_names: tuple[str, ...]
    per_day: tuple[DayScore, ...]
    pooled_qlike: dict[str, float]
    qlike_ratio_vs: dict[str, dict[str, float]]  # baseline -> {model: ratio model/baseline}
    mz: dict[str, MincerZarnowitz]
    boot_ci: dict[str, tuple[float, float]]  # model -> (lo, hi) 95% per-day-block bootstrap CI
    test_dates: tuple[str, ...]


def _block_bootstrap_ci(
    per_day_means: list[float], n_boot: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """Per-day block bootstrap CI for the mean of a per-day metric (resample whole days)."""
    vals = np.asarray([v for v in per_day_means if np.isfinite(v)], dtype=np.float64)
    if vals.size == 0:
        return (float("nan"), float("nan"))
    if vals.size == 1:
        return (float(vals[0]), float(vals[0]))
    rng = np.random.default_rng(seed)
    means = np.array([np.mean(rng.choice(vals, size=vals.size, replace=True)) for _ in range(n_boot)])
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def score_forecasts(
    rows: list[ForecastRow],
    model_names: list[str],
    test_dates: list[str] | None = None,
    seed: int = 0,
) -> EvalReport:
    """Aggregate per-origin forecast rows into the walk-forward QLIKE report.

    Pure: ``rows`` are already-produced (causal) forecast/target pairs. ``test_dates`` restricts
    scoring to held-out days (defaults to all days present). Reports per-day mean QLIKE, pooled
    QLIKE, QLIKE ratios vs each other model (as a baseline), Mincer-Zarnowitz per model, and a
    per-day block-bootstrap 95% CI of each model's mean QLIKE.
    """
    dates = sorted({r.date for r in rows})
    if test_dates is not None:
        keep = set(test_dates)
        rows = [r for r in rows if r.date in keep]
        dates = [d for d in dates if d in keep]

    per_day: list[DayScore] = []
    per_day_q: dict[str, list[float]] = {m: [] for m in model_names}
    for d in dates:
        day_rows = [r for r in rows if r.date == d]
        realized = np.array([r.realized for r in day_rows])
        q = {}
        for m in model_names:
            fc = np.array([r.forecasts.get(m, np.nan) for r in day_rows])
            qm = qlike_mean(realized, fc)
            q[m] = qm
            per_day_q[m].append(qm)
        per_day.append(DayScore(date=d, n=len(day_rows), qlike=q))

    realized_all = np.array([r.realized for r in rows])
    pooled: dict[str, float] = {}
    mz: dict[str, MincerZarnowitz] = {}
    for m in model_names:
        fc = np.array([r.forecasts.get(m, np.nan) for r in rows])
        pooled[m] = qlike_mean(realized_all, fc)
        mz[m] = mincer_zarnowitz(realized_all, fc)

    ratios: dict[str, dict[str, float]] = {}
    for base in model_names:
        ratios[base] = {
            m: (pooled[m] / pooled[base]) if pooled[base] and np.isfinite(pooled[base]) else float("nan")
            for m in model_names
        }
    boot = {m: _block_bootstrap_ci(per_day_q[m], seed=seed) for m in model_names}
    return EvalReport(
        model_names=tuple(model_names),
        per_day=tuple(per_day),
        pooled_qlike=pooled,
        qlike_ratio_vs=ratios,
        mz=mz,
        boot_ci=boot,
        test_dates=tuple(dates),
    )


# =========================================================================================
# End-to-end walk-forward runner (ties the pieces together on a single in-memory series).
# =========================================================================================
@dataclass(frozen=True)
class WalkForwardConfig:
    """Config for :func:`walk_forward_eval`.

    ``horizon_s`` — forecast/target forward window (e.g. 900 s = one contract life, or 300 s).
    ``forecast_every_s`` — spacing of forecast origins within each test day.
    ``target_method`` — forward-target proxy: ``"coarse_subsampled"`` (default; overlapping
        ``delta_star``-second returns on the fine grid — a DIFFERENT, settlement-scale, model-free
        measure from the 5 s forecast, so QLIKE is not circular), or ``"tsrv"`` / ``"naive"``.
    ``delta_star_s`` — the coarse (settlement) scale: both the signature-flattening scale used for
        the ``kappa`` level correction AND the coarse-subsampled target's lag. Pre-registered.
    ``target_fine_step_s`` — fine grid for the overlapping coarse-subsampled target (e.g. 1 Hz).
    ``target_k`` — TSRV subgrid count (only if ``target_method="tsrv"``).
    ``scale_calibrate`` — if True, calibrate ``kappa`` on trailing TRAINING days and apply it to the
        EWMA level AND every baseline (so QLIKE tests EWMA+seasonality, not the scale fix).
    ``min_target_fraction`` — require this fraction of the forward window to be non-gap, else skip.
    ``warmup_s`` — skip origins before this much data has accrued since series start (EWMA warmup).
    """

    ewma: EwmaVolConfig = field(default_factory=EwmaVolConfig)
    horizon_s: float = 900.0
    forecast_every_s: float = 60.0
    target_method: str = "coarse_subsampled"
    delta_star_s: float = 60.0
    target_fine_step_s: float = 1.0
    target_k: int = 5
    scale_calibrate: bool = True
    min_target_fraction: float = 0.8
    warmup_s: float = 1800.0
    trailing_window_s: float = 900.0
    prev_day_lookback_s: float = SECONDS_PER_DAY


def _forward_realized_var(
    ts_ns: np.ndarray, prices: np.ndarray, t0: int, horizon_s: float, cfg: WalkForwardConfig
) -> tuple[float, float]:
    """Forward realized **total** variance over ``[t0, t0+H]`` and the non-gap fraction.

    Returns ``(total_variance, covered_fraction)``. For ``coarse_subsampled`` the forward block is
    sampled on the FINE grid (``target_fine_step_s``) and the per-second variance is the overlapping
    ``delta_star``-second estimator (:func:`coarse_subsampled_var_per_second`); otherwise it samples
    on the base (EWMA) grid and uses :func:`realized_var_per_second`. x horizon = total variance.
    """
    if cfg.target_method == "coarse_subsampled":
        step = cfg.target_fine_step_s
    else:
        step = cfg.ewma.step_s
    grid = make_grid_ns(t0, t0 + int(round(horizon_s * NS_PER_S)), step)
    lp = sample_log_prices_on_grid(ts_ns, prices, grid, cfg.ewma.max_stale_s)
    r = log_returns(lp)
    n_total = r.size
    n_valid = int(np.sum(~np.isnan(r)))
    if n_total == 0:
        return float("nan"), 0.0
    covered = n_valid / n_total
    if cfg.target_method == "coarse_subsampled":
        vps = coarse_subsampled_var_per_second(lp, step, cfg.delta_star_s)
    else:
        vps = realized_var_per_second(lp, step, method=cfg.target_method, k=cfg.target_k)
    if not np.isfinite(vps):
        return float("nan"), covered
    return vps * horizon_s, covered


def walk_forward_eval(
    ts_ns: np.ndarray,
    prices: np.ndarray,
    test_dates: list[str],
    cfg: WalkForwardConfig | None = None,
    seed: int = 0,
) -> EvalReport:
    """Full causal walk-forward QLIKE evaluation on one ``(ts, price)`` series.

    For each test day: fit intraday seasonality on all data STRICTLY BEFORE that day, run the causal
    EWMA across the whole series (warm-started from the start of data — no look-ahead), then at each
    forecast origin in the day produce the model forecast (expected forward variance from the
    deseasonalized level + seasonal integral) and the baselines, with the forward realized-variance
    target. Returns the scored :class:`EvalReport` over the held-out origins.

    Models scored:
      * ``"model"``    — the deseasonalized EWMA forecaster.
      * ``"trailing"`` — trailing-window realized variance scaled to the horizon.
      * ``"prevday"``  — same-clock-time realized variance from ``prev_day_lookback_s`` earlier
                         (a seasonality-aware random-walk baseline).
    """
    cfg = cfg or WalkForwardConfig()
    ts_ns = np.asarray(ts_ns, dtype=np.int64)
    prices = np.asarray(prices, dtype=np.float64)
    model_names = ["model", "trailing", "prevday"]
    if ts_ns.size < 2:
        return score_forecasts([], model_names, test_dates, seed)

    t_start = int(ts_ns[0])
    step = cfg.ewma.step_s
    horizon_ns = int(round(cfg.horizon_s * NS_PER_S))
    warmup_ns = int(round(cfg.warmup_s * NS_PER_S))
    every_ns = int(round(cfg.forecast_every_s * NS_PER_S))

    # Pre-compute, once per test day, the seasonality on PAST days and the EWMA path.
    rows: list[ForecastRow] = []
    for d in sorted(set(test_dates)):
        day0 = _date_start_ns(d)
        day1 = day0 + SECONDS_PER_DAY * NS_PER_S
        past = ts_ns < day0
        if np.any(past):
            # Build per-return per-second squared returns on the base grid over past data for the fit.
            pg = make_grid_ns(t_start, day0, step)
            plp = sample_log_prices_on_grid(ts_ns, prices, pg, cfg.ewma.max_stale_s)
            pr = log_returns(plp)
            sq = (pr * pr) / step
            seas = IntradaySeasonality.fit(
                pg[1:], sq, n_bins=cfg.ewma.n_seasonal_bins, shrinkage=cfg.ewma.seasonal_shrinkage
            )
        else:
            seas = IntradaySeasonality.uniform(cfg.ewma.n_seasonal_bins)

        # Causal LEVEL correction: kappa calibrated on PAST (training) data only, then applied to
        # BOTH the EWMA level and the baselines so the QLIKE race tests EWMA+seasonality, not scale.
        if cfg.scale_calibrate and np.any(past):
            kappa = calibrate_scale_kappa(
                ts_ns[past], prices[past], delta_star_s=cfg.delta_star_s,
                base_step_s=step, max_stale_s=cfg.ewma.max_stale_s)
        else:
            kappa = 1.0
        ewma_cfg = replace(cfg.ewma, scale_kappa=kappa)

        # Causal EWMA path from start of data through end of this test day.
        path_grid = make_grid_ns(t_start, min(day1, int(ts_ns[-1])), step)
        if path_grid.size < 2:
            continue
        vp = estimate_vol_path(ts_ns, prices, ewma_cfg, seas, grid_ns=path_grid)

        origin = max(day0, t_start + warmup_ns)
        # snap origin up to the grid
        while origin < day1:
            if origin + horizon_ns > int(ts_ns[-1]):
                break
            gi = int(np.searchsorted(vp.grid_ns, origin, side="left"))
            if gi >= vp.grid_ns.size:
                break
            t0 = int(vp.grid_ns[gi])
            if t0 >= day1:
                break
            realized, covered = _forward_realized_var(ts_ns, prices, t0, cfg.horizon_s, cfg)
            if np.isfinite(realized) and covered >= cfg.min_target_fraction:
                f_model = expected_forward_variance(vp, gi, cfg.horizon_s, seas, step)
                f_trail = _trailing_forecast(ts_ns, prices, t0, cfg, kappa)
                f_prev = _prevday_forecast(ts_ns, prices, t0, cfg, kappa)
                rows.append(
                    ForecastRow(
                        date=d,
                        origin_ns=t0,
                        realized=realized,
                        forecasts={"model": f_model, "trailing": f_trail, "prevday": f_prev},
                    )
                )
            origin += every_ns

    return score_forecasts(rows, model_names, list(sorted(set(test_dates))), seed)


def _trailing_forecast(ts_ns, prices, t0, cfg: WalkForwardConfig, kappa: float = 1.0) -> float:
    step = cfg.ewma.step_s
    lo = t0 - int(round(cfg.trailing_window_s * NS_PER_S))
    grid = make_grid_ns(lo, t0, step)
    lp = sample_log_prices_on_grid(ts_ns, prices, grid, cfg.ewma.max_stale_s)
    vps = realized_var_per_second(lp, step, method="naive")
    return kappa * vps * cfg.horizon_s if np.isfinite(vps) else float("nan")


def _prevday_forecast(ts_ns, prices, t0, cfg: WalkForwardConfig, kappa: float = 1.0) -> float:
    step = cfg.ewma.step_s
    ref = t0 - int(round(cfg.prev_day_lookback_s * NS_PER_S))
    grid = make_grid_ns(ref, ref + int(round(cfg.horizon_s * NS_PER_S)), step)
    lp = sample_log_prices_on_grid(ts_ns, prices, grid, cfg.ewma.max_stale_s)
    vps = realized_var_per_second(lp, step, method="naive")
    return kappa * vps * cfg.horizon_s if np.isfinite(vps) else float("nan")


def _date_start_ns(date: str) -> int:
    import datetime as _dt

    d = _dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=_dt.UTC)
    return int(d.timestamp()) * NS_PER_S


# =========================================================================================
# Settlement-exact z^2 LEVEL test (the pricing-relevant calibration QLIKE cannot give).
#
# QLIKE ranks forecasts but is invariant to a common scale error in BOTH the forecast and its
# (shared-measure) proxy; it cannot tell you the σ LEVEL is right. The pricing question is whether
# Var_forecast[A] from pricing.settlement_moments actually matches the realized dispersion of the
# settlement average. For origin t, horizon τ: a(t,τ) = ln(A_{t+τ}) − ln(Y_t), where A is the 60 s
# settlement average ending at t+τ (our own 60-print mean now; the feed's avg_60s_data later) and
# Y_t is the index level at t. z = a / sqrt(Var_forecast). If σ is correctly scaled (and drift ~0),
# E[z^2] = 1 and z ~ N(0,1). Mean z^2 ≈ 1.3 ⇒ σ ≈ 14% low. Reported per τ with a per-day block
# bootstrap CI + a PIT/KS normality check.
# =========================================================================================
def realized_settlement_average(
    ts_ns: np.ndarray, prices: np.ndarray, close_ns: int, window: SettlementWindow, max_stale_s: float
) -> float:
    """Arithmetic mean of the index over the 60 one-second prints ending at ``close_ns`` (the
    settlement average A). Uses the ``T-59..T`` placement (last print at ``close_ns``). Returns the
    mean PRICE (not log); ``NaN`` if too many window seconds gap."""
    offsets = window.offsets_before_expiry()  # seconds before close, oldest first; last == 0
    sample_ns = np.array([close_ns - int(round(o * NS_PER_S)) for o in offsets], dtype=np.int64)
    lp = sample_log_prices_on_grid(ts_ns, prices, sample_ns, max_stale_s)
    px = np.exp(lp)
    px = px[~np.isnan(px)]
    if px.size < max(2, window.n_samples // 2):
        return float("nan")
    return float(np.mean(px))


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))


def ks_statistic_normal(z: np.ndarray) -> float:
    """One-sample KS statistic of ``z`` against the standard normal CDF (sup |F_emp − Φ|)."""
    z = np.sort(np.asarray(z, dtype=np.float64))
    z = z[np.isfinite(z)]
    n = z.size
    if n == 0:
        return float("nan")
    cdf = _norm_cdf(z)
    emp_hi = np.arange(1, n + 1) / n
    emp_lo = np.arange(0, n) / n
    return float(max(np.max(emp_hi - cdf), np.max(cdf - emp_lo)))


@dataclass(frozen=True)
class Z2Result:
    horizon_s: float
    n: int
    mean_z2: float
    ci: tuple[float, float]       # per-day block bootstrap 95% CI of mean z^2
    ks_normal: float              # KS of z vs N(0,1); PIT uniformity is equivalent
    implied_sigma_bias: float     # sqrt(1/mean_z2) − 1: fractional σ error (neg = σ too low)


def settlement_z2_test(
    ts_ns: np.ndarray,
    prices: np.ndarray,
    var_path: VolPath,
    seasonality: IntradaySeasonality,
    origins_ns: np.ndarray,
    horizons_s: list[float],
    *,
    window: SettlementWindow | None = None,
    max_stale_s: float = 30.0,
    seed: int = 0,
) -> dict[float, Z2Result]:
    """Settlement-exact z^2 LEVEL test at each horizon over the given forecast origins.

    ``var_path`` supplies σ̂_t = sqrt(var_per_s) at each origin (the scale-calibrated per-second
    variance). Var_forecast[A] = ``pricing.settlement_moments(ln Y_t, τ, σ̂_t, n_observed=0).var``
    (all 60 settlement samples in the future). z = a(t,τ)/sqrt(Var). Returns a :class:`Z2Result` per
    horizon. Causal: Y_t and σ̂_t are as-of t; A is realized after.
    """
    window = window or SettlementWindow()
    origins_ns = np.asarray(origins_ns, dtype=np.int64)
    out: dict[float, Z2Result] = {}
    for tau in horizons_s:
        tau_ns = int(round(tau * NS_PER_S))
        per_day: dict[str, list[float]] = {}
        zs: list[float] = []
        for t0 in origins_ns:
            gi = int(np.searchsorted(var_path.grid_ns, t0, side="left"))
            if gi >= var_path.grid_ns.size:
                continue
            vps = var_path.var_per_s[gi]
            if not np.isfinite(vps) or vps <= 0:
                continue
            sigma = math.sqrt(vps)
            # Y_t = index level as-of t0
            y_lp = sample_log_prices_on_grid(ts_ns, prices, np.array([t0], dtype=np.int64), max_stale_s)
            if np.isnan(y_lp[0]):
                continue
            log_y = float(y_lp[0])
            a_mean = realized_settlement_average(ts_ns, prices, int(t0) + tau_ns, window, max_stale_s)
            if not np.isfinite(a_mean) or a_mean <= 0:
                continue
            mom = settlement_moments(log_y, tau, sigma, observed_log_sum=0.0, n_observed=0, window=window)
            if mom.var <= 0:
                continue
            z = (math.log(a_mean) - log_y) / math.sqrt(mom.var)
            zs.append(z)
            per_day.setdefault(_utc_date(int(t0)), []).append(z * z)
        if not zs:
            out[tau] = Z2Result(tau, 0, float("nan"), (float("nan"), float("nan")), float("nan"), float("nan"))
            continue
        z_arr = np.asarray(zs)
        mean_z2 = float(np.mean(z_arr ** 2))
        day_means = [float(np.mean(v)) for v in per_day.values()]
        ci = _block_bootstrap_ci(day_means, seed=seed)
        bias = (math.sqrt(1.0 / mean_z2) - 1.0) if mean_z2 > 0 else float("nan")
        out[tau] = Z2Result(tau, z_arr.size, mean_z2, ci, ks_statistic_normal(z_arr), bias)
    return out


# =========================================================================================
# BM-shape flatness diagnostic: is the settlement-averaging variance really ~ σ^2 (τ − 2w/3)?
# =========================================================================================
@dataclass(frozen=True)
class BmShapeRow:
    horizon_s: float
    n: int
    var_point: float        # Var over origins of (ln S_{t+τ} − ln S_t)
    var_avg: float          # Var over origins of (ln A_{t+τ} − ln S_t)
    sigma2_point: float     # var_point / τ   (flat in τ ⇒ BM holds for the point)
    sigma2_avg: float       # var_avg / (τ − 2w/3)  (flat in τ ⇒ averaging formula holds)


def bm_shape_diagnostic(
    ts_ns: np.ndarray,
    prices: np.ndarray,
    origins_ns: np.ndarray,
    horizons_s: list[float],
    *,
    window: SettlementWindow | None = None,
    max_stale_s: float = 30.0,
) -> dict[float, BmShapeRow]:
    """Empirical ``V(τ)/τ`` and ``V_A(τ)/(τ − 2w/3)``; both should be ~flat (≈σ^2) if the BM +
    settlement-averaging model holds. A fixed multiplicative ``c(τ)`` table (fit on training days)
    is only warranted if these FAIL to be flat within CI. ``w`` = settlement window length."""
    window = window or SettlementWindow()
    w = window.length_s
    origins_ns = np.asarray(origins_ns, dtype=np.int64)
    out: dict[float, BmShapeRow] = {}
    for tau in horizons_s:
        tau_ns = int(round(tau * NS_PER_S))
        a_gaps, p_gaps = [], []
        for t0 in origins_ns:
            y = sample_log_prices_on_grid(ts_ns, prices, np.array([t0], dtype=np.int64), max_stale_s)[0]
            if np.isnan(y):
                continue
            end_pt = sample_log_prices_on_grid(ts_ns, prices, np.array([int(t0) + tau_ns], dtype=np.int64), max_stale_s)[0]
            a = realized_settlement_average(ts_ns, prices, int(t0) + tau_ns, window, max_stale_s)
            if np.isfinite(end_pt):
                p_gaps.append(float(end_pt) - float(y))
            if np.isfinite(a) and a > 0:
                a_gaps.append(math.log(a) - float(y))
        vp = float(np.var(p_gaps, ddof=1)) if len(p_gaps) > 1 else float("nan")
        va = float(np.var(a_gaps, ddof=1)) if len(a_gaps) > 1 else float("nan")
        denom = max(tau - 2 * w / 3, 1e-9)
        out[tau] = BmShapeRow(
            horizon_s=tau, n=len(a_gaps),
            var_point=vp, var_avg=va,
            sigma2_point=vp / tau if np.isfinite(vp) else float("nan"),
            sigma2_avg=va / denom if np.isfinite(va) else float("nan"),
        )
    return out
