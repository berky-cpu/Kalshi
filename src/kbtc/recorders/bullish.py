"""Bullish Exchange public ``l2Orderbook`` (L2) recorder — a BRTI constituent, free, no auth.

Spec VERIFIED from the primary Bullish doc (the "Multi-Order Book Data WebSocket" / public
market-data orderbook page ``docs.exchange.bullish.com/websocket/public/market-data/orderbook``,
the server list ``.../websocket/servers/server``, the keepalive page
``.../websocket/protocol/keepalive``, and the official ``bullish-exchange/api-examples``
``websocket/multi_orderbook_web_socket.py`` sample; all read 2026-10-21) AND confirmed on the live
wire by a brief read-only probe from BOTH the Ashburn (US) and EU (DE) boxes
(``wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook``, ``l2Orderbook`` BTCUSD,
2026-10-21). Recorded in ``docs/feeds.md`` §5D and ``docs/facts.md``.

* **Endpoint** ``wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook`` — PUBLIC
  market data, NO credentials (Bullish FIX carries no market data; this WS is the public source).
  Host ``wss://api.exchange.bullish.com`` (server page) + path ``/trading-api/v1/market-data/
  orderbook`` (orderbook page + the official sample's ``HOST_NAME + "/trading-api/v1/market-data/
  orderbook"``). Multiple symbols/topics multiplex on one socket.
* **Symbol** ``BTCUSD`` — the BTC/USD **spot** market (``marketId`` ``10000``, ``marketType``
  ``SPOT``; VERIFIED via the public REST ``GET api.exchange.bullish.com/trading-api/v1/markets``).
* **Subscribe** ONE JSON-RPC 2.0 frame: ``{"jsonrpc":"2.0","type":"command","method":"subscribe",
  "params":{"topic":"l2Orderbook","symbol":"BTCUSD"},"id":"<id>"}``. The L2 topic string
  ``l2Orderbook`` is VERIFIED from the official api-examples sample (the doc page only shows the
  ``l1Orderbook`` example but documents "L1 / L2 depending on your topic"). Unsubscribe mirrors it
  with ``"method":"unsubscribe"``. The ack is ``{"id":...,"jsonrpc":"2.0","result":{
  "responseCode":"200","responseCodeName":"OK","message":"Successfully subscribed ( BTCUSD N)"}}``.
* **Messages** — the book rides in frames carrying ``dataType:"V1TALevel2"``. ``data`` is the book
  object (the generic envelope documents snapshots as ``data:[{...}]`` and updates as ``data:{...}``;
  on the wire the L2 snapshot ships ``data`` as a bare OBJECT — we accept either shape). The object
  has: ``symbol``, ``bids`` and ``asks`` (FLAT string arrays alternating price,qty —
  ``[p0,q0,p1,q1,...]``, NOT nested pairs), ``sequenceNumberRange`` ``[firstSeq,lastSeq]`` (ints),
  ``datetime`` (ISO), ``timestamp`` (ms string), ``publishedAtTimestamp`` (ms string).
  - **SNAPSHOT** (``type:"snapshot"``) — the FULL book. VERIFIED on the wire to be the ONLY L2
    frame BTCUSD actually emits: 43/43 data frames over the probe were ``type:"snapshot"``, zero
    ``type:"update"``. Bullish publishes a periodic full-book snapshot and labels each with the
    contiguous ``sequenceNumberRange`` of the internal events it folded in. So a snapshot REPLACES
    the book wholesale (clear + rebuild) — there is no stale-level/ghost risk from a depth cap
    because every frame is authoritative and complete (unlike a diff feed; cf. REV-029 F1).
  - **UPDATE** (``type:"update"``, same ``dataType``) — an incremental change set. Documented in the
    generic envelope but **NEVER observed on the wire** for BTCUSD L2 (UNVERIFIED). We handle it
    defensively as an absolute-SET per price level (``qty "0"`` removes), the standard convention for
    these feeds — tagged UNVERIFIED because Bullish does not spell out the L2 update semantics and we
    could not exercise them. It is NOT the hot path; the snapshot path above is the verified one.
  Prices/qtys are kept ``Decimal`` from the exact transmitted strings (no float).
* **Keepalive** — an *idle* socket is closed after 5 minutes (doc-VERIFIED). The client sends
  ``{"jsonrpc":"2.0","type":"command","method":"keepalivePing","params":{},"id":"..."}`` periodically;
  the server answers ``{"result":{"responseCode":200,"responseCodeName":"OK","message":"Keep alive
  pong"}}`` (VERIFIED on the wire). The official sample pings every 300 s; we ping every 120 s via the
  fixed-cadence ``ping_message`` seam (well inside the 5-min idle window), plus the websockets
  library's own protocol ping. A continuous BTCUSD book is never idle in practice, so this is
  belt-and-suspenders, but it matches the documented mechanism and protects a quiet spell.

GAP DETECTION (VERIFIED — the real, documented field, NOT invented): each L2 frame carries
``sequenceNumberRange = [firstSeq, lastSeq]`` and consecutive frames CHAIN with
**``firstSeq`` of the new frame == ``lastSeq`` of the previous frame + 1** — VERIFIED on the live
wire: 42/42 (Ashburn) and 42/42 (EU) consecutive ``first == prev.last + 1`` checks held, 0 breaks.
The snapshot anchors ``last_seq = lastSeq``. A ``firstSeq`` that does not continue the chain means a
dropped batch → we LOG the gap (never silent). On the SNAPSHOT path the very frame that reveals the
gap also fully rebuilds the book, so the book self-heals immediately and no resync is needed (we log
the gap for honesty/coverage but clear ``needs_resnapshot``). On the (UNVERIFIED) UPDATE path a chain
break would corrupt an incremental book, so there we arm ``needs_resnapshot`` + ``awaiting_snapshot``
and drop updates until a fresh snapshot re-anchors — exactly like Crypto.com's ``pu`` break.

RESYNC (in-band, like Bybit / Coinbase-Advanced / Gemini / Crypto.com — no REST): resubscribe
(unsubscribe + subscribe ``l2Orderbook``) → Bullish re-pushes a fresh full snapshot that rebuilds the
book and clears the flags. ``resync_on_connect=False`` — the initial subscribe already yields the
reseed snapshot, and because every data frame is a full snapshot a mid-stream snapshot while already
synced is treated as a silent reconnect/resubscribe reseed (the triggering disconnect/gap is logged
elsewhere, so the reset itself need not re-log).
"""
from __future__ import annotations

