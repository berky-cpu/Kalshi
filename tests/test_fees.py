from decimal import Decimal

import pytest

from kbtc.fees import (
    BASE_MAKER_RATE,
    BASE_TAKER_RATE,
    KXBTC15M_FEE_MULTIPLIER,
    MAKER_RATE,
    TAKER_RATE,
    breakeven_edge,
    effective_taker_rate,
    maker_fee,
    taker_fee,
)


def test_taker_fee_at_50c_rounds_up_per_order():
    assert taker_fee(1, "0.50") == Decimal("0.02")  # 0.0175 -> 0.02
    assert taker_fee(100, "0.50") == Decimal("1.75")


def test_taker_fee_symmetric_and_small_in_tails():
    assert taker_fee(100, "0.10") == taker_fee(100, "0.90") == Decimal("0.63")
    assert taker_fee(1, "0.01") == Decimal("0.01")


def test_maker_fee_zero_unless_enabled():
    assert maker_fee(100, "0.50", maker_fees_enabled=False) == Decimal("0.00")
    assert maker_fee(100, "0.50", maker_fees_enabled=True) == Decimal("0.44")  # 0.4375 -> 0.44


def test_breakeven_edge():
    assert breakeven_edge("0.50", 100) == Decimal("0.0175")


@pytest.mark.parametrize("bad", ["0", "1", "1.2", "-0.1"])
def test_rejects_bad_prices(bad):
    with pytest.raises(ValueError):
        taker_fee(1, bad)


# --- KXBTC15M fee regime (see docs/facts.md) -----------------------------------
# PRIMARY-VERIFIED 2026-10-16: fee_type="quadratic", fee_multiplier=1 (Kalshi series API); the
# 0.07/0.0175 coefficients, round-UP-per-order, and maker-free status (KXBTC15M absent from the
# Non-standard maker-fee series list) are confirmed from Kalshi's own fee schedule PDF +
# kalshi.com/fee-schedule. Quadratic schedule:
#   taker = ceil(M_taker*0.07*C*P*(1-P)), maker = ceil(M_maker*0.0175*C*P*(1-P)); baseline M_maker=0.


def test_kxbtc15m_quadratic_multiplier_maps_to_seven_percent():
    assert BASE_TAKER_RATE == Decimal("0.07")
    assert BASE_MAKER_RATE == Decimal("0.0175")
    assert KXBTC15M_FEE_MULTIPLIER == Decimal("1")
    # fee_multiplier (M_taker) scales the 0.07 base: 1 * 0.07 = 0.07.
    assert effective_taker_rate(1) == TAKER_RATE == Decimal("0.07")
    assert effective_taker_rate(2) == Decimal("0.14")
    assert MAKER_RATE == Decimal("0.0175")


def test_maker_fee_is_zero_when_disabled():
    # NOTE: this tests the maker_fee gate, NOT the series regime. KXBTC15M is maker-free
    # (VERIFIED 2026-10-16: absent from the Kalshi fee schedule's Non-standard maker-fee series
    # list), tracked in docs/facts.md; there is no code yet that derives the regime from the
    # series fee_type, so the caller passes maker_fees_enabled=False explicitly.
    assert maker_fee(100, "0.50", maker_fees_enabled=False) == Decimal("0.00")
    assert maker_fee(500, "0.37", maker_fees_enabled=False) == Decimal("0.00")


@pytest.mark.parametrize(
    "price,expected",
    [
        ("0.10", Decimal("0.63")),  # 0.07*100*0.1*0.9  = 0.63
        ("0.25", Decimal("1.32")),  # 0.07*100*0.25*0.75= 1.3125 -> 1.32
        ("0.50", Decimal("1.75")),  # 0.07*100*0.5*0.5  = 1.75
        ("0.75", Decimal("1.32")),  # symmetric with 0.25
        ("0.90", Decimal("0.63")),  # symmetric with 0.10
    ],
)
def test_taker_fee_matches_kalshi_published_table(price, expected):
    # Matches Kalshi's own published General Trading Fees TABLE (100-contract column):
    # 0.63/1.32/1.75 at P=0.10/0.25/0.50 and symmetric at 0.90/0.75 (Kalshi fee schedule PDF +
    # kalshi.com/fee-schedule, VERIFIED 2026-10-16). Confirms the 0.07 base and round-up-per-order.
    assert taker_fee(100, price) == expected
