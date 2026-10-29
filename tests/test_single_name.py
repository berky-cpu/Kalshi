"""Tests for the single-name/mention resting-NO held-to-settlement simulator."""
from __future__ import annotations

from decimal import Decimal

from kbtc.backtest.single_name import (
    UNBOUNDED,
    Trade,
    price_decile,
    simulate_resting_no,
    simulate_single_entry,
)

D = Decimal


def _t(ts, taker, no_price, size, block=False):
    return Trade(ts_ns=ts, taker_side=taker, no_price=D(no_price), size=D(size), is_block=block)


def test_frictionless_fills_every_yes_taker_contract():
    # Q=0, unbounded size => we fill every YES-taker contract at its NO price.
    trades = [
        _t(1, "yes", "0.30", "10"),
        _t(2, "yes", "0.30", "5"),
        _t(3, "no", "0.30", "99"),  # NO taker lifts resting YES, not us -> ignored
    ]
    r = simulate_resting_no("M", trades, result_no=True, queue_ahead=D(0), size=UNBOUNDED)
    assert r.filled_contracts == D(15)
    assert r.yes_taker_vol == D(15)
    assert r.no_taker_vol == D(99)
    # settled NO: payoff 1 each, entry 0.30 => +0.70 * 15
    assert r.gross_pnl() == D("0.70") * D(15)
    assert r.net_pnl() == r.gross_pnl()  # maker-free


def test_settled_yes_loses_for_resting_no():
    trades = [_t(1, "yes", "0.40", "10")]
    r = simulate_resting_no("M", trades, result_no=False, queue_ahead=D(0), size=UNBOUNDED)
    # payoff 0, entry 0.40 => -0.40 each
    assert r.gross_pnl() == D("-0.40") * D(10)


def test_queue_ahead_absorbs_flow_before_we_fill():
    # Q=20 ahead; only 15 YES-taker volume arrives -> we never fill.
    trades = [_t(1, "yes", "0.30", "10"), _t(2, "yes", "0.30", "5")]
    r = simulate_resting_no("M", trades, result_no=True, queue_ahead=D(20), size=UNBOUNDED)
    assert r.filled_contracts == D(0)
    # 25 arrives, Q=20 -> 5 leftover fills us (capped by unbounded size)
    trades2 = trades + [_t(3, "yes", "0.30", "10")]
    r2 = simulate_resting_no("M", trades2, result_no=True, queue_ahead=D(20), size=UNBOUNDED)
    assert r2.filled_contracts == D(5)


def test_capacity_cap_by_size():
    # Q=0 but size=5 -> at most 5 fill even though 30 YES-taker volume arrives at the level.
    trades = [_t(1, "yes", "0.25", "30")]
    r = simulate_resting_no("M", trades, result_no=True, queue_ahead=D(0), size=D(5))
    assert r.filled_contracts == D(5)
    assert r.fills[0].no_price == D("0.25")


def test_per_level_independent_orders():
    # Two distinct NO levels each get their own resting order (size 5, Q 0).
    trades = [_t(1, "yes", "0.20", "8"), _t(2, "yes", "0.60", "8")]
    r = simulate_resting_no("M", trades, result_no=True, queue_ahead=D(0), size=D(5))
    got = {f.no_price: f.size for f in r.fills}
    assert got == {D("0.20"): D(5), D("0.60"): D(5)}


def test_blocks_excluded_by_default():
    trades = [_t(1, "yes", "0.30", "100", block=True)]
    r = simulate_resting_no("M", trades, result_no=True, queue_ahead=D(0), size=UNBOUNDED)
    assert r.filled_contracts == D(0)
    assert r.yes_taker_vol == D(0)
    r2 = simulate_resting_no("M", trades, result_no=True, queue_ahead=D(0),
                             size=UNBOUNDED, include_blocks=True)
    assert r2.filled_contracts == D(100)


def test_maker_fee_zero_when_free_nonzero_when_enabled():
    trades = [_t(1, "yes", "0.50", "100")]
    r = simulate_resting_no("M", trades, result_no=True, queue_ahead=D(0), size=UNBOUNDED)
    assert r.fee(maker_fees_enabled=False) == D("0.00")
    # 0.0175 * 100 * 0.5 * 0.5 = 0.4375 -> ceil to 0.44
    assert r.fee(maker_fees_enabled=True) == D("0.44")


def test_price_decile_buckets():
    assert price_decile(D("0.00")) == 0
    assert price_decile(D("0.09")) == 0
    assert price_decile(D("0.10")) == 1
    assert price_decile(D("0.95")) == 9
    assert price_decile(D("0.999")) == 9


def test_single_entry_rests_at_first_trade_price_fills_later_flow():
    # Entry at first trade (no=0.40). Q=0, size=10. Later YES-taker flow at 0.40 fills us.
    trades = [_t(1, "yes", "0.40", "3"),   # entry (sets price) AND counts? no: entry trade excluded
              _t(2, "yes", "0.40", "4"),
              _t(3, "yes", "0.40", "5")]
    r = simulate_single_entry("M", trades, result_no=True, queue_ahead=D(0), size=D(10))
    # entry trade at ts=1 sets price; fills only from ts>1: 4+5=9
    assert r.filled_contracts == D(9)
    assert r.fills[0].no_price == D("0.40")


def test_single_entry_adverse_trade_through():
    # Entry at 0.50; a later trade at 0.30 (<0.50) runs through -> fills our whole remaining size.
    trades = [_t(1, "yes", "0.50", "1"), _t(2, "yes", "0.30", "1")]
    r = simulate_single_entry("M", trades, result_no=False, queue_ahead=D(0), size=D(5))
    assert r.filled_contracts == D(5)  # trade-through clears the rest
    # settled YES, entry 0.50 -> -0.50 each
    assert r.gross_pnl() == D("-0.50") * D(5)


def test_single_entry_higher_no_price_does_not_fill():
    # Entry at 0.50; later trade at 0.70 (>0.50) is a better NO bid lifted, not us.
    trades = [_t(1, "yes", "0.50", "1"), _t(2, "yes", "0.70", "100")]
    r = simulate_single_entry("M", trades, result_no=True, queue_ahead=D(0), size=D(5))
    assert r.filled_contracts == D(0)


def test_time_order_independence():
    # unsorted input must give the same fills as sorted
    trades = [_t(3, "yes", "0.30", "10"), _t(1, "yes", "0.30", "5"), _t(2, "yes", "0.30", "5")]
    r = simulate_resting_no("M", trades, result_no=True, queue_ahead=D(12), size=UNBOUNDED)
    assert r.filled_contracts == D(8)  # 20 total - 12 queue
