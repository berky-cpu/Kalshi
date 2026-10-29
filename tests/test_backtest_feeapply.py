"""Fee boundary tests: exact Decimal/integer-cents, never float (kbtc.backtest.feeapply)."""
from decimal import Decimal

from kbtc.backtest.feeapply import fill_fee
from kbtc.fees import taker_fee

D = Decimal


def test_maker_fill_is_free_no_rebate():
    assert fill_fee(D("300.00"), D("0.50"), is_taker=False) == D("0.00")


def test_taker_fill_matches_fee_schedule():
    # 100 contracts at $0.50: schedule table value is $1.75 (ceil(0.07*100*0.5*0.5)=ceil(1.75)).
    assert fill_fee(D("100.00"), D("0.50"), is_taker=True) == D("1.75")
    assert fill_fee(D("100.00"), D("0.50"), is_taker=True) == taker_fee(100, D("0.50"))


def test_fixed_point_size_rounds_to_whole_contracts():
    # count_fp arrives as "300.00" -> 300 contracts.
    assert fill_fee(D("300.00"), D("0.10"), is_taker=True) == taker_fee(300, D("0.10"))


def test_zero_contracts_is_zero_fee():
    assert fill_fee(D("0"), D("0.50"), is_taker=True) == D("0.00")


def test_fee_is_decimal_rounded_up_per_order():
    # A fee with sub-cent raw value must round UP to the next cent (Decimal boundary, no float).
    f = fill_fee(D("1"), D("0.50"), is_taker=True)  # raw = 0.07*1*0.25 = 0.0175 -> ceil = 0.02
    assert f == D("0.02")
    assert isinstance(f, Decimal)
