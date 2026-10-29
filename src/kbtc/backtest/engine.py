"""Event-driven, queue-aware backtest engine (ROADMAP 4.1).

Replays recorded Kalshi ``orderbook_delta`` + ``trade`` frames (full depth, via the verified
``KalshiProcessor``) merged with an index/fair-value series in strict ``recv_ts_ns`` order, runs a
policy to produce quote intents, and simulates back-of-queue maker fills with explicit latency and
exact fees. OFFLINE only — no order path of any kind.

No look-ahead is structural:
  * the policy at decision time ``t`` sees a :class:`MarketState` built only from events already
    applied (``recv_ts_ns <= t``);
  * a submitted order becomes effective at ``t + submit_latency`` and its ``queue_ahead`` is
    snapshotted from the book as-of that instant (the book BEFORE the first later event);
  * an order can only fill on trade events with ``recv_ts_ns > effective_ts_ns`` (strictly after it
    joined — back of queue at the same instant).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Callable, Iterable

from kbtc.recorders.kalshi import KalshiProcessor

from . import events as ev
from .feeapply import fill_fee
from .policy import MarketState, Policy, QuoteIntent
from .queue import (
    CancelPolicy,
    FillEvent,
    FillMode,
    RestingOrder,
    apply_cancel_at_level,
    apply_trade_at_level,
    apply_trade_through,
)

ZERO = Decimal(0)
NS_PER_S = 1_000_000_000
# Sentinel default ticker for parsing (never equals a real market_ticker) — see run().
_FOREIGN_TICKER = "\x00__not_our_market__"

# A fair-value provider enriches the causal MarketState with (prob_up, delta_x, sigma) before the
# policy sees it — wiring the index replica / 3.1 vol / 3.2 fair value WITHOUT coupling the engine to
# any of them (it only sees the tuple). It is handed the base state (built from events <= t), so it
# too is strictly causal. ``None`` entries mean "unavailable" and a model policy should pull.
FairValueFn = Callable[["MarketState"], "tuple[float | None, float | None, float | None]"]


@dataclass(frozen=True)
class BacktestMarket:
    """Settlement ground truth + timing for one KXBTC15M market (from recorded results)."""

    ticker: str
    open_time_ns: int
    close_time_ns: int
    strike: Decimal | None = None
    result_yes: bool | None = None  # official outcome; None if not yet settled/unknown
    expiration_value: Decimal | None = None


@dataclass(frozen=True)
class BacktestConfig:
    """Latency + queue/fill assumptions. Defaults are PLACEHOLDERS to be swept in 4.2."""

    submit_latency_ns: int = 50_000_000  # decision -> order effective (50 ms placeholder)
    cancel_latency_ns: int = 50_000_000  # cancel request -> effective (50 ms placeholder)
    fill_mode: FillMode = FillMode.QUEUE
    cancel_policy: CancelPolicy = CancelPolicy.CONSERVATIVE
    requote_on_trade: bool = True  # re-run the policy after trade events too


@dataclass
class FillRecord:
    order_id: int
    ts_ns: int
    book_side: str  # "yes" | "no"
    price: Decimal  # leg price in dollars
    size: Decimal  # contracts filled
    fee: Decimal  # dollars
    is_taker: bool
    yes_mid_at_fill: Decimal | None
    index_at_fill: float | None
    inventory_after: int  # net YES contracts after this fill


@dataclass
class BacktestResult:
    market: BacktestMarket
    config: BacktestConfig
    fills: list[FillRecord] = field(default_factory=list)
    inventory_path: list[tuple[int, int]] = field(default_factory=list)  # (ts, net YES)
    mid_series: list[tuple[int, Decimal]] = field(default_factory=list)  # (ts, yes_mid)
    index_series: list[tuple[int, float]] = field(default_factory=list)  # (ts, level)

    @property
    def fill_count(self) -> int:
        return len(self.fills)

    @property
    def total_fee(self) -> Decimal:
        return sum((f.fee for f in self.fills), ZERO)

    @property
    def final_inventory(self) -> int:
        return self.inventory_path[-1][1] if self.inventory_path else 0


class _Engine:
    def __init__(self, market: BacktestMarket, policy: Policy, config: BacktestConfig,
                 fair_value_fn: FairValueFn | None = None) -> None:
        self.market = market
        self.policy = policy
        self.cfg = config
        self.fair_value_fn = fair_value_fn
        self.proc = KalshiProcessor(product=market.ticker)
        self.result = BacktestResult(market=market, config=config)
        self._next_id = 0
        self.orders: dict[int, RestingOrder] = {}  # live (pending or active, not removed)
        # Pending trade volume per (side, price) level, used to classify a later resting-size
        # DECREASE as trade-fills vs. cancels (see module/queue docstrings). Default conservative
        # cancel policy is immune to the trade/delta ordering ambiguity.
        self._pending_trade: dict[tuple[str, Decimal], Decimal] = {}
        self._index_level: float | None = None

    # -- book helpers -------------------------------------------------------------------
    def _book(self) -> dict:
        return self.proc.books.get(self.market.ticker, {})

    def _resting_size(self, side: str, price: Decimal) -> Decimal:
        return self._book().get(side, {}).get(price, ZERO)

    def _best(self, side: str) -> tuple[Decimal | None, Decimal | None]:
        levels = self._book().get(side, {})
        if not levels:
            return None, None
        p = max(levels)
        return p, levels[p]

    def _yes_mid(self) -> Decimal | None:
        yp, _ = self._best("yes")
        np_, _ = self._best("no")
        if yp is None or np_ is None:
            return None
        return (yp + (Decimal(1) - np_)) / 2

    # -- order lifecycle ----------------------------------------------------------------
    def _activate_and_expire(self, now_ns: int) -> None:
        """Activate orders whose effective time has arrived (snapshot queue_ahead from the CURRENT
        book, i.e. as-of the time just before the next event), then remove cancelled orders."""
        for o in self.orders.values():
            if not o.active and not o.removed and o.effective_ts_ns <= now_ns:
                o.activate(self._resting_size(o.book_side, o.price))
        for oid, o in list(self.orders.items()):
            if o.cancel_effective_ts_ns is not None and o.cancel_effective_ts_ns <= now_ns:
                o.removed = True
            if o.removed:
                self.orders.pop(oid, None)

    def _record_fill(self, fe: FillEvent) -> None:
        price = fe.price
        fee = fill_fee(fe.size, price, fe.is_taker)
        # Inventory: a yes-side fill buys YES (+); a no-side fill buys NO (= short YES, -).
        qty = int(fe.size.to_integral_value(rounding="ROUND_HALF_UP"))
        inv_prev = self.result.inventory_path[-1][1] if self.result.inventory_path else 0
        inv = inv_prev + (qty if fe.book_side == "yes" else -qty)
        self.result.inventory_path.append((fe.ts_ns, inv))
        self.result.fills.append(FillRecord(
            order_id=fe.order_id, ts_ns=fe.ts_ns, book_side=fe.book_side, price=price,
            size=fe.size, fee=fee, is_taker=fe.is_taker, yes_mid_at_fill=self._yes_mid(),
            index_at_fill=self._index_level, inventory_after=inv,
        ))

    # -- event handlers -----------------------------------------------------------------
    def _on_trade(self, t: ev.Trade) -> None:
        # SEV-1 ticker guard: the Kalshi stream is multi-ticker with a SHARED per-sid seq (all
        # orderbook_delta on one sid, all trades on another), so we MUST feed the FULL stream to
        # ``self.proc`` for seq contiguity + correct per-ticker book reconstruction — but we may only
        # ACT on OUR market's events. All ~6 concurrent markets share traded price levels, so without
        # this guard a foreign trade at e.g. yes@0.50 would fill/advance our resting order.
        if t.ticker != self.market.ticker:
            return
        side = t.consumed_side
        cp = t.consumed_price
        # Remember this lift so the matching resting-size decrease is not mis-read as a cancel.
        self._pending_trade[(side, cp)] = self._pending_trade.get((side, cp), ZERO) + t.size
        for o in list(self.orders.values()):
            if not o.active or o.book_side != side or o.removed:
                continue
            if o.effective_ts_ns >= t.recv_ts_ns:  # joined at/after the trade: behind it
                continue
            fe: FillEvent | None = None
            if cp == o.price:  # trade AT our level
                if self.cfg.fill_mode is FillMode.QUEUE:
                    fe = apply_trade_at_level(o, t.size, t.recv_ts_ns)
                # TRADE_THROUGH: same-price trades never fill (handled below only on a through).
            elif cp < o.price:  # trade THROUGH us (worse price): our level fully cleared
                fe = apply_trade_through(o, t.recv_ts_ns)
            # cp > o.price: a better-priced level ahead of us traded — does not touch our queue.
            if fe is not None:
                self._record_fill(fe)
            if o.removed:
                self.orders.pop(o.order_id, None)

    def _on_delta(self, d: ev.BookDelta) -> None:
        if d.ticker != self.market.ticker:  # SEV-1: act only on our market (see _on_trade)
            return
        mid = self._yes_mid()
        if mid is not None:  # SEV-3: never store a None->0 mid (LOCF must carry the last VALID mid)
            self.result.mid_series.append((d.recv_ts_ns, mid))
        if d.delta >= ZERO:
            return  # adds join BEHIND us; no queue advance
        magnitude = -d.delta
        key = (d.side, d.price)
        pend = self._pending_trade.get(key, ZERO)
        trade_part = min(magnitude, pend)
        self._pending_trade[key] = pend - trade_part
        cancel_part = magnitude - trade_part
        if cancel_part <= ZERO:
            return
        # resting size just BEFORE this decrease (proc already applied the delta).
        size_before = self._resting_size(d.side, d.price) + magnitude
        for o in self.orders.values():
            if o.active and not o.removed and o.book_side == d.side and o.price == d.price:
                apply_cancel_at_level(o, cancel_part, size_before, self.cfg.cancel_policy)

    def _on_snapshot(self, s: ev.BookSnapshot) -> None:
        if s.ticker != self.market.ticker:  # SEV-1: act only on our market (see _on_trade)
            return
        # A (re)snapshot means a resync/gap: our prior queue estimate is stale. Conservatively
        # re-join the back of the (new) resting size at each active order's level.
        self._pending_trade.clear()
        for o in self.orders.values():
            if o.active and not o.removed:
                o.queue_ahead = self._resting_size(o.book_side, o.price)
        mid = self._yes_mid()
        if mid is not None:  # SEV-3: skip None mids (see _on_delta)
            self.result.mid_series.append((s.recv_ts_ns, mid))

    # -- policy / requote ---------------------------------------------------------------
    def _build_state(self, now_ns: int) -> MarketState:
        yp, ysz = self._best("yes")
        np_, nsz = self._best("no")
        inv = self.result.inventory_path[-1][1] if self.result.inventory_path else 0
        tau_s = max((self.market.close_time_ns - now_ns) / NS_PER_S, 0.0)
        return MarketState(
            recv_ts_ns=now_ns, ticker=self.market.ticker, tau_s=tau_s,
            best_yes_price=yp, best_yes_size=ysz, best_no_price=np_, best_no_size=nsz,
            index_level=self._index_level, fair_prob_up=None, delta_x=None, sigma=None,
            inventory=inv,
        )

    def _requote(self, now_ns: int) -> None:
        state = self._build_state(now_ns)
        if self.fair_value_fn is not None:
            prob, dx, sig = self.fair_value_fn(state)
            state = replace(state, fair_prob_up=prob, delta_x=dx, sigma=sig)
        intents = self.policy.quotes(state)
        wanted = {(i.side, i.price): i for i in intents}
        live_keys = set()
        for o in self.orders.values():
            if o.removed:
                continue
            key = (o.book_side, o.price)
            live_keys.add(key)
            if key not in wanted and o.cancel_effective_ts_ns is None:
                o.cancel_effective_ts_ns = now_ns + self.cfg.cancel_latency_ns
        for key, intent in wanted.items():
            if key in live_keys:
                continue  # already resting / pending at this (side, price)
            self._submit(intent, now_ns)

    def _submit(self, intent: QuoteIntent, now_ns: int) -> None:
        self._next_id += 1
        self.orders[self._next_id] = RestingOrder(
            order_id=self._next_id, book_side=intent.side, price=intent.price, size=intent.size,
            submit_ts_ns=now_ns, effective_ts_ns=now_ns + self.cfg.submit_latency_ns,
            is_taker=intent.is_taker,
        )

    # -- main loop ----------------------------------------------------------------------
    def run(self, kalshi_records: Iterable[ev.Record], index_ticks: Iterable[ev.IndexTick]) -> BacktestResult:
        kr = list(kalshi_records)
        it = sorted(index_ticks, key=lambda x: x.recv_ts_ns)
        i = j = 0
        while i < len(kr) or j < len(it):
            k_ts = kr[i][0] if i < len(kr) else None
            x_ts = it[j].recv_ts_ns if j < len(it) else None
            take_index = x_ts is not None and (k_ts is None or x_ts <= k_ts)
            now_ns = x_ts if take_index else k_ts
            self._activate_and_expire(now_ns)
            if take_index:
                tick = it[j]; j += 1
                self._index_level = tick.price
                self.result.index_series.append((tick.recv_ts_ns, tick.price))
                self._requote(now_ns)
                continue
            recv, raw = kr[i]; i += 1
            # Apply to the processor FIRST so the reconstructed book reflects this frame, then act
            # on the typed event(s). (Trade frames do not mutate the book; their resting-size
            # effect arrives as a separate delta.)
            self.proc.process(raw, recv)
            # Parse with a non-matching default ticker so a frame MISSING ``market_ticker`` (verified
            # frames always carry it; malformed ones might not) is NOT mis-attributed to our market.
            for e in ev.parse_kalshi_record(recv, raw, _FOREIGN_TICKER):
                ours = e.ticker == self.market.ticker  # SEV-1: only act/requote on our market
                if isinstance(e, ev.Trade):
                    self._on_trade(e)
                    if ours and self.cfg.requote_on_trade:
                        self._requote(now_ns)
                elif isinstance(e, ev.BookDelta):
                    self._on_delta(e)
                    if ours:
                        self._requote(now_ns)
                elif isinstance(e, ev.BookSnapshot):
                    self._on_snapshot(e)
                    if ours:
                        self._requote(now_ns)
        # Final inventory marker so inventory_path always ends at the last state.
        if not self.result.inventory_path:
            self.result.inventory_path.append((self.market.close_time_ns, 0))
        return self.result


def run_backtest(market: BacktestMarket, policy: Policy,
                 kalshi_records: Iterable[ev.Record],
                 index_ticks: Iterable[ev.IndexTick] | None = None,
                 config: BacktestConfig | None = None,
                 fair_value_fn: FairValueFn | None = None) -> BacktestResult:
    """Run one market through the queue-aware simulator. ``kalshi_records`` are ``(recv_ts_ns, raw)``
    pairs from the recorded ``kalshi/market`` stream (in arrival order); ``index_ticks`` is the
    fair-value/index input series (optional); ``fair_value_fn`` optionally enriches each causal state
    with ``(prob_up, delta_x, sigma)`` for a model policy. OFFLINE only — no order path."""
    return _Engine(market, policy, config or BacktestConfig(), fair_value_fn).run(
        kalshi_records, index_ticks or [])
