"""Back-of-queue fill model (pure). The load-bearing core of the simulator.

A resting order joins the BACK of the resting size at its price when it becomes effective (after
submit latency). ``queue_ahead`` is the number of contracts resting ahead of us at that exact
``(side, price)`` level at the instant we join — everything already there. It shrinks only via:

* **Observed trades at our level** — a trade that lifts ``consumed_price == our price`` on
  ``consumed_side == our side`` eats the front of the queue; any volume beyond ``queue_ahead``
  fills us (this is the "fill only when queue ahead reaches zero and a trade crosses" rule, and it
  supports PARTIAL fills).
* **Cancels at our level** — inferred by the engine as the part of a resting-size DECREASE not
  attributable to a trade. Two policies: ``CONSERVATIVE`` assumes cancels happen BEHIND us (never
  advances our queue — also sidesteps the book-delta/trade ordering ambiguity) and ``PRO_RATA``
  assumes a cancel is equally likely anywhere in the resting size, advancing our queue by the
  fraction ahead of us.
* **A trade THROUGH our level** — a trade that lifts a price strictly WORSE (lower, since both
  Kalshi book sides are bids: higher price = higher priority) than ours can only occur under
  price-time priority once everything at our price and better, INCLUDING us, has been consumed. So
  it fills our entire remaining size. In ``FillMode.TRADE_THROUGH`` this is the ONLY thing that
  fills us (same-price trades never do) — the most conservative mode.

All quantities are exact ``Decimal`` (contracts); no look-ahead lives here — the engine only calls
these functions for trade/cancel events strictly after the order's effective time.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

ZERO = Decimal(0)


class FillMode(str, Enum):
    QUEUE = "queue"  # fill when queue-ahead hits zero and a trade crosses (default)
    TRADE_THROUGH = "trade_through"  # fill ONLY on a trade through our price (most conservative)


class CancelPolicy(str, Enum):
    """How an inferred cancel advances our queue.

    ``CONSERVATIVE`` (the 4.2 GATE DEFAULT) assumes cancels happen BEHIND us: it never advances our
    queue and is genuinely IMMUNE to the book-delta/trade ordering ambiguity (a decrease is only
    ever credited as a fill via the trade tape).

    ``PRO_RATA`` is an OPTIMISTIC UPPER BOUND ONLY and MUST NOT back any accepted 4.2 number. If a
    book delta arrives BEFORE its trade, the decrease is classed as a cancel (``_pending_trade`` is
    still 0) and PRO_RATA advances our queue, then the trade advances/fills again — double-counting
    and overstating fills. Use it solely to bracket how much queue-position assumptions matter.
    """

    CONSERVATIVE = "conservative"  # cancels behind us: never advance our queue (gate default)
    PRO_RATA = "pro_rata"  # OPTIMISTIC upper bound only — see class docstring; never a gate number


@dataclass
class RestingOrder:
    """Our simulated maker order. Never sent anywhere — a pure bookkeeping object.

    ``book_side`` is the Kalshi resting-bid side we sit on: ``yes`` = we are buying YES at
    ``price``; ``no`` = we are buying NO at ``price`` (equivalently selling YES at ``1 - price``).
    ``queue_ahead`` is ``None`` until the order becomes effective (joins the book); the engine then
    snapshots it from the reconstructed resting size at the level.
    """

    order_id: int
    book_side: str  # "yes" | "no"
    price: Decimal  # dollars
    size: Decimal  # contracts requested
    submit_ts_ns: int  # decision time t (policy emitted it here)
    effective_ts_ns: int  # t + submit latency (joins the book here)
    is_taker: bool = False  # True only if the policy crossed (marketable); maker-free otherwise
    queue_ahead: Decimal | None = None
    filled: Decimal = ZERO
    active: bool = False  # became effective and not yet removed
    cancel_effective_ts_ns: int | None = None  # set when a cancel is requested + latency applied
    removed: bool = False  # cancelled (cancel took effect) or fully filled

    @property
    def remaining(self) -> Decimal:
        return self.size - self.filled

    def activate(self, resting_size: Decimal) -> None:
        """Join the back of the queue: ``queue_ahead`` = everything currently resting at the level."""
        if self.active:
            return
        self.queue_ahead = resting_size if resting_size > ZERO else ZERO
        self.active = True


@dataclass(frozen=True)
class FillEvent:
    order_id: int
    ts_ns: int
    book_side: str
    price: Decimal
    size: Decimal  # contracts filled in THIS event (supports partials)
    is_taker: bool


def apply_trade_at_level(order: RestingOrder, trade_size: Decimal, ts_ns: int) -> FillEvent | None:
    """A trade lifts ``trade_size`` contracts AT our level. Consume the queue ahead, then fill us.

    Returns a :class:`FillEvent` for the contracts filled in this trade (``None`` if none). Mutates
    ``order.queue_ahead`` and ``order.filled``. Caller guarantees the order is active and the trade
    is strictly after the order's effective time.
    """
    if order.queue_ahead is None or not order.active or order.remaining <= ZERO:
        return None
    eaten = min(order.queue_ahead, trade_size)
    order.queue_ahead -= eaten
    leftover = trade_size - eaten
    if leftover <= ZERO:
        return None
    fill = min(leftover, order.remaining)
    order.filled += fill
    if order.remaining <= ZERO:
        order.removed = True
    return FillEvent(order.order_id, ts_ns, order.book_side, order.price, fill, order.is_taker)


def apply_trade_through(order: RestingOrder, ts_ns: int) -> FillEvent | None:
    """A trade lifted a price strictly WORSE than ours → our level fully cleared → fill the rest.

    Under price-time priority a worse-priced trade implies everything at/above our price (our queue
    and us) was consumed, so our entire remaining size fills. Returns the fill (``None`` if nothing
    remains). This is the only fill path in :data:`FillMode.TRADE_THROUGH`.
    """
    if not order.active or order.remaining <= ZERO:
        return None
    order.queue_ahead = ZERO
    fill = order.remaining
    order.filled += fill
    order.removed = True
    return FillEvent(order.order_id, ts_ns, order.book_side, order.price, fill, order.is_taker)


def apply_cancel_at_level(order: RestingOrder, cancel_size: Decimal, resting_size_before: Decimal,
                          policy: CancelPolicy) -> None:
    """Advance our queue for a CANCEL of ``cancel_size`` contracts at our level.

    ``resting_size_before`` is the market resting size at the level (excluding our phantom order)
    just before the cancel. ``CONSERVATIVE`` never advances us; ``PRO_RATA`` advances by the share
    of resting size that is ahead of us (``queue_ahead / resting_size_before``), capped at
    ``queue_ahead``.
    """
    if order.queue_ahead is None or not order.active or order.queue_ahead <= ZERO:
        return
    if policy is CancelPolicy.CONSERVATIVE:
        return
    if resting_size_before <= ZERO:
        order.queue_ahead = ZERO
        return
    ahead_fraction = order.queue_ahead / resting_size_before
    reduction = min(order.queue_ahead, cancel_size * ahead_fraction)
    order.queue_ahead -= reduction
    if order.queue_ahead < ZERO:
        order.queue_ahead = ZERO