import json
from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, PriceLevelBook, as_dict

ENDPOINT = "wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook"  # VERIFIED 2026-10-21 (doc+sample+wire)
SYMBOL = "BTCUSD"           # BTC/USD SPOT (marketId 10000); VERIFIED via public REST /markets
L2_TOPIC = "l2Orderbook"   # VERIFIED from the official api-examples sample + live wire
DATATYPE_L2 = "V1TALevel2"
# Client keepalive: ping well inside the 5-min idle close; matches the documented keepalivePing.
KEEPALIVE_PING = json.dumps(
    {"jsonrpc": "2.0", "type": "command", "method": "keepalivePing", "params": {}, "id": "kbtc-ka"}
)
KEEPALIVE_INTERVAL_S = 120.0


def _ints(rng: Any) -> tuple[int | None, int | None]:
    """``sequenceNumberRange`` → ``(firstSeq, lastSeq)`` as ints (or ``(None, None)``)."""
    if isinstance(rng, (list, tuple)) and len(rng) >= 2:
        try:
            return int(rng[0]), int(rng[1])
        except (TypeError, ValueError):
            return None, None
    return None, None


class BullishL2Processor(MessageProcessor):
    venue = "bullish"

    def __init__(self, product: str = SYMBOL, channel: str = "l2_btcusd", **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self.book = PriceLevelBook()
        self.last_seq: int | None = None   # lastSeq of the last applied book frame
        # One-shot guard so a sustained pre-snapshot run (update path) logs a single gap, not a storm.
        self._gap_flagged = False

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict):
            return []
        if msg.get("dataType") != DATATYPE_L2:
            return []  # acks, keepalive pongs, errors, other dataTypes: not L2 book frames
        body = self._body(msg.get("data"))
        if not isinstance(body, dict):
            return []
        if body.get("symbol") != self.product:
            return []  # defensive: ignore a frame for a different symbol on the multiplexed socket
        first, last = _ints(body.get("sequenceNumberRange"))
        typ = msg.get("type")

        # SNAPSHOT — the verified hot path: a FULL book that replaces our book wholesale and
        # re-anchors the sequence chain. A chain break here is LOGGED (never silent) but the book
        # self-heals from this same frame, so no resync is needed.
        if typ == "snapshot" or (typ != "update" and ("bids" in body or "asks" in body)):
            # A chain break between live snapshots is a dropped batch → LOG it (never silent); the
            # book still self-heals from this full frame. But a snapshot that arrives while
            # ``awaiting_snapshot`` is the EXPECTED resync reseed (after an update-path gap or
            # begin_resync), so it re-anchors silently — not a new gap.
            if (self.synced and not self.awaiting_snapshot and self.last_seq is not None
                    and first is not None and first != self.last_seq + 1):
                self._emit_gap("sequence gap (snapshot)", recv_ts_ns,
                               expected=self.last_seq + 1, got=first)
            self._reset_from_snapshot(body)
            self.synced = True
            self.needs_resnapshot = False   # the snapshot IS the (re)sync, and fully rebuilt the book
            self.awaiting_snapshot = False
            self._gap_flagged = False
            self.last_seq = last
            return [{"kind": "snapshot"}]

        # UPDATE (incremental) — UNVERIFIED defensive path (never seen on the wire for BTCUSD L2).
        if self.awaiting_snapshot:
            return []  # resubscribed, waiting for the rebuild snapshot: drop stale updates, no re-arm
        if not self.synced:
            # An update with no established baseline is a real gap (never silent).
            if not self._gap_flagged:
                self._emit_gap("update before snapshot", recv_ts_ns, got=first)
                self._gap_flagged = True
            return []
        # Strict ordering: this update must continue the chain (firstSeq == last_seq + 1). A break is
        # a dropped batch → log once, arm the in-band resync, drop updates until a snapshot re-anchors.
        if self.last_seq is not None and (first is None or first != self.last_seq + 1):
            if not self._gap_flagged:
                self._emit_gap("sequence gap (update)", recv_ts_ns,
                               expected=self.last_seq + 1, got=first)
                self._gap_flagged = True
            self.awaiting_snapshot = True
            return []
        self._apply_levels(body.get("bids"), BID)
        self._apply_levels(body.get("asks"), ASK)
        self.last_seq = last
        return [{"kind": "book", "seq": last}]

    @staticmethod
    def _body(data: Any) -> Any:
        """The book object from ``data`` — accept the bare object (wire) or the ``[{...}]`` array
        (generic-envelope doc form)."""
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    def _reset_from_snapshot(self, body: dict) -> None:
        self.book.clear()
        self._apply_levels(body.get("bids"), BID)
        self._apply_levels(body.get("asks"), ASK)

    def _apply_levels(self, flat: Any, side: str) -> None:
        """``bids``/``asks`` is a FLAT array alternating price,qty. ``qty "0"`` removes the level
        (``PriceLevelBook.set`` deletes on 0, replaces otherwise — absolute-set semantics)."""
        if not isinstance(flat, (list, tuple)):
            return
        for i in range(0, len(flat) - 1, 2):
            self.book.set(side, flat[i], flat[i + 1])

    def begin_resync(self) -> None:
        """Enter resync mode before the resubscribe-triggered fresh snapshot arrives (in-band).

        Clearing the book + dropping ``synced`` means the fresh snapshot is the expected rebuild;
        ``awaiting_snapshot`` drops stale pre-snapshot updates so they do not re-arm resync or thrash
        resubscribe frames. ``last_seq`` is reset so the reseed snapshot re-anchors the chain.
        """
        self.synced = False
        self.book.clear()
        self.awaiting_snapshot = True
        self.last_seq = None
        self._gap_flagged = False

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Bullish resyncs in-band; on (re)connect we just arm 'await snapshot'."""
        self.synced = False
        self.needs_resnapshot = False
        self.awaiting_snapshot = True
        self.book.clear()
        self.last_seq = None
        self._gap_flagged = False


def subscribe_messages(product: str = SYMBOL) -> list[dict]:
    """Subscribe to the ``l2Orderbook`` topic for ``product`` (one JSON-RPC 2.0 frame). No credential
    (public market data). The ``id`` is a client command id for request/response mapping only."""
    return [{
        "jsonrpc": "2.0", "type": "command", "method": "subscribe",
        "params": {"topic": L2_TOPIC, "symbol": product}, "id": "1",
    }]


def resubscribe_messages(product: str = SYMBOL) -> list[dict]:
    """Frames that force a fresh ``l2Orderbook`` snapshot (unsubscribe then subscribe; in-band)."""
    return [
        {"jsonrpc": "2.0", "type": "command", "method": "unsubscribe",
         "params": {"topic": L2_TOPIC, "symbol": product}, "id": "1"},
        *subscribe_messages(product),
    ]


def io_config(product: str = SYMBOL) -> IoConfig:
    return IoConfig(
        venue="bullish",
        channel="l2_btcusd",
        urls=[ENDPOINT],
        subscribe_messages=subscribe_messages(product),
        # Keepalive: an idle socket is closed after 5 min; send the documented ``keepalivePing`` every
        # 120 s (plus the websockets library's own protocol ping). A continuous book is never idle, so
        # this is belt-and-suspenders that also matches the official sample's mechanism.
        ping_message=KEEPALIVE_PING,
        ping_interval_s=KEEPALIVE_INTERVAL_S,
        # Every L2 frame is a FULL book snapshot (~hundreds of levels/side). Keep the frame limit
        # unbounded (trusted exchange feed) so a busy-moment snapshot can never exceed the websockets
        # 1 MiB default and be silently rejected → reconnect-loop (matches Gemini/Coinbase-Advanced).
        max_size=None,
    )
