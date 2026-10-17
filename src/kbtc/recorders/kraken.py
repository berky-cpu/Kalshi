"""Kraken WebSocket v2 `level3` recorder (BRTI constituent).

feeds.md §2. Order-by-order book keyed by ``order_id``. There is **NO sequence number**;
integrity is a **CRC32 `checksum`** carried on every snapshot and update. After applying a
message we recompute the CRC32 over the top-10 price levels per side (see ``base.kraken_crc32``)
and compare; a mismatch means the local book diverged → resync by resubscribing with
``snapshot: true``.

After each update the book is truncated to the subscribed ``depth`` per side: Kraken sends no
delete events for orders that fall out of the top-N, so the recorder drops them itself.

Auth: a REST ``GetWebSocketsToken`` token (from env-derived API key/secret) goes in every
subscribe; it grants connection access to read market data only — never to trade.
Endpoint host is given three ways across Kraken's own docs (feeds.md F-5) → try in order.
"""
from __future__ import annotations

from typing import Any

from .base import (
    ASK,
    BID,
    IoConfig,
    MessageProcessor,
    OrderIdBook,
    as_dict,
    kraken_crc32,
)

# feeds.md F-5: three-way host conflict; prefer the level3-page value, then auth, then public.
ENDPOINTS = [
    "wss://ws-l3.kraken.com/v2",
    "wss://ws-auth.kraken.com/v2",
    "wss://ws.kraken.com/v2",
]
REST_TOKEN = "https://api.kraken.com/0/private/GetWebSocketsToken"


class KrakenProcessor(MessageProcessor):
    venue = "kraken"

    def __init__(self, product: str = "BTC/USD", channel: str = "level3",
                 depth: int = 10, **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self.depth = depth
        self.book = OrderIdBook()

    def begin_resync(self) -> None:
        """In-band resync: drop ``synced`` and set ``awaiting_snapshot`` so checksum-failing
        updates arriving before the resubscribe snapshot do NOT re-arm resync (no frame thrash
        against Kraken's ~150-reconnect/10-min cap)."""
        self.synced = False
        self.awaiting_snapshot = True

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict) or msg.get("channel") != "level3":
            return []  # heartbeat / status / subscribe-ack frames
        mtype = msg.get("type")
        # Suppress pre-snapshot updates after a resubscribe: only the fresh snapshot resyncs.
        if self.awaiting_snapshot and mtype != "snapshot":
            return []
        data = msg.get("data") or []
        events: list[dict] = []
        for entry in data:
            if mtype == "snapshot":
                self._apply_snapshot_entry(entry)
            elif mtype == "update":
                self._apply_update_entry(entry)
            else:
                continue
            self._truncate()
            if not self._verify(entry, recv_ts_ns):
                break  # gap emitted; stop applying this frame, resync pending
            events.append({"kind": "book", "type": mtype})
        return events

    def _apply_snapshot_entry(self, entry: dict) -> None:
        self.book.clear()
        for o in entry.get("bids", []):
            self.book.add(str(o["order_id"]), BID, o["limit_price"], o["order_qty"])
        for o in entry.get("asks", []):
            self.book.add(str(o["order_id"]), ASK, o["limit_price"], o["order_qty"])
        self.synced = True
        self.awaiting_snapshot = False  # the fresh snapshot landed; resume normal checks

    def _apply_update_entry(self, entry: dict) -> None:
        for side, label in ((entry.get("bids", []), BID), (entry.get("asks", []), ASK)):
            for e in side:
                ev, oid = e["event"], str(e["order_id"])
                if ev == "add":
                    self.book.add(oid, label, e["limit_price"], e["order_qty"])
                elif ev == "modify":
                    self.book.modify(oid, qty=e["order_qty"], price=e.get("limit_price"))
                elif ev == "delete":
                    self.book.delete(oid)

    def _truncate(self) -> None:
        """Drop orders whose price level has fallen out of the top-``depth`` per side."""
        for side, rev in ((BID, True), (ASK, False)):
            prices = sorted(self.book.grouped(side).keys(), reverse=rev)
            keep = set(prices[: self.depth])
            for oid in [o_id for o_id, o in self.book.orders.items()
                        if o.side == side and o.price not in keep]:
                self.book.delete(oid)

    def _verify(self, entry: dict, recv_ts_ns: int) -> bool:
        want = entry.get("checksum")
        if want is None:
            # feeds.md §2: every snapshot/update carries a checksum. A missing one means we
            # cannot validate book integrity -> treat as a gap and resync (never silent).
            self._emit_gap("missing checksum", recv_ts_ns)
            return False
        got = kraken_crc32(self.book)
        if int(want) != got:
            self._emit_gap("checksum mismatch", recv_ts_ns, expected=int(want), got=got)
            return False
        return True

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Resync entry point: feed a fresh ``snapshot`` frame (from resubscribe snapshot:true)."""
        self.needs_resnapshot = False
        self.process(snapshot, recv_ts_ns or 0)


def subscribe_messages(product: str = "BTC/USD", depth: int = 10,
                       token: str = "<token>") -> list[dict]:
    return [{
        "method": "subscribe",
        "params": {"channel": "level3", "symbol": [product], "depth": depth,
                   "snapshot": True, "token": token},
        "req_id": 1,
    }]


def unsubscribe_messages(product: str = "BTC/USD", *, token: str) -> list[dict]:
    return [{
        "method": "unsubscribe",
        "params": {"channel": "level3", "symbol": [product], "token": token},
    }]


def resubscribe_messages(product: str = "BTC/USD", depth: int = 10, *, token: str) -> list[dict]:
    """Frames that pull a fresh ``type:"snapshot"`` (resync on checksum mismatch / disconnect).

    feeds.md §2: unsubscribe then resubscribe with ``snapshot: true``; the fresh snapshot frame
    then rebuilds the book via the processor's normal ``process`` path (resync PARTIAL — Kraken
    does not name this as a procedure, it is inferred from the checksum-mismatch behaviour).
    ``token`` is REQUIRED (no placeholder) so a mis-wire fails loudly rather than gapping forever.
    """
    return [*unsubscribe_messages(product, token=token),
            *subscribe_messages(product, depth, token)]


def io_config(product: str = "BTC/USD", depth: int = 10, token: str = "<token>") -> IoConfig:
    return IoConfig(
        venue="kraken",
        channel="level3",
        urls=ENDPOINTS,
        subscribe_messages=subscribe_messages(product, depth, token),
        ping_message='{"method":"ping"}',
        ping_interval_s=30.0,  # idle connections cut after ~1 min (feeds.md §2)
    )
