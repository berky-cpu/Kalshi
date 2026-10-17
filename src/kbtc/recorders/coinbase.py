"""Coinbase Exchange `full` / `level3` recorder (BRTI constituent).

feeds.md §1. Order-by-order book keyed by order id. Gap detection is on **`sequence`**
(per product, +1 per message); a jump > 1 means a dropped message → resnapshot via REST
``GET /products/BTC-USD/book?level=3``, discarding replayed messages with
``sequence <= snapshot sequence``. A lower-than-expected sequence is a dup/out-of-order and is
ignored.

Auth (`full`/`level3` require it since 2023-07-01): HMAC-SHA256 over
``timestamp + "GET" + "/users/self/verify"`` with the base64-decoded secret; key/secret/
passphrase come from env vars ONLY and are used solely to subscribe to market data. No order
path exists here (CLAUDE.md hard rule 1).

The pure processor accepts both the `full` object messages and `level3` positional arrays
(decoded via the one-off schema object). level3 resync is inferred from `full` (feeds.md F-3,
PARTIAL).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from decimal import Decimal
from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, OrderIdBook, as_dict, normalize_side

ENDPOINT_PROD = "wss://ws-direct.exchange.coinbase.com"  # auth-required Direct feed
ENDPOINT_FAILOVER = "wss://ws-feed.exchange.coinbase.com"  # docs-recommended failover
REST_SNAPSHOT = "https://api.exchange.coinbase.com/products/{product}/book?level=3"


class CoinbaseProcessor(MessageProcessor):
    venue = "coinbase"

    def __init__(self, product: str = "BTC-USD", channel: str = "full", **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self.book = OrderIdBook()
        self.sequence: int | None = None
        self._schema: dict[str, list[str]] = {}
        self._pending: list[dict] = []  # buffer while awaiting the first snapshot

    # -- snapshot / resync -----------------------------------------------------------
    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """REST level=3 snapshot: {"sequence": N, "bids":[[price,size,order_id],...], "asks":...}.

        Rebuilds the book, then replays buffered live messages dropping any with
        ``sequence <= snapshot sequence`` (feeds.md §1 step 4).
        """
        snap = as_dict(snapshot)
        self.needs_resnapshot = False
        self.book.clear()
        for px, sz, oid in snap.get("bids", []):
            self.book.add(str(oid), BID, px, sz)
        for px, sz, oid in snap.get("asks", []):
            self.book.add(str(oid), ASK, px, sz)
        self.sequence = int(snap["sequence"])
        self.synced = True
        pending, self._pending = self._pending, []
        for msg in pending:
            self._apply_if_next(msg, int(msg.get("sequence", -1)), recv_ts_ns or 0)

    # -- message decoding ------------------------------------------------------------
    def _decode(self, obj: Any) -> dict | None:
        """Return a dict message, decoding level3 positional arrays via the stored schema."""
        if isinstance(obj, dict):
            if obj.get("type") == "level3" and "schema" in obj:  # one-off schema object
                self._schema = {k: list(v) for k, v in obj["schema"].items()}
                return None
            return obj
        if isinstance(obj, list) and obj:
            mtype = obj[0]
            fields = self._schema.get(mtype)
            if not fields:
                return None  # schema not yet seen; cannot decode this row
            return dict(zip(fields, obj))
        return None

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = self._decode(as_dict(raw))
        if msg is None:
            return []
        mtype = msg.get("type")
        if mtype in {"subscriptions", "error"}:
            return []
        if "sequence" not in msg:
            return []  # e.g. non-sequenced control frames
        seq = int(msg["sequence"])

        if not self.synced:
            self._pending.append(msg)
            return []
        return self._apply_if_next(msg, seq, recv_ts_ns)

    def _apply_if_next(self, msg: dict, seq: int, recv_ts_ns: int) -> list[dict]:
        assert self.sequence is not None
        expected = self.sequence + 1
        if seq < expected:
            return []  # duplicate / out-of-order — ignore (feeds.md §1)
        if seq > expected:
            self._emit_gap("sequence jump", recv_ts_ns, expected=expected, got=seq)
            return []
        self.sequence = seq
        return self._apply(msg, recv_ts_ns)

    def _apply(self, msg: dict, recv_ts_ns: int) -> list[dict]:
        mtype = msg["type"]
        oid = msg.get("order_id") or msg.get("maker_order_id")
        if mtype == "open":
            self.book.add(
                str(oid), normalize_side(msg["side"]), msg["price"], msg["remaining_size"]
            )
        elif mtype == "done":
            self.book.delete(str(oid))
        elif mtype == "change":
            self.book.modify(str(oid), qty=msg.get("new_size"))
        elif mtype == "match":
            # A match reduces the resting (maker) order by the filled size.
            maker = str(msg.get("maker_order_id") or oid)
            o = self.book.orders.get(maker)
            if o is not None:
                remaining = o.qty - Decimal(str(msg["size"]))
                self.book.modify(maker, qty=remaining if remaining > 0 else 0)
                if remaining <= 0:
                    self.book.delete(maker)
            return [{"kind": "trade", "price": msg.get("price"), "size": msg.get("size")}]
        # "received" and others do not change the resting book.
        return []


# -- IO wiring ----------------------------------------------------------------------
def _coinbase_auth(timestamp: str) -> dict[str, str]:
    """Build the HMAC-SHA256 auth fields from env vars (market-data subscribe only)."""
    key = os.environ["COINBASE_API_KEY"]
    secret = os.environ["COINBASE_API_SECRET"]
    passphrase = os.environ["COINBASE_API_PASSPHRASE"]
    message = f"{timestamp}GET/users/self/verify".encode()
    sig = hmac.new(base64.b64decode(secret), message, hashlib.sha256).digest()
    return {"signature": base64.b64encode(sig).decode(), "key": key, "passphrase": passphrase,
            "timestamp": timestamp}


def subscribe_messages(product: str = "BTC-USD", channel: str = "full") -> list[dict]:
    auth = _coinbase_auth(str(int(time.time())))
    return [{"type": "subscribe", "product_ids": [product], "channels": [channel], **auth}]


def io_config(product: str = "BTC-USD", channel: str = "full") -> IoConfig:
    return IoConfig(
        venue="coinbase",
        channel=channel,
        urls=[ENDPOINT_PROD, ENDPOINT_FAILOVER],
        subscribe_messages=subscribe_messages(product, channel),
        ping_interval_s=None,  # Coinbase has a 1 s server heartbeat channel instead
    )
