"""Coinbase **Advanced Trade** (RETAIL) ``level2`` recorder — a 3rd real BRTI constituent, free.

DISTINCT from the institutional Coinbase **Exchange** ``full``/``level3`` adapter in
``coinbase.py`` (``ws-direct.exchange.coinbase.com``, HMAC-auth, $5k-blocked). This is the public
retail Advanced Trade WebSocket: no credentials, reachable from anywhere.

Spec VERIFIED from primary docs (docs.cdp.coinbase.com, read 2026-10-17) — recorded in
``docs/feeds.md`` §6 and ``docs/facts.md``:

* **Endpoint** ``wss://advanced-trade-ws.coinbase.com`` — PUBLIC ("A JWT is not required" for
  market-data channels). We wire NO auth (public is sufficient; the optional JWT expires in 2 min).
* **Subscribe** one channel per message: ``{"type":"subscribe","product_ids":["BTC-USD"],
  "channel":"level2"}`` AND ``{"type":"subscribe","product_ids":["BTC-USD"],
  "channel":"heartbeats"}``. Subscribe within 5 s of connect or you are disconnected; idle channels
  close in 60–90 s, so the ``heartbeats`` subscription is REQUIRED to keep the socket alive.
* **Envelope** (messages arrive on channel ``l2_data`` — you SUBSCRIBE to ``"level2"`` but the data
  channel name is ``"l2_data"``): top-level ``channel``, ``timestamp`` (RFC3339), ``sequence_num``,
  ``events[]``.
* **events[]**: ``type`` (``"snapshot"`` | ``"update"``), ``product_id``, ``updates[]``.
* **updates[]**: ``side`` (``"bid"`` | ``"offer"``), ``price_level`` (str), ``new_quantity`` (str —
  the FULL size at that level, NOT a delta; ``"0"`` removes the level), ``event_time`` (RFC3339).

This is an L2 **price-level** book, so it uses :class:`PriceLevelBook` (price→qty), NOT
``OrderIdBook``. ``new_quantity == "0"`` deletes the level; any other value SETS (replaces) it.
Prices/qtys stay ``Decimal`` from the exact transmitted strings (no float).

GAP DETECTION (important subtlety, VERIFIED): ``sequence_num`` is per-**CONNECTION** (not
per-product) and increments by exactly 1 on EVERY message on the connection — ``l2_data``,
``heartbeats``, the ``subscriptions`` ack, everything. So we check the full connection stream: a
jump (``got > prev + 1``) is a gap (→ ``needs_resnapshot``); a lower/equal value is a
duplicate/out-of-order and is ignored (never a false gap on a heartbeat). We do NOT filter to
``l2_data`` before the sequence check.

COMPLETENESS: the first ``l2_data`` after subscribe is a ``type:"snapshot"`` (full book), so this
venue CAN establish a COMPLETE baseline offline (unlike Bitstamp) and mark ``synced=True`` — a plus
for the replica. A ``snapshot`` also RE-ANCHORS the per-connection sequence (a reconnect restarts
``sequence_num``), which is why it is handled before the numeric gap check.

RESYNC (in-band, like Bybit's snapshot reset — no REST fetch): on a gap, resubscribe to
``level2``; Coinbase answers with a fresh ``snapshot`` that rebuilds the book and clears the flag.
"""
from __future__ import annotations

from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, PriceLevelBook, as_dict

ENDPOINT = "wss://advanced-trade-ws.coinbase.com"  # VERIFIED 2026-10-17 (docs.cdp.coinbase.com)
DATA_CHANNEL = "l2_data"   # messages arrive here though we SUBSCRIBE to "level2"
SUB_CHANNEL = "level2"
HEARTBEATS = "heartbeats"  # REQUIRED subscription: keeps the socket alive (idle cut 60–90 s)


def _side(raw_side: Any) -> str | None:
    """Coinbase retail L2 side token → internal BID/ASK. ``"bid"``→BID, ``"offer"``→ASK."""
    s = str(raw_side).lower()
    if s == "bid":
        return BID
    if s == "offer":
        return ASK
    return None  # unknown token: ignore the update rather than mis-side it


