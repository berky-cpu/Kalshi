"""Gemini v2 public ``marketdata`` ``l2`` recorder — a BRTI constituent, free, no auth.

Spec VERIFIED from the primary Gemini doc (``developer.gemini.com/websocket/archived/v2`` — the v2
``marketdata`` page, now tagged *Archived* by Gemini but the endpoint is STILL LIVE; read
2026-10-21) AND confirmed on the live wire by a brief read-only probe (``wss://api.gemini.com/v2/
marketdata``, BTCUSD, 2026-10-20). Recorded in ``docs/feeds.md`` §5B and ``docs/facts.md``.

* **Endpoint** ``wss://api.gemini.com/v2/marketdata`` — PUBLIC market data, no credentials.
* **Subscribe** ONE frame: ``{"type":"subscribe","subscriptions":[{"name":"l2",
  "symbols":["BTCUSD"]}]}``. Unsubscribe mirrors it with ``"type":"unsubscribe"``.
* **Messages** (all ``type:"l2_updates"`` for the book):
  - **Initial / reseed SNAPSHOT** — ``{"type":"l2_updates","symbol":"BTCUSD","changes":[[side,
    price,qty],...],"trades":[...],"auction_events":[...]}``. ``changes`` is the FULL book (both
    sides; ~4.5k levels on BTCUSD). It is the ONLY l2 message that also carries ``trades`` and
    ``auction_events`` (VERIFIED on the wire).
  - **Incremental DELTA** — ``{"type":"l2_updates","symbol":"BTCUSD","changes":[[side,price,
    qty]]}`` with ONLY ``changes`` (no ``trades``/``auction_events``). ``qty`` is the FULL size at
    that price level (absolute SET, not a delta); ``"0"``/``"0.0"`` REMOVES the level.
  - ``side`` is ``"buy"``→BID / ``"sell"``→ASK. Prices/qtys kept ``Decimal`` from the exact
    transmitted strings (no float).
* **Trades** arrive separately as ``type:"trade"`` frames (and the last-50 are backfilled in the
  snapshot's ``trades`` array). The book processor ignores them; the trade tape is parsed later in
  ``normalize`` (store-raw-parse-later).
* **Heartbeats** — the server pushes ``type:"heartbeat"`` frames; combined with the websockets
  library's protocol ping this keeps the socket alive (no client app-ping needed).

GAP DETECTION — the critical subtlety (VERIFIED): **the v2 ``l2`` feed carries NO sequence
number and NO per-message ordering/gap field whatsoever** (the probe found no ``seq`` /
``socket_sequence`` / ``event_id`` on any l2 message). So we CANNOT detect a dropped message by
sequence the way Coinbase/Binance/Bitstamp do. We therefore detect divergence by the only in-band
signal the feed affords — a **crossed book** (``best_bid > best_ask``), which on a venue that never
rests a bid above an ask can only mean we MISSED a level-removal update — and we rely on
**reconnect = reseed** for everything a cross cannot catch. This is a genuine limitation
(``docs/facts.md`` marks the no-sequence fact VERIFIED and the crossed-book detector PARTIAL /
best-effort): a missed update that does NOT cross the book is undetectable in-band and is only
corrected at the next reseed (reconnect, staleness-watchdog force-reconnect, or a crossed-book
resync). We do NOT invent a sequence field that does not exist.

SNAPSHOT vs DELTA — because v2 has no ``type:"snapshot"`` marker, a snapshot is identified by its
signature: an ``l2_updates`` carrying ``trades`` (or ``auction_events``). This is Gemini's analogue
of Bybit's ``type=="snapshot"`` reset: a snapshot CLEARS and rebuilds the book, a delta SETS single
levels. Because a delta NEVER carries ``trades`` (VERIFIED over many samples), a stale pre-resync
delta can never be mistaken for the rebuild snapshot, which cleanly closes the in-band resync race.

RESYNC (in-band, like Bybit / Coinbase-Advanced — no REST): the feed sends a fresh full snapshot on
every (re)subscribe, so a reconnect reseeds automatically and a crossed-book gap is recovered by
resubscribing (``l2``) → fresh snapshot rebuilds the book and clears the flags. A mid-stream
snapshot while we still hold a book is a RESET (reconnect reseed): it rebuilds silently — the
triggering event (the disconnect, logged by the IO layer; or the crossed-book gap, logged here) is
never silent, so the reset itself need not re-log. While awaiting the resync snapshot we DROP
deltas without re-arming resync (no resubscribe-frame thrash), mirroring Bybit/Coinbase-Advanced.
"""
from __future__ import annotations

from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, PriceLevelBook, as_dict

ENDPOINT = "wss://api.gemini.com/v2/marketdata"  # VERIFIED 2026-10-21 (doc) + 2026-10-20 (live wire)
L2_TYPE = "l2_updates"


def _side(raw_side: Any) -> str | None:
    """Gemini l2 side token → internal BID/ASK. ``"buy"``→BID, ``"sell"``→ASK."""
    s = str(raw_side).lower()
    if s == "buy":
        return BID
    if s == "sell":
        return ASK
    return None  # unknown token: ignore the change rather than mis-side it


