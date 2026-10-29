"""Fill-conditional markouts and net P&L — the inputs to the 4.2 acceptance gate.

For each fill we compute a signed per-contract edge (in YES-equivalent dollars) at horizons of 5 s,
30 s, and at settlement:

* **Mid markout at horizon h** = value of the position at ``fill_ts + h`` (using the Kalshi YES
  mid, last-observation-carried-forward) minus the price paid, in the trade's direction:
    - yes-side buy at ``p``:  ``mid(t+h) - p``
    - no-side buy at ``q``  :  ``(1 - mid(t+h)) - q``   (we are long NO / short YES)
* **Settlement P&L** uses the recorded official result (``result_yes``) and INCLUDES the fee:
    - yes buy at ``p``:  ``(1 if result_yes else 0) - p`` per contract, minus fee
    - no  buy at ``q``:  ``(1 if not result_yes else 0) - q`` per contract, minus fee

Markouts are the adverse-selection / edge diagnostic; settlement P&L is the realized money. Prices,
mids and P&L are exact ``Decimal``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .engine import BacktestResult, FillRecord

ZERO = Decimal(0)
ONE = Decimal(1)
NS_PER_S = 1_000_000_000


def as_of(series: list[tuple[int, Decimal]], ts_ns: int) -> Decimal | None:
    """Last value in ``series`` (sorted by ts) with ts <= ``ts_ns`` (LOCF), or None if none."""
    lo, hi, found = 0, len(series) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts_ns:
            found = series[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return found


@dataclass(frozen=True)
class FillMarkout:
    order_id: int
    ts_ns: int
    book_side: str
    price: Decimal
    size: Decimal
    fee: Decimal
    markout_5s: Decimal | None  # per-contract, YES-equivalent dollars
    markout_30s: Decimal | None
    settle_pnl_per_contract: Decimal | None  # incl. fee amortized? no — fee is per fill, see total
    settle_pnl_total: Decimal | None  # size * per-contract payoff edge, minus total fill fee


def _position_value(book_side: str, yes_mid: Decimal) -> Decimal:
    """Value of one contract of the fill's position given a YES mid."""
    return yes_mid if book_side == "yes" else (ONE - yes_mid)


def fill_markout(fill: FillRecord, mid_series: list[tuple[int, Decimal]],
                 result_yes: bool | None, *, horizons_s=(5.0, 30.0)) -> FillMarkout:
    mids = [as_of(mid_series, fill.ts_ns + int(round(h * NS_PER_S))) for h in horizons_s]

    def edge(mid: Decimal | None) -> Decimal | None:
        if mid is None:
            return None
        return _position_value(fill.book_side, mid) - fill.price

    m5, m30 = (edge(mids[0]), edge(mids[1]))
    settle_pc = settle_tot = None
    if result_yes is not None:
        if fill.book_side == "yes":
            payoff = ONE if result_yes else ZERO
        else:
            payoff = ONE if not result_yes else ZERO
        settle_pc = payoff - fill.price
        settle_tot = settle_pc * fill.size - fill.fee
    return FillMarkout(
        order_id=fill.order_id, ts_ns=fill.ts_ns, book_side=fill.book_side, price=fill.price,
        size=fill.size, fee=fill.fee, markout_5s=m5, markout_30s=m30,
        settle_pnl_per_contract=settle_pc, settle_pnl_total=settle_tot,
    )


def markouts(result: BacktestResult, horizons_s=(5.0, 30.0)) -> list[FillMarkout]:
    """Markouts for every fill in a single-market backtest result."""
    ry = result.market.result_yes
    return [fill_markout(f, result.mid_series, ry, horizons_s=horizons_s) for f in result.fills]


def net_settlement_pnl(result: BacktestResult) -> Decimal | None:
    """Total realized P&L at settlement, net of fees, across all fills (None if result unknown)."""
    if result.market.result_yes is None:
        return None
    total = ZERO
    for mo in markouts(result):
        if mo.settle_pnl_total is not None:
            total += mo.settle_pnl_total
    return total