class CoinbaseAdvancedProcessor(MessageProcessor):
    venue = "coinbase_advanced"

    def __init__(self, product: str = "BTC-USD", channel: str = SUB_CHANNEL, **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self.book = PriceLevelBook()
        self.last_seq: int | None = None  # per-CONNECTION sequence anchor

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict):
            return []
        seq = _int_or_none(msg.get("sequence_num"))
        chan = msg.get("channel")
        events = msg.get("events") or []
        has_snapshot = chan == DATA_CHANNEL and any(
            isinstance(e, dict) and e.get("type") == "snapshot" for e in events
        )

        # A snapshot is the authoritative fresh-book baseline (initial subscribe, reconnect, or
        # resync). It RE-ANCHORS the per-connection sequence — a reconnect restarts sequence_num,
        # so the snapshot must be handled BEFORE the numeric gap check (which would otherwise see
        # the restarted low value as out-of-order and never re-anchor). Never a gap.
        if has_snapshot:
            self._reset_from_snapshot(events)
            self.synced = True
            self.needs_resnapshot = False
            self.awaiting_snapshot = False
            self.last_seq = seq
            return [{"kind": "snapshot"}]

        # Per-connection sequence gap check on EVERY other message (incl. heartbeats / acks).
        if seq is not None:
            if self.last_seq is None:
                self.last_seq = seq  # first message seen on this connection: anchor
            elif seq == self.last_seq + 1:
                self.last_seq = seq
            elif seq <= self.last_seq:
                return []  # duplicate / out-of-order — ignore, do not advance
            else:  # seq > last_seq + 1: dropped message(s)
                expected = self.last_seq + 1
                self.last_seq = seq  # advance so the chain continues from here
                if not self.awaiting_snapshot:
                    # FIRST gap: log it (never silent) and arm the in-band resync; mark the book
                    # diverged so updates are dropped until the resync snapshot re-anchors (mirror
                    # live begin_resync's awaiting_snapshot).
                    self._emit_gap("sequence_num gap", recv_ts_ns, expected=expected, got=seq)
                    self.awaiting_snapshot = True
                # else: already waiting for the resync snapshot — stay QUIET (no re-log, no re-arm)
                # so sustained packet loss can't thrash resubscribe frames against Coinbase's
                # PARTIAL/unknown rate limits. Matches Kraken/Bybit (quiet until the snapshot lands).
                return []

        if chan != DATA_CHANNEL:
            return []  # heartbeats / subscriptions ack: sequence checked, nothing to apply

        if self.awaiting_snapshot:
            return []  # post-gap / pre-resync-snapshot updates: drop without re-arming resync

        out: list[dict] = []
        for e in events:
            if not isinstance(e, dict) or e.get("type") != "update":
                continue
            if not self.synced:
                # An update with no established baseline is a real gap (never silent).
                self._emit_gap("update before snapshot", recv_ts_ns, got=seq)
                continue
            self._apply_updates(e.get("updates", []))
            out.append({"kind": "book"})
        return out

    def _reset_from_snapshot(self, events: list) -> None:
        self.book.clear()
        for e in events:
            if isinstance(e, dict) and e.get("type") == "snapshot":
                self._apply_updates(e.get("updates", []))

    def _apply_updates(self, updates: list) -> None:
        for u in updates:
            if not isinstance(u, dict):
                continue
            side = _side(u.get("side"))
            if side is None:
                continue
            # new_quantity is the FULL size at the level; "0" removes it. PriceLevelBook.set
            # deletes on qty 0 and replaces otherwise — exactly the venue's semantics.
            self.book.set(side, u.get("price_level"), u.get("new_quantity"))

    def begin_resync(self) -> None:
        """Enter resync mode before the resubscribe-triggered fresh snapshot arrives (in-band).

        Clearing the book + dropping ``synced`` means the fresh ``snapshot`` is the expected
        rebuild, and ``awaiting_snapshot`` drops any pre-snapshot updates so they do not re-arm
        ``needs_resnapshot`` and thrash resubscribe frames.
        """
        self.synced = False
        self.book.clear()
        self.awaiting_snapshot = True

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Coinbase retail resyncs in-band; on (re)connect we just arm 'await snapshot'."""
        self.synced = False
        self.needs_resnapshot = False
        self.awaiting_snapshot = True
        self.book.clear()


def _int_or_none(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def subscribe_messages(product: str = "BTC-USD") -> list[dict]:
    """One channel per message: the ``level2`` book + the REQUIRED ``heartbeats`` keep-alive."""
    return [
        {"type": "subscribe", "product_ids": [product], "channel": SUB_CHANNEL},
        {"type": "subscribe", "product_ids": [product], "channel": HEARTBEATS},
    ]


def resubscribe_messages(product: str = "BTC-USD") -> list[dict]:
    """Frames that force a fresh ``level2`` ``snapshot`` (unsubscribe then subscribe)."""
    return [
        {"type": "unsubscribe", "product_ids": [product], "channel": SUB_CHANNEL},
        {"type": "subscribe", "product_ids": [product], "channel": SUB_CHANNEL},
    ]


def io_config(product: str = "BTC-USD") -> IoConfig:
    return IoConfig(
        venue="coinbase_advanced",
        channel=SUB_CHANNEL,
        urls=[ENDPOINT],
        subscribe_messages=subscribe_messages(product),
        # Liveness is provided by the server-pushed ``heartbeats`` channel (subscribed above), not a
        # client ping; no client ping frame is required.
        ping_interval_s=None,
        # The level2 ``snapshot`` is ONE frame carrying the full book (~40k levels, ~1.6 MiB+ on
        # BTC-USD), which exceeds the websockets 1 MiB default and would be silently rejected →
        # reconnect-loop with nothing recorded. Unbounded so the complete baseline frame is received
        # (trusted exchange feed). VERIFIED by a live wire probe from the box (2026-10-17).
        max_size=None,
    )
