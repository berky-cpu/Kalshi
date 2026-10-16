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
# PRIMARY-VERIFIED 2026-10-16 (Kalshi series API): fee_type="quadratic", fee_multiplier=1.
# PARTIAL (corroboration only; Kalshi fee PDF 429'd): the 0.07/0.0175 coefficients,
# round-UP-per-order, and maker-free status. Quadratic schedule:
#   taker = ceil(M_taker*0.07*C*P*(1-P)), maker = ceil(M_maker*0.0175*C*P*(1-P)); baseline M_maker=0.
# These tests pin the CODE's behaviour and the fee_multiplier->rate mapping; they do NOT
# independently verify the 0.07 base (see test note below).


def test_kxbtc15m_quadratic_multiplier_maps_to_seven_percent():
    assert BASE_TAKER_RATE == Decimal("0.07")
    assert BASE_MAKER_RATE == Decimal("0.0175")
    assert KXBTC15M_FEE_MULTIPLIER == Decimal("1")
    # fee_multiplier (M_taker) scales the 0.07 base: 1 * 0.07 = 0.07.
    assert effective_taker_rate(1) == TAKER_RATE == Decimal("0.07")
    assert effective_taker_rate(2) == Decimal("0.14")
    assert MAKER_RATE == Decimal("0.0175")


def test_maker_fee_is_zero_when_disabled():
    # NOTE: this tests the maker_fee gate, NOT that KXBTC15M is maker-free. KXBTC15M's
    # maker-free status is a PARTIAL human-asserted mapping (baseline M_maker=0 + no maker
    # field in the API), tracked in docs/facts.md; there is no code yet that derives the
    # regime from the series fee_type. Caller passes maker_fees_enabled=False explicitly.
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
def test_taker_fee_quadratic_shape_self_consistent(price, expected):
    # Internal-consistency / regression check of the quadratic shape and round-up
    # (100 contracts). The `expected` cents are themselves derived from the 0.07 base
    # (see inline arithmetic), so this does NOT independently verify 0.07 — it matches
    # Allium's single-source reproduction of the schedule. Upgrading 0.07 to primary
    # needs a hand-transcribed real Kalshi fee charge as the oracle (see docs/facts.md).
    assert taker_fee(100, price) == expected
