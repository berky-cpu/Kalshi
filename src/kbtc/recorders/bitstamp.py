"""Bitstamp `live_orders_{pair}` recorder (BRTI constituent).

feeds.md §3. Order-by-order book keyed by order id. Events: ``order_created`` /
``order_changed`` / ``order_deleted`` with ``order_type`` (0 = buy, 1 = sell), ``price``,
``amount``, ``microtimestamp`` (monotonic per channel), ``id``.

Gap handling (feeds.md §3): the ONLY live-stream gap signal is ``microtimestamp`` monotonicity
— feeds.md §3 says ``microtimestamp`` is monotonic per channel and that MarketEventID is the
REST-backfill ``since_id`` key; it does NOT say event ids increment by 1. So by default we run
only the ``microtimestamp``-regression check and leave MarketEventID purely as the backfill key.

An OPTIONAL +1-contiguity check on a MarketEventID field is available but **disabled by default**
(``event_id_field=None``): the WS v2 page is Incapsula-blocked, so the live field name carrying
the MarketEventID is unconfirmed AND it is unknown whether those ids are contiguous. Do not
enable it until both are confirmed — otherwise, pointing it at ``microtimestamp`` (a microsecond
clock, not a +1 counter) would fire a false gap on every message.

Everything below the "VERIFIED from REST primary" line (endpoint host, subscribe envelope,
event-id field name) is PARTIAL — see the UNVERIFIED markers — and is kept as configurable
constants so a corrected value needs no rewrite.
"""
from __future__ import annotations

from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, OrderIdBook, as_dict

# --- CONFIGURABLE / UNVERIFIED (ROADMAP 0.4 open item F-1) --------------------------
ENDPOINT = "wss://ws.bitstamp.net"  # UNVERIFIED (ROADMAP 0.4 open item): WS v2 page bot-walled
SUBSCRIBE_EVENT = "bts:subscribe"  # UNVERIFIED (ROADMAP 0.4 open item): envelope from mirror
UNSUBSCRIBE_EVENT = "bts:unsubscribe"  # UNVERIFIED (ROADMAP 0.4 open item)
# Optional WS field carrying the MarketEventID, for +1-contiguity gap detection. DISABLED by
# default (None): the REST backfill key is VERIFIED but the live WS field *name* is not (blocked
# page), nor is it known whether those ids are contiguous. Set to the real field name ONLY after
# both are confirmed. NEVER set this to "microtimestamp" — that is a microsecond clock, not a
# +1 counter, and would false-fire a gap on every message.
EVENT_ID_FIELD: str | None = None  # UNVERIFIED (ROADMAP 0.4 open item): keep disabled
# -----------------------------------------------------------------------------------

REST_ORDER_BOOK = "https://www.bitstamp.net/api/v2/order_book/{pair}/"  # seed (group=2 detail)
REST_ORDER_DATA = "https://www.bitstamp.net/api/v2/order_data/"  # since_id backfill (VERIFIED)

_EVENTS = {"order_created", "order_changed", "order_deleted"}


class BitstampProcessor(MessageProcessor):
    venue = "bitstamp"

    def __init__(self, product: str = "btcusd", channel: str | None = None,
                 event_id_field: str | None = EVENT_ID_FIELD, **kw: Any) -> None:
        channel = channel or f"live_orders_{product}"
        super().__init__(product=product, channel=channel, **kw)
        self.book = OrderIdBook()
        self.event_id_field = event_id_field
        self.last_microtimestamp: int | None = None
        self.last_event_id: int | None = None

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Seed from the REST detail order book: {"bids":[[price,amount,order_id],...], "asks":...}."""
        snap = as_dict(snapshot)
        self.book.clear()
        for row in snap.get("bids", []):
            px, amt, oid = row[0], row[1], row[2]
            self.book.add(str(oid), BID, px, amt)
        for row in snap.get("asks", []):
            px, amt, oid = row[0], row[1], row[2]
            self.book.add(str(oid), ASK, px, amt)
        self.synced = True
        self.needs_resnapshot = False

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

        # --- gap / ordering checks (never silent) ---
        micro = _int_or_none(data.get("microtimestamp"))
        if micro is not None and self.last_microtimestamp is not None and micro < self.last_microtimestamp:
            self._emit_gap("microtimestamp regression", recv_ts_ns,
                           expected=f">{self.last_microtimestamp}", got=micro)
            return []
        if micro is not None:
            self.last_microtimestamp = micro

        eid = _hex_or_int(data.get(self.event_id_field)) if self.event_id_field else None
        if eid is not None and self.last_event_id is not None and eid != self.last_event_id + 1:
            self._emit_gap("MarketEventID skip", recv_ts_ns,
                           expected=self.last_event_id + 1, got=eid)
            # fall through: still record latest id so we don't re-fire every message post-gap
        if eid is not None:
            self.last_event_id = eid

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


def _hex_or_int(x: Any) -> int | None:
    """MarketEventID is a hex id; microtimestamp is decimal. Accept either."""
    if x is None:
        return None
    if isinstance(x, int):
        return x
    s = str(x).strip()
    if s.startswith(("0x", "0X")) or any(c in "abcdefABCDEF" for c in s):
        try:
            return int(s, 16)
        except ValueError:
            return None
    try:
        return int(s, 10)
    except ValueError:
        return None


def subscribe_messages(product: str = "btcusd") -> list[dict]:
    return [{"event": SUBSCRIBE_EVENT, "data": {"channel": f"live_orders_{product}"}}]


def io_config(product: str = "btcusd") -> IoConfig:
    return IoConfig(
        venue="bitstamp",
        channel=f"live_orders_{product}",
        urls=[ENDPOINT],
        subscribe_messages=subscribe_messages(product),
        ping_interval_s=None,  # heartbeat/limits UNVERIFIED (feeds.md §3)
    )
