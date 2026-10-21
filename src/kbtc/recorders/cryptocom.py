"""Crypto.com Exchange v1 public ``book`` (L2) recorder — a BRTI constituent, free, no auth.

Spec VERIFIED from the primary Crypto.com doc (the v1 ``book.{instrument_name}.{depth}`` channel
page at ``exchange-developer.crypto.com/exchange/v1/docs/api/websocket/ws-channel-book-instrument-
name-depth`` — the old ``exchange-docs.crypto.com/exchange/v1/rest-ws`` 301-redirects here; read
2026-10-21) AND confirmed on the live wire by a brief read-only probe from BOTH the Ashburn (US)
and EU (DE) boxes (``wss://stream.crypto.com/exchange/v1/market``, ``book.BTC_USD.50``,
SNAPSHOT_AND_UPDATE, 2026-10-21). Recorded in ``docs/feeds.md`` §5C and ``docs/facts.md``.

* **Endpoint** ``wss://stream.crypto.com/exchange/v1/market`` — PUBLIC market data, no credentials.
* **Instrument** ``BTC_USD`` — the BTC/USD **spot** pair (``inst_type`` ``CCY_PAIR``; confirmed via
  the public ``get-instruments`` REST read). The doc examples' ``BTCUSD-PERP`` is the PERPETUAL, a
  different instrument — we want spot for BRTI. Depth-capped at **50** (the venue max; BRTI caps
  depth anyway).
* **Subscribe** ONE frame: ``{"id":N,"method":"subscribe","params":{"channels":["book.BTC_USD.50"],
  "book_subscription_type":"SNAPSHOT_AND_UPDATE","book_update_frequency":100},"nonce":<ms>}``.
  ``SNAPSHOT_AND_UPDATE`` is the 100 ms delta mode (the old full-snapshot-only feed was retired
  2025-02); ``book_update_frequency`` 10|100|500 ms. The nonce is a fresh ms timestamp per connect
  (``subscribe_factory``). Unsubscribe mirrors it with ``"method":"unsubscribe"``.
* **Messages** (book data rides in ``result`` with ``channel`` starting ``book``; ``result.data`` is
  an array of ONE object):
  - **SNAPSHOT** (first frame after subscribe, and after any resubscribe/reconnect) — ``data[0]``
    has top-level ``asks`` and ``bids`` (the FULL depth-capped book), ``u`` (sequence), ``t``/``tt``
    (ms timestamps), ``cs`` (book checksum); NO ``pu``. Each level is ``[price, quantity,
    num_orders]`` as strings; ``quantity`` ``"0"`` removes the level. VERIFIED on the wire
    (``snapshot_fields = [asks,bids,cs,t,tt,u]``).
  - **DELTA** — ``data[0]`` has an ``update`` object holding the CHANGED ``asks``/``bids`` levels,
    plus ``u`` (this update's sequence) and ``pu`` (the PREVIOUS update's sequence); NO top-level
    ``asks``/``bids``. VERIFIED on the wire (``delta_fields = [cs,pu,t,tt,u,update]``). ``quantity``
    is the FULL (absolute SET) size at the level; ``"0"`` removes it. Prices/qtys kept ``Decimal``
    from the exact transmitted strings (no float).
* **Heartbeats** — the server sends ``{"id":N,"method":"public/heartbeat","code":0}`` ~every 30 s;
  the client MUST reply ``{"id":N,"method":"public/respond-heartbeat"}`` (echoing the id) within 5 s
  or the server disconnects (doc-VERIFIED; a heartbeat was seen and answered on the wire). The pure
  processor QUEUES that reply on ``self.outbound``; the IO layer (``run_recorder``) sends it over the
  live socket right after ``process``. The websockets library's own protocol ping is a second layer.

GAP DETECTION (VERIFIED — the real, documented mechanism, NOT invented): each DELTA carries ``pu``
(previous sequence) and ``u`` (this sequence); deltas must be applied in strict order where
**``pu`` of the new message equals ``u`` of the previous message** (Binance-USDⓈ-M-futures-style
chain). The snapshot anchors ``last_u = u``. A ``pu`` that does not match ``last_u`` means a dropped
message → log the gap (never silent) and RESYNC. VERIFIED on the live wire: 37/37 (Ashburn) and
38/38 (EU) consecutive ``pu == prev.u`` checks held. (The frames also carry a ``cs`` book checksum —
present but its algorithm is not spelled out on the current doc page; we do NOT rely on it and do
NOT invent one. It is a future hardening option, see ``docs/feeds.md`` F-12.)

RESYNC (in-band, like Bybit / Coinbase-Advanced / Gemini — no REST): on a ``pu`` break, resubscribe
(unsubscribe + subscribe the book channel) → Crypto.com re-pushes a fresh SNAPSHOT that rebuilds the
book and clears the flags. While awaiting that snapshot we DROP deltas without re-arming resync (no
resubscribe-frame thrash). A mid-stream snapshot while already synced is treated as the reconnect/
resubscribe reseed: rebuild SILENTLY (the triggering event — the disconnect logged by the IO layer,
or the ``pu`` gap logged here — is never silent, so the reset itself need not re-log).
``resync_on_connect=False`` (the initial subscribe already yields the reseed snapshot).
"""
from __future__ import annotations

