"""Price-volume curves, utilized depth, and the exponential index value
(brti-spec §3.3 Eq 1a–1f, §3.4 Eq 2, §3.5 Eq 3).

Marginal prices (askPV/bidPV/midPV) are exact ``Decimal``; the volume axis (v, s, cumulative
sizes) and the mid-spread curve (a ratio) are float. The curve is walked lazily and stops at the
first D-breach so the grid never runs away for a tiny s (§3.4 picks the pre-breach point).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .book import Level
from .params import CENT, BrtiParams


@dataclass(frozen=True)
class GridPoint:
    v: float            # cumulative volume (BTC) on the grid
    ask_px: Decimal     # askPV(v): marginal ask price (exact)
    bid_px: Decimal     # bidPV(v): marginal bid price (exact)
    mid_px: Decimal     # midPV(v) = (ask+bid)/2 (exact)
    mid_sv: float       # midSV(v) = askPV/midPV − 1 (fractional half-spread)


class _CumCurve:
    """Cumulative size → marginal price lookup for one side (Eq 1a/1b).

    ``marginal(v)`` returns the price of the level containing the v-th BTC unit: the smallest m
    with cumulative size ≥ v. Returns None if v exceeds the side's total depth.
    """

    def __init__(self, levels: list[Level]) -> None:
        self._prices = [lv.price for lv in levels]
        self._cum: list[float] = []
        acc = 0.0
        for lv in levels:
            acc += lv.size
            self._cum.append(acc)
        self.total = acc
        self._i = 0  # monotone pointer (curve is queried on an increasing grid)

    def marginal(self, v: float) -> Decimal | None:
        if not self._cum or v > self.total:
            return None
        i = self._i
        while i < len(self._cum) and self._cum[i] < v:
            i += 1
        if i >= len(self._cum):
            return None
        self._i = i
        return self._prices[i]


def price_volume_curve(
    bids: list[Level], asks: list[Level], s: float, params: BrtiParams
) -> list[GridPoint]:
    """Build the mid PV / mid-spread curve on the grid v∈{s,2s,…} up to the first D-breach.

    The walk stops one grid step after midSV first exceeds D (so utilized_depth can read the
    pre-breach point), or when either side runs out of depth. Returns [] if s ≤ 0.
    """
    if s <= 0:
        return []
    ask_c = _CumCurve(asks)
    bid_c = _CumCurve(bids)
    max_v = min(ask_c.total, bid_c.total)
    D = params.deviation_from_mid
    pts: list[GridPoint] = []
    m = 1
    while True:
        v = m * s
        if v > max_v or m > params.max_grid_points:
            break
        ap = ask_c.marginal(v)
        bp = bid_c.marginal(v)
        if ap is None or bp is None:
            break
        mid = (ap + bp) / 2
        # midSV: ratio ⇒ float. Guard a zero mid (impossible for positive prices).
        mid_sv = (float(ap) / float(mid) - 1.0) if mid != 0 else float("inf")
        pts.append(GridPoint(v, ap, bp, mid, mid_sv))
        if mid_sv > D:
            break
        m += 1
    return pts


def utilized_depth(curve: list[GridPoint], s: float, params: BrtiParams) -> float:
    """v_T = largest grid v with midSV ≤ D before the first breach, floored at s (§3.4 Eq 2).

    If even the first point (v=s) already breaches D, v_T = s (the floor); the index then reduces
    to the consolidated best mid (§4.2 "A Note on Properties").
    """
    D = params.deviation_from_mid
    last_ok = None
    for p in curve:
        if p.mid_sv <= D:
            last_ok = p.v
        else:
            break
    if last_ok is None:
        return s
    return max(last_ok, s)


def index_value(curve: list[GridPoint], v_T: float, params: BrtiParams) -> tuple[Decimal, int, float]:
    """Exponential depth-weighted mean of the mid PV curve up to v_T (§3.5 Eq 3).

    λ = 1/(lam_factor·v_T) (= 1/(0.3·v_T), VERIFIED O-2). Weights w_i = λ·e^(−λ v_i) over
    v∈{s,…,v_T}, normalized by NF=Σw_i. Returns (value rounded to $0.01, n_grid used, λ).

    Float is used only for the (transcendental) weights and the weighted sum; the exact Decimal
    mid prices are converted at the final sum, which is rounded to the $0.01 dissemination
    precision — exactly where the methodology rounds.
    """
    import math

    lam = 1.0 / (params.lam_factor * v_T)
    used = [p for p in curve if p.v <= v_T + 1e-12]
    if not used:
        raise ValueError("index_value called with no grid points up to v_T")
    weights = [lam * math.exp(-lam * p.v) for p in used]
    nf = sum(weights)
    if nf <= 0:
        # All weights underflowed (λ·v huge): fall back to the shallowest mid (best mid).
        value = used[0].mid_px
    else:
        acc = 0.0
        for p, w in zip(used, weights):
            acc += float(p.mid_px) * (w / nf)
        value = Decimal(repr(acc))
    return value.quantize(CENT, rounding=ROUND_HALF_UP), len(used), lam
