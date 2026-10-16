"""Kalshi trading fees.

Formula (Kalshi fee schedule; re-verify in ROADMAP 0.2):
    fee = round_up_to_cent(rate * C * P * (1 - P))
with C contracts at price P dollars. Taker rate 0.07. Maker rate 0.0175 only on series
flagged for maker fees; KXBTC15M is believed to be maker-free (UNVERIFIED — check
`GET /series/KXBTC15M` fee_type before relying on it).

Use Decimal throughout: fees are rounded up per order, so float error can flip a cent.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0.0175")
CENT = Decimal("0.01")


def _to_price(price: Decimal | str | int) -> Decimal:
    p = Decimal(str(price)) if not isinstance(price, Decimal) else price
    if not (Decimal(0) < p < Decimal(1)):
        raise ValueError(f"price must be in (0, 1) dollars, got {p}")
    return p


def _fee(rate: Decimal, contracts: int, price: Decimal | str | int) -> Decimal:
    if contracts < 0:
        raise ValueError("contracts must be non-negative")
    p = _to_price(price)
    raw = rate * contracts * p * (1 - p)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


def taker_fee(contracts: int, price: Decimal | str | int, rate: Decimal = TAKER_RATE) -> Decimal:
    """Fee in dollars for taking `contracts` at `price` (dollars)."""
    return _fee(rate, contracts, price)


def maker_fee(
    contracts: int,
    price: Decimal | str | int,
    maker_fees_enabled: bool,
    rate: Decimal = MAKER_RATE,
) -> Decimal:
    """Fee in dollars when a resting order is filled. Zero unless the series charges makers."""
    if not maker_fees_enabled:
        _to_price(price)
        return Decimal("0.00")
    return _fee(rate, contracts, price)


def breakeven_edge(price: Decimal | str | int, contracts: int = 1, rate: Decimal = TAKER_RATE) -> Decimal:
    """Edge per contract (dollars) a taker needs just to cover the fee."""
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    return taker_fee(contracts, price, rate) / contracts