import time
from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, PriceLevelBook, as_dict

ENDPOINT = "wss://stream.crypto.com/exchange/v1/market"  # VERIFIED 2026-10-21 (doc + live wire)
INSTRUMENT = "BTC_USD"  # BTC/USD SPOT (CCY_PAIR); NOT the BTCUSD-PERP perpetual
DEPTH = 50              # venue max; BRTI caps depth anyway
HEARTBEAT = "public/heartbeat"
RESPOND_HEARTBEAT = "public/respond-heartbeat"


def _channel_name(product: str = INSTRUMENT, depth: int = DEPTH) -> str:
    """The venue channel token, e.g. ``book.BTC_USD.50``."""
    return f"book.{product}.{depth}"


class CryptoComL2Processor(MessageProcessor):
    venue = "cryptocom"

    def __init__(self, product: str = INSTRUMENT, channel: str = "book_btcusd",
                 depth: int = DEPTH, **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self.depth = depth
        self.book = PriceLevelBook()
        self.last_u: int | None = None   # sequence of the last applied book message
        # One-shot guard so a sustained pre-snapshot delta run logs a single gap, not a storm.
        self._gap_flagged = False

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict):
            return []

        # Heartbeat: the server requires an id-echoing reply within 5 s. Queue it (the IO layer
        # sends over the live socket); it is NOT a book frame.
        if msg.get("method") == HEARTBEAT:
            self.outbound.append({"id": msg.get("id"), "method": RESPOND_HEARTBEAT})
            return []

        res = msg.get("result")
        if not isinstance(res, dict):
            return []  # subscribe ack without data / errors / other channels: not a book frame
        if not str(res.get("channel", "")).startswith("book"):
            return []
        data = res.get("data") or []
        if not data or not isinstance(data[0], dict):
            return []
        d0 = data[0]

        # SNAPSHOT vs DELTA (VERIFIED on the wire): a DELTA carries an ``update`` object; a SNAPSHOT
        # carries top-level ``asks``/``bids`` and NO ``update``/``pu``.
        if "update" not in d0:
            # SNAPSHOT — authoritative fresh baseline (initial subscribe, resubscribe resync, or
            # reconnect reseed). Re-anchors the sequence; never a gap.
            self._reset_from_snapshot(d0)
            self.synced = True
            self.needs_resnapshot = False   # the snapshot IS the (re)sync (in-band)
            self.awaiting_snapshot = False  # the fresh snapshot landed
            self._gap_flagged = False
            self.last_u = _int_or_none(d0.get("u"))
            return [{"kind": "snapshot"}]

        # DELTA.
        if self.awaiting_snapshot:
            return []  # resubscribed, waiting for the rebuild snapshot: drop stale deltas, no re-arm
        if not self.synced:
            # A delta with no established baseline is a real gap (never silent).
            if not self._gap_flagged:
                self._emit_gap("delta before snapshot", recv_ts_ns, got=d0.get("u"))
                self._gap_flagged = True
            return []

        pu = _int_or_none(d0.get("pu"))
        u = _int_or_none(d0.get("u"))
        # Strict ordering: this delta's ``pu`` must equal the previous message's ``u`` (VERIFIED
        # chain). A mismatch is a dropped message → log once, arm the in-band resync, drop deltas
        # until the reseed snapshot re-anchors (no resubscribe thrash on sustained loss).
        if self.last_u is not None and (pu is None or pu != self.last_u):
            if not self._gap_flagged:
                self._emit_gap("pu chain break", recv_ts_ns, expected=self.last_u, got=pu)
                self._gap_flagged = True
            self.awaiting_snapshot = True
            return []

        self._apply_update(d0.get("update") or {})
        self.last_u = u
        return [{"kind": "book", "u": u}]

    def _reset_from_snapshot(self, d0: dict) -> None:
        self.book.clear()
        self._apply_levels(d0.get("bids"), BID)
        self._apply_levels(d0.get("asks"), ASK)

    def _apply_update(self, update: dict) -> None:
        self._apply_levels(update.get("bids"), BID)
        self._apply_levels(update.get("asks"), ASK)

    def _apply_levels(self, levels: Any, side: str) -> None:
        for lvl in levels or []:
            if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
                continue
            # level = [price, quantity, num_orders]; we keep price→qty. qty "0" removes the level
            # (PriceLevelBook.set deletes on 0, replaces otherwise — the venue's absolute-set
            # semantics). num_orders (lvl[2]) is not needed for an L2 price-level book.
            self.book.set(side, lvl[0], lvl[1])

    def begin_resync(self) -> None:
        """Enter resync mode before the resubscribe-triggered fresh snapshot arrives (in-band).

        Clearing the book + dropping ``synced`` means the fresh snapshot is the expected rebuild;
        ``awaiting_snapshot`` drops stale pre-snapshot deltas so they do not re-arm
        ``needs_resnapshot`` and thrash resubscribe frames. ``last_u`` is reset so the reseed
        snapshot re-anchors the chain.
        """
        self.synced = False
        self.book.clear()
        self.awaiting_snapshot = True
        self.last_u = None
        self._gap_flagged = False

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Crypto.com resyncs in-band; on (re)connect we just arm 'await snapshot'."""
        self.synced = False
        self.needs_resnapshot = False
        self.awaiting_snapshot = True
        self.book.clear()
        self.last_u = None
        self._gap_flagged = False


def _int_or_none(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def subscribe_messages(product: str = INSTRUMENT, depth: int = DEPTH,
                       nonce: int | None = None) -> list[dict]:
    """Subscribe to the ``book`` channel for ``product`` at ``depth`` (one frame).

    ``nonce`` is the documented required current-ms timestamp; ``None`` generates a fresh one (the
    ``subscribe_factory`` regenerates it per connect so a long-lived reconnect never replays a stale
    nonce). No credential is involved (public market data)."""
    if nonce is None:
        nonce = int(time.time() * 1000)
    return [{
        "id": 1,
        "method": "subscribe",
        "params": {
            "channels": [_channel_name(product, depth)],
            "book_subscription_type": "SNAPSHOT_AND_UPDATE",
            "book_update_frequency": 100,
        },
        "nonce": nonce,
    }]


def resubscribe_messages(product: str = INSTRUMENT, depth: int = DEPTH,
                         nonce: int | None = None) -> list[dict]:
    """Frames that force a fresh ``book`` SNAPSHOT (unsubscribe then subscribe; in-band, no REST)."""
    if nonce is None:
        nonce = int(time.time() * 1000)
    return [
        {"id": 1, "method": "unsubscribe",
         "params": {"channels": [_channel_name(product, depth)]}, "nonce": nonce},
        *subscribe_messages(product, depth, nonce=nonce),
    ]


def io_config(product: str = INSTRUMENT, depth: int = DEPTH) -> IoConfig:
    return IoConfig(
        venue="cryptocom",
        channel="book_btcusd",
        urls=[ENDPOINT],
        subscribe_messages=subscribe_messages(product, depth),
        # Fresh nonce per (re)connect (public market-data subscribe; no credential).
        subscribe_factory=lambda: subscribe_messages(product, depth),
        # Liveness: the server pushes ``public/heartbeat`` (answered via the processor's queued
        # ``public/respond-heartbeat``), and the websockets library runs its own protocol ping; no
        # fixed-cadence client app-ping frame is required.
        ping_interval_s=None,
        # Depth-capped at 50 levels/side → a tiny snapshot frame; the default 1 MiB limit is ample.
    )
