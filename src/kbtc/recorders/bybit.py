"""Bybit v5 linear recorder (lead signal, NOT a BRTI constituent).

feeds.md §5. ``orderbook.{depth}.{symbol}`` is a price-level book; ``publicTrade.{symbol}`` is
the trade tape. Public, no auth.

Gap handling: Bybit does **not** document a numeric ``u``-gap rule. The authoritative recovery
is a **snapshot reset** — every ``type == "snapshot"`` message resets and rebuilds the book (it
is guaranteed to carry the latest data and is re-sent by Bybit on any problem), and after a
reconnect the first message is a snapshot. So the resync trigger is ``type == "snapshot"``, not
a ``u`` jump. We therefore:

* treat the first snapshot as a normal reset (synced),
* treat any later mid-stream snapshot as a Bybit-initiated **resync** and log it as a gap event
  (so a reset is never silent), and
* treat a ``delta`` arriving while we hold no book (pre-first-snapshot or post-reset gap) as a
  gap.

``u``/``seq`` are monotonic ordering aids; we do NOT treat a ``u`` jump alone as a gap (feeds.md
F-6, PARTIAL — undocumented).
"""
from __future__ import annotations

from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, PriceLevelBook, as_dict

ENDPOINT = "wss://stream.bybit.com/v5/public/linear"
ENDPOINT_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"


class BybitOrderbookProcessor(MessageProcessor):
    venue = "bybit"

    def __init__(self, product: str = "BTCUSDT", depth: int = 50,
                 channel: str | None = None, **kw: Any) -> None:
        channel = channel or f"orderbook.{depth}.{product}"
        super().__init__(product=product, channel=channel, **kw)
        self.depth = depth
        self.book = PriceLevelBook()
        self.last_u: int | None = None

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict) or "data" not in msg:
            return []
        topic = msg.get("topic", "")
        if not topic.startswith("orderbook."):
            return []
        mtype = msg.get("type")
        data = msg["data"]
        u = data.get("u")

        if mtype == "snapshot":
            if self.synced:
                # Bybit re-sent a snapshot mid-stream => it had a problem. Log the reset as a
                # gap event (never silent), then rebuild.
                self._emit_gap("snapshot reset", recv_ts_ns, expected=self.last_u, got=u)
            self._reset_from_snapshot(data)
            self.synced = True
            self.needs_resnapshot = False  # snapshot IS the resync (in-band)
            self.awaiting_snapshot = False  # the fresh snapshot landed
            self.last_u = _int_or_none(u)
            return [{"kind": "snapshot"}]

        if mtype == "delta":
            if self.awaiting_snapshot:
                # We resubscribed and are waiting for the fresh snapshot: drop pre-snapshot
                # deltas WITHOUT re-arming resync (no resubscribe-frame thrash).
                return []
            if not self.synced:
                # Delta with no book (before first snapshot / after a reset) — a real gap.
                self._emit_gap("delta before snapshot", recv_ts_ns, got=u)
                return []
            self._apply_delta(data)
            self.last_u = _int_or_none(u)
            return [{"kind": "book", "u": u}]
        return []

    def _reset_from_snapshot(self, data: dict) -> None:
        self.book.clear()
        for p, q in data.get("b", []):
            self.book.set(BID, p, q)
        for p, q in data.get("a", []):
            self.book.set(ASK, p, q)

    def _apply_delta(self, data: dict) -> None:
        for p, q in data.get("b", []):
            self.book.set(BID, p, q)  # amount 0 ⇒ delete
        for p, q in data.get("a", []):
            self.book.set(ASK, p, q)

    def begin_resync(self) -> None:
        """Enter resync mode before the resubscribe-triggered snapshot arrives.

        Clearing the book (and dropping ``synced``) means the fresh ``type:"snapshot"`` frame is
        treated as the expected in-band rebuild — NOT logged as a spurious "snapshot reset" gap.
        ``awaiting_snapshot`` suppresses pre-snapshot deltas so they do NOT re-arm ``needs_resnapshot``
        and spam resubscribe frames (feeds.md §5: ≤500 connections/5 min) until the snapshot lands.
        """
        self.synced = False
        self.book.clear()
        self.awaiting_snapshot = True

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Bybit resyncs in-band; on reconnect we just mark 'await snapshot'."""
        self.synced = False
        self.needs_resnapshot = False
        self.awaiting_snapshot = True
        self.book.clear()


class BybitPublicTradeProcessor(MessageProcessor):
    """publicTrade tape. Trade-gap detection on the trade id ``i`` (feeds.md §5).

    Bybit does not promise contiguous trade ids, so we can only flag a NON-MONOTONIC id
    (out-of-order / duplicate) — a forward jump is not provably a gap. Log-only
    (``resync_on_gap=False``): a trade tape has no book to rebuild.
    """

    venue = "bybit"
    resync_on_gap = False

    def __init__(self, product: str = "BTCUSDT", channel: str | None = None, **kw: Any) -> None:
        channel = channel or f"publicTrade.{product}"
        super().__init__(product=product, channel=channel, **kw)
        self.last_trade_id: str | None = None
        self.last_trade_id_int: int | None = None

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict) or not str(msg.get("topic", "")).startswith("publicTrade."):
            return []
        out: list[dict] = []
        for t in msg.get("data", []):
            tid = t.get("i")
            tid_int = _int_or_none(tid)
            if (tid_int is not None and self.last_trade_id_int is not None
                    and tid_int <= self.last_trade_id_int):
                self._emit_gap("trade id non-monotonic", recv_ts_ns,
                               expected=f">{self.last_trade_id_int}", got=tid_int)
            out.append({"kind": "trade", "price": t.get("p"), "size": t.get("v"),
                        "side": t.get("S"), "trade_id": tid, "ts": t.get("T")})
            self.last_trade_id = tid
            if tid_int is not None:
                self.last_trade_id_int = tid_int
        return out


def _int_or_none(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def subscribe_messages(product: str = "BTCUSDT", depth: int = 50) -> list[dict]:
    return [{"req_id": "kbtc", "op": "subscribe",
             "args": [f"orderbook.{depth}.{product}", f"publicTrade.{product}"]}]


def resubscribe_messages(product: str = "BTCUSDT", depth: int = 50) -> list[dict]:
    """Frames that force a fresh orderbook ``type:"snapshot"`` (feeds.md §5).

    A resubscribe (unsubscribe then subscribe) to the orderbook topic makes Bybit re-push a
    snapshot first, which resets/rebuilds the book. Only the orderbook topic needs this — the
    trade tape has no book to rebuild.
    """
    topic = f"orderbook.{depth}.{product}"
    return [{"op": "unsubscribe", "args": [topic]},
            {"req_id": "kbtc-resync", "op": "subscribe", "args": [topic]}]


def io_config(product: str = "BTCUSDT", depth: int = 50) -> IoConfig:
    return IoConfig(
        venue="bybit",
        channel=f"orderbook.{depth}.{product}",
        urls=[ENDPOINT],
        subscribe_messages=subscribe_messages(product, depth),
        ping_message='{"op":"ping"}',
        ping_interval_s=20.0,  # feeds.md §5: ping every 20 s; idle cut after 10 min
    )
