"""Exact fee application at the Decimal/integer-cents boundary (never float).

Wraps ``kbtc.fees`` so the engine never re-implements the schedule. KXBTC15M is MAKER-FREE with NO
rebate (VERIFIED, docs/facts.md 0.2) — a resting maker fill costs $0.00. A taker (marketable) fill
pays ``ceil_to_cent(0.07 * C * P * (1 - P))``. ``P`` is the fill price in dollars of the leg being
transacted (YES leg for a yes-side order, NO leg for a no-side order); both are in (0, 1).
"""
from __future__ import annotations

from decimal import Decimal

from kbtc.fees import maker_fee, taker_fee

# KXBTC15M charges makers nothing (no rebate). Flip only if a future series says otherwise.
MAKER_FEES_ENABLED = False


def fill_fee(contracts: Decimal, price: Decimal, is_taker: bool) -> Decimal:
    """Fee in dollars for a fill of ``contracts`` at ``price`` (dollars), maker or taker.

    ``contracts`` may be a fixed-point ``Decimal`` (Kalshi sizes are whole-contract fixed-point,
    e.g. ``300.00``); it is rounded to whole contracts for the fee count. Price must be in (0, 1).
    """
    n = int(contracts.to_integral_value(rounding="ROUND_HALF_UP"))
    if n <= 0:
        return Decimal("0.00")
    if is_taker:
        return taker_fee(n, price)
    return maker_fee(n, price, maker_fees_enabled=MAKER_FEES_ENABLED)
