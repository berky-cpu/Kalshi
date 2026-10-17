"""Bitstamp `live_orders_{pair}` recorder (BRTI constituent).

feeds.md §3 (endpoint, subscribe envelope, and gap mechanism VERIFIED from the primary Bitstamp
WebSocket v2 doc, 2026-10-16). Order-by-order book keyed by order id. Events: ``order_created`` /
``order_changed`` / ``order_deleted``, each a frame with top-level ``event``, ``channel``,
``event_id`` (UUID string), ``pre_event_id`` (UUID string), and ``data`` {id, id_str,
order_type (0 = buy, 1 = sell), datetime, microtimestamp, amount, amount_str, amount_at_create,
amount_traded, price, price_str, order_source}.

**Gap detection (VERIFIED): the `pre_event_id` → `event_id` chain.** Each new event's
``pre_event_id`` must equal the previous event's ``event_id`` (exactly analogous to Binance
futures ``pu == prev.u``); a mismatch means a dropped event → resync. ``microtimestamp`` is kept
as a secondary monotonic-ordering sanity check (a regression still logs a gap), but the chain is
the primary signal. Resync path: REST order-book seed + ``POST /api/v2/order_data/`` ``since_id``
(MarketEventID) backfill.

NOTE: the ``microtimestamp``-sequenced "discard buffered events older than the snapshot" behaviour
belongs to the SEPARATE ``detail/diff_order_book`` channel — NOT ``live_orders`` — do not conflate.
``live_orders`` has no snapshot message; seed the book from the REST order book.
"""
from __future__ import annotations

from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, OrderIdBook, as_dict

ENDPOINT = "wss://ws.bitstamp.net"  # VERIFIED 2026-10-16 (Bitstamp WS v2 doc)
SUBSCRIBE_EVENT = "bts:subscribe"  # VERIFIED 2026-10-16
UNSUBSCRIBE_EVENT = "bts:unsubscribe"  # VERIFIED 2026-10-16
MAX_CHANNELS_PER_CONNECTION = 1024  # VERIFIED 2026-10-16

REST_ORDER_BOOK = "https://www.bitstamp.net/api/v2/order_book/{pair}/"  # seed (group=2 detail)
REST_ORDER_DATA = "https://www.bitstamp.net/api/v2/order_data/"  # since_id backfill (VERIFIED)

_EVENTS = {"order_created", "order_changed", "order_deleted"}


class BitstampProcessor(MessageProcessor):
    venue = "bitstamp"

    def __init__(self, product: str = "btcusd", channel: str | None = None, **kw: Any) -> None:
        channel = channel or f"live_orders_{product}"
        super().__init__(product=product, channel=channel, **kw)
        self.book = OrderIdBook()
        self.last_event_id: str | None = None  # primary chain anchor (UUID string)
        self.last_microtimestamp: int | None = None  # secondary ordering sanity check

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Seed from the REST detail order book: {"bids":[[price,amount,order_id],...], "asks":...}."""
        snap = as_dict(snapshot)
        self.book.clear()
        for row in snap.get("bids", []):
            self.book.add(str(row[2]), BID, row[0], row[1])
        for row in snap.get("asks", []):
            self.book.add(str(row[2]), ASK, row[0], row[1])
        self.synced = True
        self.needs_resnapshot = False
        # A fresh snapshot breaks the old chain; re-anchor on the next live event.
        self.last_event_id = None

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict):
            return []
        event = msg.get("event")
        if event == "bts:request_reconnect":
            # Bitstamp control event: it wants us to reconnect (and reseed).
            self._emit_gap("request_reconnect", recv_ts_ns)
            return []
        if event not in _EVENTS:
            return []  # subscription-succeeded / heartbeat frames
        data = msg.get("data") or {}
        event_id = msg.get("event_id")
        pre_event_id = msg.get("pre_event_id")

        # --- PRIMARY gap signal: pre_event_id must chain off the previous event_id ---
        chain_broke = False
        if self.last_event_id is not None:
            if pre_event_id is None:
                # Once anchored, a missing pre_event_id can't be verified -> treat as a gap
                # rather than silently re-anchoring (the doc says it's always present).
                self._emit_gap("missing pre_event_id", recv_ts_ns,
                               expected=self.last_event_id, got=None)
                chain_broke = True
            elif pre_event_id != self.last_event_id:
                self._emit_gap("event_id chain break", recv_ts_ns,
                               expected=self.last_event_id, got=pre_event_id)
                chain_broke = True
        if event_id is not None:
            self.last_event_id = event_id  # re-anchor (seeds on first event)

        # --- SECONDARY: microtimestamp monotonic sanity check. Only run when the chain check
        # didn't already flag this event, so one dropped event logs at most one gap. ---
        micro = _int_or_none(data.get("microtimestamp"))
        if (not chain_broke and micro is not None and self.last_microtimestamp is not None
                and micro < self.last_microtimestamp):
            self._emit_gap("microtimestamp regression", recv_ts_ns,
                           expected=f">{self.last_microtimestamp}", got=micro)
        if micro is not None:
            self.last_microtimestamp = micro

        self._apply(event, data)
        return [{"kind": "book", "event": event}]

    def _apply(self, event: str, data: dict) -> None:
        oid = str(data["id"])
        side = BID if int(data["order_type"]) == 0 else ASK
        if event == "order_created":
            self.book.add(oid, side, data["price"], data["amount"])
        elif event == "order_changed":
            self.book.modify(oid, qty=data.get("amount"), price=data.get("price"))
        elif event == "order_deleted":
            self.book.delete(oid)


def _int_or_none(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def subscribe_messages(product: str = "btcusd") -> list[dict]:
    return [{"event": SUBSCRIBE_EVENT, "data": {"channel": f"live_orders_{product}"}}]


def io_config(product: str = "btcusd") -> IoConfig:
    return IoConfig(
        venue="bitstamp",
        channel=f"live_orders_{product}",
        urls=[ENDPOINT],
        subscribe_messages=subscribe_messages(product),
        ping_interval_s=None,  # heartbeat/limits beyond 1024 channels/conn not needed here
    )