class GeminiL2Processor(MessageProcessor):
    venue = "gemini"

    def __init__(self, product: str = "BTCUSD", channel: str = "l2_btcusd", **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self.book = PriceLevelBook()
        # Suppress repeated crossed-book gap emissions while diverged (until a snapshot re-anchors),
        # both live and in offline replay. Prevents a gap storm under sustained divergence.
        self._crossed_flagged = False

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict) or msg.get("type") != L2_TYPE:
            return []  # heartbeats, separate trade frames, acks: not book frames
        if msg.get("symbol") != self.product:
            return []  # defensive: ignore a frame for a different symbol
        changes = msg.get("changes") or []

        # SNAPSHOT signature: the only l2_updates that also carries ``trades``/``auction_events``
        # (VERIFIED on the wire). This is Gemini's analogue of Bybit's ``type:"snapshot"`` marker.
        if "trades" in msg or "auction_events" in msg:
            self._reset_from_snapshot(changes)
            self.synced = True
            self.needs_resnapshot = False  # the snapshot IS the (re)sync (in-band)
            self.awaiting_snapshot = False  # the fresh snapshot landed
            self._crossed_flagged = False
            return [{"kind": "snapshot"}]

        # DELTA.
        if self.awaiting_snapshot:
            return []  # resubscribed, waiting for the rebuild snapshot: drop stale deltas, no re-arm
        if not self.synced:
            # A delta with no established baseline is a real gap (never silent).
            if not self._crossed_flagged:
                self._emit_gap("delta before snapshot", recv_ts_ns)
                self._crossed_flagged = True  # reuse the one-shot guard: quiet until a snapshot
            return []

        self._apply_changes(changes)

        # The only in-band integrity signal v2 affords: a STRICTLY crossed book (bid > ask) can only
        # mean a missed level-removal (the venue never rests a bid above an ask). Flag once, then
        # stay quiet until a snapshot re-anchors (no gap storm, no resubscribe thrash).
        bid, ask = self.book.best_bid(), self.book.best_ask()
        if bid is not None and ask is not None and bid > ask:
            if not self._crossed_flagged:
                self._emit_gap("crossed book", recv_ts_ns, expected="bid<=ask", got=f"{bid}>{ask}")
                self._crossed_flagged = True
        return [{"kind": "book"}]

    def _reset_from_snapshot(self, changes: list) -> None:
        self.book.clear()
        self._apply_changes(changes)

    def _apply_changes(self, changes: list) -> None:
        for c in changes:
            if not isinstance(c, (list, tuple)) or len(c) < 3:
                continue
            side = _side(c[0])
            if side is None:
                continue
            # qty is the FULL size at the level; "0"/"0.0" removes it. PriceLevelBook.set deletes on
            # qty 0 and replaces otherwise — exactly the venue's absolute-set semantics.
            self.book.set(side, c[1], c[2])

    def begin_resync(self) -> None:
        """Enter resync mode before the resubscribe-triggered fresh snapshot arrives (in-band).

        Clearing the book + dropping ``synced`` means the fresh snapshot is the expected rebuild;
        ``awaiting_snapshot`` drops any stale pre-snapshot deltas so they do not re-arm
        ``needs_resnapshot`` and thrash resubscribe frames until the snapshot lands.
        """
        self.synced = False
        self.book.clear()
        self.awaiting_snapshot = True
        self._crossed_flagged = False

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Gemini resyncs in-band; on (re)connect we just arm 'await snapshot'."""
        self.synced = False
        self.needs_resnapshot = False
        self.awaiting_snapshot = True
        self.book.clear()
        self._crossed_flagged = False


def subscribe_messages(product: str = "BTCUSD") -> list[dict]:
    """Subscribe to the ``l2`` book for ``product`` (one frame)."""
    return [{"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": [product]}]}]


def resubscribe_messages(product: str = "BTCUSD") -> list[dict]:
    """Frames that force a fresh ``l2`` snapshot (unsubscribe then subscribe)."""
    return [
        {"type": "unsubscribe", "subscriptions": [{"name": "l2", "symbols": [product]}]},
        {"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": [product]}]},
    ]


def io_config(product: str = "BTCUSD") -> IoConfig:
    return IoConfig(
        venue="gemini",
        channel="l2_btcusd",
        urls=[ENDPOINT],
        subscribe_messages=subscribe_messages(product),
        # Liveness: the server pushes ``type:"heartbeat"`` frames and the websockets library runs its
        # own protocol ping; no client app-ping frame is required.
        ping_interval_s=None,
        # The initial ``l2_updates`` snapshot is ONE frame carrying the full book (~4.5k levels,
        # ~200 KiB on BTCUSD, measured) plus the last-50 ``trades`` and ``auction_events``. Keep the
        # frame limit unbounded (trusted exchange feed) so a busy-moment snapshot can never exceed
        # the websockets 1 MiB default and be silently rejected → reconnect-loop. (Matches Coinbase
        # Advanced's reasoning.)
        max_size=None,
    )
