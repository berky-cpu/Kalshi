"""Unit tests for the pure back-of-queue fill model (kbtc.backtest.queue)."""
from decimal import Decimal

from kbtc.backtest.queue import (
    CancelPolicy,
    RestingOrder,
    apply_cancel_at_level,
    apply_trade_at_level,
    apply_trade_through,
)

D = Decimal


def make_order(size="10", price="0.40", side="yes", eff=0):
    o = RestingOrder(order_id=1, book_side=side, price=D(price), size=D(size),
                     submit_ts_ns=0, effective_ts_ns=eff)
    return o


def test_activate_joins_back_of_queue():
    o = make_order()
    o.activate(D("100"))
    assert o.queue_ahead == D("100")
    assert o.active and not o.removed


def test_trade_consumes_queue_before_filling():
    o = make_order(size="10")
    o.activate(D("100"))
    # A 60-contract trade only eats into the queue; we are still behind -> no fill.
    fe = apply_trade_at_level(o, D("60"), ts_ns=5)
    assert fe is None
    assert o.queue_ahead == D("40")
    assert o.filled == 0


def test_partial_then_full_fill():
    o = make_order(size="30")
    o.activate(D("100"))
    fe1 = apply_trade_at_level(o, D("110"), ts_ns=5)  # eats 100, 10 leftover -> partial 10
    assert fe1 is not None and fe1.size == D("10")
    assert o.queue_ahead == 0 and o.filled == D("10") and not o.removed
    fe2 = apply_trade_at_level(o, D("25"), ts_ns=6)  # queue 0 -> fill min(25,20)=20
    assert fe2 is not None and fe2.size == D("20")
    assert o.filled == D("30") and o.removed


def test_no_fill_once_fully_filled():
    o = make_order(size="5")
    o.activate(D("0"))
    assert apply_trade_at_level(o, D("5"), ts_ns=1).size == D("5")
    assert o.removed
    assert apply_trade_at_level(o, D("5"), ts_ns=2) is None


def test_trade_through_fills_all_remaining():
    o = make_order(size="10")
    o.activate(D("100"))  # big queue ahead
    fe = apply_trade_through(o, ts_ns=7)  # market traded through our price -> level cleared
    assert fe is not None and fe.size == D("10")
    assert o.queue_ahead == 0 and o.removed


def test_cancel_conservative_never_advances():
    o = make_order()
    o.activate(D("100"))
    apply_cancel_at_level(o, D("40"), D("100"), CancelPolicy.CONSERVATIVE)
    assert o.queue_ahead == D("100")


def test_cancel_pro_rata_advances_by_ahead_fraction():
    o = make_order()
    o.activate(D("100"))  # queue_ahead = 100, resting = 100 -> fraction 1.0
    apply_cancel_at_level(o, D("40"), D("100"), CancelPolicy.PRO_RATA)
    assert o.queue_ahead == D("60")


def test_cancel_pro_rata_fractional():
    o = make_order()
    o.activate(D("100"))
    o.queue_ahead = D("50")  # we are halfway; resting (excl us) is 200
    apply_cancel_at_level(o, D("40"), D("200"), CancelPolicy.PRO_RATA)
    # reduce = 40 * (50/200) = 10
    assert o.queue_ahead == D("40")


def test_cancel_pro_rata_capped_and_nonnegative():
    o = make_order()
    o.activate(D("10"))
    apply_cancel_at_level(o, D("1000"), D("10"), CancelPolicy.PRO_RATA)
    assert o.queue_ahead == D("0")
