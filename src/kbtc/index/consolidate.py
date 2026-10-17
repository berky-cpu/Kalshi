"""Order-book consolidation, order-size cap C_T, and uncrossing (brti-spec §3.2 + §3.7).

All price arithmetic is exact ``Decimal``; sizes are float BTC volume (precision policy in
``params.py``). The order-size-cap statistics (mean, winsorized std) are float by nature (§3.7).
"""
from __future__ import annotations

from decimal import Decimal

import numpy as np

from .book import ASK, BID, AsOfBook, Level
from .params import BrtiParams


# ---------------------------------------------------------------------------------------
# Consolidation (§3.2 step 1–2): merge all venues' levels by price, optionally capping each
# consolidated price level's aggregate size at C_T.
# ---------------------------------------------------------------------------------------
def consolidate(
    books: list[AsOfBook], cap: float | None = None
) -> tuple[list[Level], list[Level]]:
    """Merge included venues into one consolidated book (bids desc, asks asc).

    Sizes at the same price are summed across venues (§3.2 step 2). If ``cap`` is given, each
    consolidated price level's aggregate size is capped at ``cap`` (``min(size, C_T)``) — the
    order-size cap of §3.7, applied per price level in the consolidated book.
    """
    bid_sizes: dict[Decimal, float] = {}
    ask_sizes: dict[Decimal, float] = {}
    for bk in books:
        for lv in bk.bids:
            bid_sizes[lv.price] = bid_sizes.get(lv.price, 0.0) + lv.size
        for lv in bk.asks:
            ask_sizes[lv.price] = ask_sizes.get(lv.price, 0.0) + lv.size

    def _mk(sizes: dict[Decimal, float], reverse: bool) -> list[Level]:
        out = []
        for price, size in sizes.items():
            if cap is not None and size > cap:
                size = cap
            out.append(Level(price, size))
        out.sort(key=lambda lv: lv.price, reverse=reverse)
        return out

    return _mk(bid_sizes, reverse=True), _mk(ask_sizes, reverse=False)


# ---------------------------------------------------------------------------------------
# Order-size cap C_T (§3.7 Eq 4–5), computed from the UNCAPPED consolidated book.
# ---------------------------------------------------------------------------------------
def _near_top_sizes(levels: list[Level], best: Decimal, within_mult: Decimal,
                    min_levels: int, side: str) -> list[float]:
    """The "near-top" size pool for one side (§3.7 Eq 4a/4b): all levels within ±pct of best,
    but at least the top ``min_levels`` (or all levels if fewer)."""
    if not levels:
        return []
    if side == ASK:
        thresh = best * within_mult  # 1.05 * best_ask
        within = sum(1 for lv in levels if lv.price <= thresh)
    else:
        thresh = best * within_mult  # 0.95 * best_bid
        within = sum(1 for lv in levels if lv.price >= thresh)
    cnt = max(within, min(len(levels), min_levels))
    return [lv.size for lv in levels[:cnt]]


def _winsorize(sorted_s: np.ndarray, k: int) -> np.ndarray:
    """Winsorize a sorted sample: replace the bottom/top ``k`` with the k-th / (n-k-1)-th value."""
    n = len(sorted_s)
    if k <= 0 or n - 2 * k <= 0:
        return sorted_s.copy()
    out = sorted_s.copy()
    out[:k] = sorted_s[k]
    out[n - k:] = sorted_s[n - k - 1]
    return out


def order_size_cap(bids: list[Level], asks: list[Level], params: BrtiParams) -> float | None:
    """Compute C_T from the uncapped consolidated book (§3.7 Eq 4–5).

    ``C_T = mean + 5·σ`` where σ is the winsorized sample std (Eq 4h); ``mean`` is the TRIMMED
    mean (Eq 4e, literal/default) or the winsorized mean (alternative) per ``params.cap_mean``
    (O-4). Returns None if the pool is empty (no cap applied).
    """
    ask_mult = Decimal(1) + Decimal(str(params.cap_near_top_pct))   # 1.05
    bid_mult = Decimal(1) - Decimal(str(params.cap_near_top_pct))   # 0.95
    pool: list[float] = []
    if asks:
        pool += _near_top_sizes(asks, asks[0].price, ask_mult, params.cap_min_levels, ASK)
    if bids:
        pool += _near_top_sizes(bids, bids[0].price, bid_mult, params.cap_min_levels, BID)
    if not pool:
        return None

    s = np.sort(np.asarray(pool, dtype=float))
    n = len(s)
    k = int(np.floor(0.01 * n))

    # Trimmed mean (Eq 4e): drop k from each end. Winsorized set (Eq 4f): replace k from each end.
    trimmed = s[k:n - k] if (k > 0 and n - 2 * k > 0) else s
    wins = _winsorize(s, k)
    trimmed_mean = float(np.mean(trimmed))
    wins_mean = float(np.mean(wins))
    # Winsorized SAMPLE std (Eq 4h): ddof=1; degenerate to 0 for n<2.
    wins_std = float(np.std(wins, ddof=1)) if n >= 2 else 0.0

    mean = trimmed_mean if params.cap_mean == "trimmed" else wins_mean
    return mean + params.cap_sigma_mult * wins_std


# ---------------------------------------------------------------------------------------
# Uncrossing the consolidated book (§3.2 step 2a).
# ---------------------------------------------------------------------------------------
def uncross(bids: list[Level], asks: list[Level]) -> tuple[list[Level], list[Level]]:
    """Resolve cross-venue crossing: while best bid ≥ best ask, consume min(size,size) from each
    side (§3.2 step 2a). Partially consumed levels keep their price with residual size; zeroed
    levels are removed. Stops when best bid < best ask or a side is exhausted.

    Bids must be desc, asks asc. Returns new (bids, asks). Does not mutate the inputs.
    """
    b = [Level(lv.price, lv.size) for lv in bids]
    a = [Level(lv.price, lv.size) for lv in asks]
    bi = 0
    ai = 0
    while bi < len(b) and ai < len(a) and b[bi].price >= a[ai].price:
        m = min(b[bi].size, a[ai].size)
        b[bi] = Level(b[bi].price, b[bi].size - m)
        a[ai] = Level(a[ai].price, a[ai].size - m)
        if b[bi].size <= 0:
            bi += 1
        if a[ai].size <= 0:
            ai += 1
    return b[bi:], a[ai:]
