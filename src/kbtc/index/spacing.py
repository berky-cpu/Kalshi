"""Dynamic spacing s_T (brti-spec §3.6, Eq 6–7).

s_T = min(s_lev, s_KDE):
  * s_lev (Eq 6a): min(total_ask_liq, total_bid_liq) / τ, τ=100 target levels.
  * s_KDE (Eq 6b–6j): on the LOWER-liquidity side, winsorize its size sample (k=⌊0.01 n⌋),
    Silverman bandwidth h = 1.06·σ_side·n^(−1/5), Gaussian KDE, s_KDE = argmax_s f_KDE(s) if
    n≥4, else the smallest sampled size.

The ``argmax`` has no methodology-specified grid (O-3): we search a uniform grid over the sample
support with ``params.kde_grid_points`` points and show (in tests) the output is invariant to the
resolution. All of §3.6 is float — a BTC volume-axis quantity, not a price boundary.
"""
from __future__ import annotations

import numpy as np

from .book import Level
from .consolidate import _winsorize
from .params import BrtiParams


def _total_liquidity(levels: list[Level]) -> float:
    return float(sum(lv.size for lv in levels))


def level_spacing(bids: list[Level], asks: list[Level], tau: int) -> float:
    """s_lev = min(total_ask_liq, total_bid_liq) / τ (Eq 6a)."""
    return min(_total_liquidity(asks), _total_liquidity(bids)) / tau


def kde_spacing(sizes: list[float], params: BrtiParams) -> float:
    """s_KDE on a side's size sample (Eq 6b–6j).

    n < kde_min_samples (4) ⇒ the smallest sampled size. Else: winsorized std → Silverman
    bandwidth → Gaussian KDE → argmax over a uniform support grid. A degenerate (zero-variance)
    sample returns that single value (its mode).
    """
    s = np.sort(np.asarray(sizes, dtype=float))
    n = len(s)
    if n == 0:
        return 0.0
    if n < params.kde_min_samples:
        return float(s[0])  # smallest sampled size (Eq 6j else-branch)

    k = int(np.floor(0.01 * n))
    wins = _winsorize(s, k)
    sigma = float(np.std(wins, ddof=1))
    smin, smax = float(s[0]), float(s[-1])
    if sigma <= 0 or smax <= smin:
        return smin  # all equal (or no spread) ⇒ the mode is that value

    h = params.kde_silverman_c * sigma * n ** (-1.0 / 5.0)
    if h <= 0:
        return smin
    grid = np.linspace(smin, smax, params.kde_grid_points)
    # Gaussian KDE density (constant 1/(n·h·√2π) is monotone so immaterial to the argmax, but
    # kept for a faithful f_KDE): f(x) = 1/(n h) Σ φ((x−s_i)/h).
    z = (grid[:, None] - s[None, :]) / h
    dens = np.exp(-0.5 * z * z).sum(axis=1) / (n * h * np.sqrt(2 * np.pi))
    return float(grid[int(np.argmax(dens))])


def dynamic_spacing(bids: list[Level], asks: list[Level], params: BrtiParams) -> float:
    """s_T = min(s_lev, s_KDE) (Eq 7). KDE is computed on the lower-liquidity side (Eq 6b)."""
    s_lev = level_spacing(bids, asks, params.target_levels)
    total_ask = _total_liquidity(asks)
    total_bid = _total_liquidity(bids)
    lower_side = asks if total_ask <= total_bid else bids
    s_kde = kde_spacing([lv.size for lv in lower_side], params)
    candidates = [c for c in (s_lev, s_kde) if c > 0]
    if not candidates:
        return 0.0
    return min(candidates)
