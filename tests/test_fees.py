from decimal import Decimal

import pytest

from kbtc.fees import breakeven_edge, maker_fee, taker_fee


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
