"""Kalshi trading fees.

Fee schedule (primary-VERIFIED 2026-10-16; see docs/facts.md). Source: Kalshi's own fee
schedule PDF + `https://kalshi.com/fee-schedule`, fetched by Berk. Kalshi uses a "quadratic"
fee schedule. The per-order fee in dollars is (quoted verbatim from the Kalshi schedule):
    taker: fee = round_up(M_taker * 0.07   * C * P * (1 - P))
    maker: fee = round_up(M_maker * 0.0175 * C * P * (1 - P))
with C contracts at price P in dollars, rounded UP to the next cent per order. (The PDF text
uses "centicent" wording; the schedule's own General Trading Fees TABLE resolves it to whole
cents — e.g. the 100-contract column is 0.07/0.63/1.32/1.75 at P=0.01/0.10/0.25/0.50 — so our
ROUND_CEILING to $0.01 reproduces the published table.)

The per-series `fee_multiplier` returned by `GET /series/<ticker>` is M_taker (the
taker multiplier). The baseline "quadratic" schedule has M_taker = 1 and M_maker = 0.

KXBTC15M (`GET /series/KXBTC15M`): fee_type="quadratic", fee_multiplier=1 (VERIFIED-
2026-10-16, Kalshi series API). Hence taker rate = 1 * 0.07 = 0.07. KXBTC15M is MAKER-FREE:
it is NOT on the Kalshi fee schedule's Non-standard maker-fee series list (VERIFIED-2026-10-16,
kalshi.com/fee-schedule), consistent with the baseline M_maker = 0.

Verification (see docs/facts.md), all primary-VERIFIED 2026-10-16:
- fee_type / fee_multiplier: Kalshi series API.
- 0.07 / 0.0175 constants, round-UP-per-order, and C*P*(1-P) shape: Kalshi fee-schedule PDF
  (formulas verbatim) + the schedule's published whole-cent fee table.
- KXBTC15M maker-free: absent from the schedule's Non-standard maker-fee series list.

Use Decimal throughout: fees are rounded up per order, so float error can flip a cent.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

# Fixed coefficients of Kalshi's quadratic schedule (the multiplier scales these).
BASE_TAKER_RATE = Decimal("0.07")
BASE_MAKER_RATE = Decimal("0.0175")

# KXBTC15M taker multiplier M_taker from GET /series/KXBTC15M (VERIFIED-2026-10-16, series API).
KXBTC15M_FEE_MULTIPLIER = Decimal("1")

# Effective per-contract rates used by this project's KXBTC15M quoting/backtests.
TAKER_RATE = BASE_TAKER_RATE * KXBTC15M_FEE_MULTIPLIER  # = 0.07
MAKER_RATE = BASE_MAKER_RATE
CENT = Decimal("0.01")


def effective_taker_rate(fee_multiplier: Decimal | str | int = KXBTC15M_FEE_MULTIPLIER) -> Decimal:
    """Taker rate for a series given its API `fee_multiplier` (M_taker).

    rate = fee_multiplier * BASE_TAKER_RATE. KXBTC15M has fee_multiplier=1 => 0.07.
    """
    return Decimal(str(fee_multiplier)) * BASE_TAKER_RATE


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
