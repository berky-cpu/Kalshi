"""Single-name / "mention" held-to-settlement resting-NO simulator (Candidate-2 study).

Tests the Bartlett & O'Hara (2026, NBER) claim that takers systematically OVERBUY YES on Kalshi
single-name/mention markets, so a passive maker resting on the NO side nets a positive per-contract
edge. The paper uses IDEALIZED fills (every taker trade fills the maker) and IGNORES fees. This
module stress-tests that under realistic frictions:

* **Back-of-queue fills.** A resting-NO maker at price ``p`` fills ONLY from YES-taker flow that
  reaches price ``p`` and clears the ``queue_ahead`` resting ahead of us, capped at our ``size``.
  Reuses the reviewed queue primitives in :mod:`kbtc.backtest.queue` (``apply_trade_at_level``):
  a YES-taker trade lifts resting NO liquidity (``consumed_side == "no"``), eats the queue ahead,
  and only the leftover fills us. This is the honesty the paper's idealized fill lacks.
* **Exact fees** via :mod:`kbtc.fees`. Mention series are ``fee_type="quadratic"``, maker-free
  (``fee_multiplier`` drives the TAKER curve only) — VERIFY per series via the API ``fee_type``
  before trusting the maker-free assumption. A resting maker pays $0; this module still routes the
  (zero) maker fee through ``fees`` so a non-maker-free series would be handled correctly.

HOW THIS DIFFERS from the intraday KXBTC15M engine (``backtest.engine``): there is no recorded
order book — the public REST API gives only the TRADE TAPE (taker side + both leg prices + size +
time) and the settlement ``result``. So we cannot reconstruct a live queue depth; ``queue_ahead``
is an ASSUMED parameter swept over {0, 5, 20, ...} (0 == the paper's frictionless/front-of-queue
idealization). P&L is held-to-settlement (``payoff_no - entry_price``), not a markout.

Pure functions only — no IO, no network, no order path. The file-backed study driver lives in
``notes/_candidate2_scripts/``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from kbtc.fees import maker_fee
from .queue import RestingOrder, apply_trade_at_level, apply_trade_through

ZERO = Decimal(0)
ONE = Decimal(1)
# A "size" large enough to never cap fills — the frictionless benchmark's unbounded resting depth.
UNBOUNDED = Decimal(10) ** 12


@dataclass(frozen=True)
class Trade:
    """One trade print from the public tape (sizes/prices exact Decimal; time in ns)."""

    ts_ns: int
    taker_side: str  # "yes" | "no" — the aggressor
    no_price: Decimal  # NO leg price (dollars, (0,1)); yes_price = 1 - no_price
    size: Decimal  # contracts
    is_block: bool = False

    @property
    def lifts_resting_no(self) -> bool:
        """A YES-taker crosses to buy YES, lifting resting NO liquidity (fills a resting-NO maker)."""
        return self.taker_side == "yes"


@dataclass(frozen=True)
class Fill:
    no_price: Decimal
    size: Decimal  # contracts filled at this price


@dataclass(frozen=True)
class MarketSimResult:
    ticker: str
    result_no: bool  # True if the market settled NO (good for a resting-NO holder)
    fills: tuple[Fill, ...]
    # Stylized-fact aggregates (ALL trades, incl. the ones we did not fill):
    yes_taker_vol: Decimal  # total contracts bought by YES takers
    no_taker_vol: Decimal  # total contracts bought by NO takers

    @property
    def filled_contracts(self) -> Decimal:
        return sum((f.size for f in self.fills), ZERO)

    def gross_pnl(self) -> Decimal:
        """Settlement P&L per our filled contracts, BEFORE fees (maker-free => == net)."""
        payoff = ONE if self.result_no else ZERO
        return sum(((payoff - f.no_price) * f.size for f in self.fills), ZERO)

    def fee(self, maker_fees_enabled: bool) -> Decimal:
        if not maker_fees_enabled:
            return Decimal("0.00")
        return sum((maker_fee(int(f.size.to_integral_value(rounding="ROUND_HALF_UP")),
                              f.no_price, True) for f in self.fills), Decimal("0.00"))

    def net_pnl(self, maker_fees_enabled: bool = False) -> Decimal:
        return self.gross_pnl() - self.fee(maker_fees_enabled)


def simulate_resting_no(
    ticker: str,
    trades: list[Trade],
    result_no: bool,
    *,
    queue_ahead: Decimal,
    size: Decimal,
    include_blocks: bool = False,
) -> MarketSimResult:
    """Simulate a passive maker resting ``size`` NO contracts at EVERY NO price level that trades.

    For each distinct NO price ``p`` touched by YES-taker flow we rest one order (``queue_ahead``
    contracts resting ahead of us, our ``size`` behind). We walk the YES-taker trades at ``p`` in
    time order; each eats the queue ahead first (``apply_trade_at_level``) and only the leftover
    fills us, up to ``size``. At-level fills only (a trade at ``p`` fills the order at ``p``) — no
    cross-level trade-through, so every taker contract is attributed to exactly one level and never
    double-counted. (Excluding trade-through is GENEROUS to the strategy: it omits the
    picked-off fills a falling NO bid would take as the market runs through it.)

    Frictionless / paper-idealized benchmark == ``queue_ahead=0, size=UNBOUNDED``: then every
    YES-taker contract fills us at its price.

    ``trades`` need not be sorted; we sort by ``ts_ns`` (stable). Block trades are excluded unless
    ``include_blocks`` (negotiated, not continuous-book maker-fillable).
    """
    ts = sorted(trades, key=lambda t: t.ts_ns)
    yes_vol = sum((t.size for t in ts if t.lifts_resting_no and (include_blocks or not t.is_block)), ZERO)
    no_vol = sum((t.size for t in ts if not t.lifts_resting_no and (include_blocks or not t.is_block)), ZERO)

    orders: dict[Decimal, RestingOrder] = {}
    fills_by_price: dict[Decimal, Decimal] = {}
    oid = 0
    for t in ts:
        if not t.lifts_resting_no:
            continue
        if t.is_block and not include_blocks:
            continue
        p = t.no_price
        o = orders.get(p)
        if o is None:
            oid += 1
            o = RestingOrder(order_id=oid, book_side="no", price=p, size=size,
                             submit_ts_ns=0, effective_ts_ns=0, is_taker=False,
                             queue_ahead=queue_ahead, active=True)
            orders[p] = o
        if o.removed or o.remaining <= ZERO:
            continue
        fe = apply_trade_at_level(o, t.size, t.ts_ns)
        if fe is not None:
            fills_by_price[p] = fills_by_price.get(p, ZERO) + fe.size

    fills = tuple(Fill(no_price=p, size=s) for p, s in sorted(fills_by_price.items()) if s > ZERO)
    return MarketSimResult(ticker=ticker, result_no=result_no, fills=fills,
                           yes_taker_vol=yes_vol, no_taker_vol=no_vol)


def simulate_single_entry(
    ticker: str,
    trades: list[Trade],
    result_no: bool,
    *,
    queue_ahead: Decimal,
    size: Decimal,
    include_blocks: bool = False,
) -> MarketSimResult:
    """STRICTLY-CAUSAL single-entry variant: rest ONE NO order of ``size`` at the NO price of the
    FIRST (non-block) trade in the market, joining the back of a ``queue_ahead`` queue at that
    instant. Fills come ONLY from LATER YES-taker trades, and INCLUDE adverse trade-through
    (``apply_trade_through``): a later YES-taker trade at a NO price BELOW ours means the market ran
    down through our bid (toward YES) and picks us off — the adverse-selection case the multi-level
    at-level model omits.

    This removes two generosities of :func:`simulate_resting_no`: (1) path-conditioned capacity
    (resting at every level that ever trades) and (2) ignoring pick-offs. Entry price = opening
    clear, a causal, non-look-ahead choice.
    """
    ts = sorted(trades, key=lambda t: t.ts_ns)
    usable = [t for t in ts if include_blocks or not t.is_block]
    yes_vol = sum((t.size for t in usable if t.lifts_resting_no), ZERO)
    no_vol = sum((t.size for t in usable if not t.lifts_resting_no), ZERO)
    if not usable:
        return MarketSimResult(ticker, result_no, (), yes_vol, no_vol)
    entry = usable[0]
    p = entry.no_price
    o = RestingOrder(order_id=1, book_side="no", price=p, size=size, submit_ts_ns=entry.ts_ns,
                     effective_ts_ns=entry.ts_ns, is_taker=False, queue_ahead=queue_ahead, active=True)
    filled = ZERO
    for t in usable:
        if t.ts_ns <= entry.ts_ns or not t.lifts_resting_no or o.removed or o.remaining <= ZERO:
            continue
        if t.no_price == p:
            fe = apply_trade_at_level(o, t.size, t.ts_ns)
        elif t.no_price < p:  # market ran through our NO bid (toward YES) -> adverse pick-off
            fe = apply_trade_through(o, t.ts_ns)
        else:
            fe = None  # a better (higher) NO bid than ours was lifted; does not reach us
        if fe is not None:
            filled += fe.size
    fills = (Fill(no_price=p, size=filled),) if filled > ZERO else ()
    return MarketSimResult(ticker, result_no, fills, yes_vol, no_vol)


def price_decile(no_price: Decimal) -> int:
    """Pre-registered NO-price bucket index 0..9 (0 => [0.00,0.10), ..., 9 => [0.90,1.00])."""
    d = int((no_price * 10).to_integral_value(rounding="ROUND_FLOOR"))
    return max(0, min(9, d))
