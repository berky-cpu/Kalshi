"""Top-level BRTI replica at one Effective Time T (brti-spec §3.8 pseudocode).

``compute_index`` is **constituent-agnostic**: it takes whatever constituent books are provided
(a SUBSET is fine — we currently record only Bitstamp + Kraken of the 7 BRTI constituents) and the
result records exactly which venues were used and why the others were dropped. A gap (no print) is
returned when nothing survives the contingency filters (§5.5) — never an interpolated value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .book import AsOfBook
from .consolidate import consolidate, order_size_cap, uncross
from .curves import index_value, price_volume_curve, utilized_depth
from .filters import DeviationFilter, prefilter_books
from .params import BrtiParams
from .spacing import dynamic_spacing


@dataclass(frozen=True)
class BrtiResult:
    """One Effective Time's replica output + full diagnostics.

    ``value`` is None on a gap (``gap=True``); ``reason`` explains a gap. ``venues_used`` is the
    ordered tuple of constituents that contributed; ``venues_dropped`` maps each disregarded venue
    to its reason token (filters.R_*). The remaining fields expose the algorithm's internal state
    for the tracking study / empirical open-item closing (cap, spacing, utilized depth, λ).
    """

    effective_time_ns: int
    value: Decimal | None
    gap: bool
    reason: str
    venues_used: tuple[str, ...]
    venues_dropped: dict[str, str] = field(default_factory=dict)
    cap_C_T: float | None = None
    spacing_s: float | None = None
    v_T: float | None = None
    lam: float | None = None
    n_grid: int = 0


def _gap(T: int, reason: str, dropped: dict[str, str]) -> BrtiResult:
    return BrtiResult(T, None, True, reason, (), dict(dropped))


def compute_index(
    books_by_venue: dict[str, AsOfBook],
    effective_time_ns: int,
    params: BrtiParams | None = None,
    dev_filter: DeviationFilter | None = None,
) -> BrtiResult:
    """Compute the replica index at ``effective_time_ns`` from the given constituent books.

    ``dev_filter`` carries the §5.3 hysteresis state across Effective Times; pass the SAME instance
    across a time series. If None, a fresh (stateless-this-call) filter is used.
    """
    params = params or BrtiParams()
    dev_filter = dev_filter or DeviationFilter(params)
    T = effective_time_ns

    # §5.1/§5.2 per-venue drops.
    kept, dropped = prefilter_books(books_by_venue, T, params)
    if not kept:
        return _gap(T, "all_stale_or_erroneous", dropped)

    # §5.3 potentially-erroneous median-deviation filter (stateful hysteresis).
    mids = {v: bk.mid for v, bk in kept.items()}
    kept_mids, dev_dropped = dev_filter.apply(mids)
    dropped.update(dev_dropped)
    included = {v: kept[v] for v in kept_mids}
    if not included:
        return _gap(T, "all_potentially_erroneous", dropped)

    books = list(included.values())

    # §3.7 order-size cap from the uncapped consolidated book (pre-uncross per F4 default).
    unc_bids, unc_asks = consolidate(books, cap=None)
    if params.cap_pre_uncross:
        cap_bids, cap_asks = unc_bids, unc_asks
    else:
        cap_bids, cap_asks = uncross(unc_bids, unc_asks)
    C_T = order_size_cap(cap_bids, cap_asks, params)

    # §3.2 consolidate with cap, then uncross.
    cons_bids, cons_asks = consolidate(books, cap=C_T)
    cons_bids, cons_asks = uncross(cons_bids, cons_asks)
    if not cons_bids or not cons_asks:
        return _gap(T, "empty_after_uncross", dropped)

    # §3.6 dynamic spacing.
    s_T = dynamic_spacing(cons_bids, cons_asks, params)
    if s_T <= 0:
        return _gap(T, "nonpositive_spacing", dropped)

    # §3.3 PV curves (up to the first D-breach), §3.4 utilized depth, §3.5 index value.
    curve = price_volume_curve(cons_bids, cons_asks, s_T, params)
    if not curve:
        return _gap(T, "no_depth", dropped)
    v_T = utilized_depth(curve, s_T, params)
    value, n_grid, lam = index_value(curve, v_T, params)

    return BrtiResult(
        effective_time_ns=T,
        value=value,
        gap=False,
        reason="",
        venues_used=tuple(included.keys()),
        venues_dropped=dropped,
        cap_C_T=C_T,
        spacing_s=s_T,
        v_T=v_T,
        lam=lam,
        n_grid=n_grid,
    )
