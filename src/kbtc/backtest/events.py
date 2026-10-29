"""Typed market-event stream for the backtest: parse raw Kalshi frames and merge with an index
series in strict ``recv_ts_ns`` order (no look-ahead).

The engine consumes these events and, in parallel, feeds the SAME raw frames to a
``KalshiProcessor`` so it always has the full reconstructed depth as-of the current event. The
typed events here expose exactly the per-level information the queue model needs that the
processor's net book does not retain on its own:

* ``BookDelta`` — the SIGNED size change at one ``(side, price)`` level. Needed to decompose a
  resting-size DECREASE into trade-fills (from the trade tape) vs. cancels (the remainder).
* ``Trade`` — the aggressor side + both dollar prices + size, from which the CONSUMED resting
  level ``(side, price)`` is derived unambiguously (a ``taker_side="no"`` trade lifts resting
  ``yes`` liquidity at ``yes_price``; ``taker_side="yes"`` lifts resting ``no`` liquidity at
  ``no_price``). The two prices are the trade's YES/NO legs (they sum to $1.00).
* ``BookSnapshot`` — a (re)baseline; the engine re-anchors our queue position on it.
* ``IndexTick`` — a fair-value / index input sample (float; no money crosses this type).

Prices and sizes are exact ``Decimal`` (dollars / contract counts); only the index level is a
float (probability math, per CLAUDE.md — Decimal only at the price/fee boundary).

Pure parsing/merging only — no IO, no network, no order path.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Iterator

from kbtc.recorders.base import as_dict
from kbtc.recorders.kalshi import _dec, _event_body

# Event-type ordering within one identical recv_ts_ns (stable, documented): index first (freshest
# fair value available before we act), then book deltas/snapshots, then trades. A trade at the same
# ns as a quote decision never fills an order that only becomes effective at/after that ns, so this
# ordering cannot create look-ahead; it only fixes a deterministic tie-break.
_INDEX, _SNAPSHOT, _DELTA, _TRADE = 0, 1, 2, 3


@dataclass(frozen=True)
class IndexTick:
    recv_ts_ns: int
    price: float  # index / fair-value input level (float; no money crosses this type)
    _order: int = _INDEX


@dataclass(frozen=True)
class BookSnapshot:
    recv_ts_ns: int
    ticker: str
    _order: int = _SNAPSHOT


@dataclass(frozen=True)
class BookDelta:
    recv_ts_ns: int
    ticker: str
    side: str  # "yes" | "no" (resting-bid side of the Kalshi book)
    price: Decimal  # dollars
    delta: Decimal  # SIGNED contract change at (side, price)
    _order: int = _DELTA


@dataclass(frozen=True)
class Trade:
    recv_ts_ns: int
    ticker: str
    taker_side: str  # "yes" | "no" — the aggressor's side
    yes_price: Decimal  # dollars (YES leg)
    no_price: Decimal  # dollars (NO leg); yes_price + no_price == 1.00
    size: Decimal  # contracts
    _order: int = _TRADE

    @property
    def consumed_side(self) -> str:
        """The resting book side this trade LIFTS (opposite the aggressor)."""
        return "no" if self.taker_side == "yes" else "yes"

    @property
    def consumed_price(self) -> Decimal:
        """The price on ``consumed_side`` at which resting liquidity was lifted."""
        return self.no_price if self.taker_side == "yes" else self.yes_price


MarketEvent = BookSnapshot | BookDelta | Trade
AnyEvent = IndexTick | MarketEvent

Record = tuple[int, Any]  # (recv_ts_ns, raw json str or dict)


def parse_kalshi_record(recv_ts_ns: int, raw: Any, default_ticker: str = "KXBTC15M") -> list[MarketEvent]:
    """Parse ONE raw Kalshi frame into zero or more typed market events (snapshot/delta/trade).

    Mirrors the verified schema used by ``recorders.kalshi``/``normalize.extract`` (payload nested
    under ``msg`` or flat; dollar-string prices; signed ``delta_fp``; fixed-point ``count_fp``).
    Non-book/non-trade frames (acks, lifecycle, heartbeats) yield ``[]``. Never raises on schema
    drift — a frame it cannot parse is skipped (the raw copy is authoritative elsewhere).
    """
    msg = as_dict(raw)
    if not isinstance(msg, dict):
        return []
    typ = msg.get("type")
    body = _event_body(msg)
    if not isinstance(body, dict):
        return []
    ticker = body.get("market_ticker", default_ticker)

    if typ == "orderbook_snapshot":
        return [BookSnapshot(recv_ts_ns=recv_ts_ns, ticker=ticker)]

    if typ == "orderbook_delta":
        side = body.get("side")
        if side not in ("yes", "no"):
            return []
        try:
            price = _dec(body["price_dollars"])
            delta = _dec(body["delta_fp"])
        except (KeyError, ValueError):
            return []
        return [BookDelta(recv_ts_ns=recv_ts_ns, ticker=ticker, side=side, price=price, delta=delta)]

    if typ == "trade":
        taker_side = body.get("taker_side")
        yp = body.get("yes_price_dollars")
        np_ = body.get("no_price_dollars")
        size = body.get("count_fp")
        if taker_side not in ("yes", "no") or yp is None or size is None:
            return []
        try:
            yes_price = _dec(yp)
            no_price = _dec(np_) if np_ is not None else (Decimal(1) - yes_price)
            sz = _dec(size)
        except ValueError:
            return []
        return [Trade(recv_ts_ns=recv_ts_ns, ticker=ticker, taker_side=taker_side,
                      yes_price=yes_price, no_price=no_price, size=sz)]

    return []


def iter_kalshi_events(records: Iterable[Record], default_ticker: str = "KXBTC15M") -> Iterator[MarketEvent]:
    """Yield typed market events from raw ``(recv_ts_ns, raw)`` Kalshi records, in input order."""
    for recv, raw in records:
        yield from parse_kalshi_record(recv, raw, default_ticker)


def merge_events(market_events: Iterable[MarketEvent],
                 index_ticks: Iterable[IndexTick]) -> list[AnyEvent]:
    """Merge market events and index ticks into one list sorted strictly by ``recv_ts_ns``.

    Ties on ``recv_ts_ns`` break by event type (index < snapshot < delta < trade); within the same
    ``(ts, type)`` the input order is preserved (Python's sort is stable). This is the single
    no-look-ahead ordering seam: every consumer sees events only in non-decreasing time.
    """
    merged: list[AnyEvent] = list(market_events) + list(index_ticks)
    merged.sort(key=lambda e: (e.recv_ts_ns, e._order))
    return merged
