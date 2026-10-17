"""Kalshi market-data recorder: `orderbook_delta`, `trade`, and market lifecycle.

CLAUDE.md HARD RULE 1: this recorder reads MARKET DATA ONLY. Kalshi's *production* host may be
used here for reads; NO order path exists or may be added in this module (order sends must go
through ``kbtc.execution.guard`` on the demo host — never from a recorder).

VERIFICATION (primary, docs.kalshi.com, read 2026-10-16):
* Endpoints (VERIFIED, quick_start_websockets): production
  ``wss://external-api-ws.kalshi.com/trade-api/ws/v2`` (legacy
  ``wss://api.elections.kalshi.com/trade-api/ws/v2`` still supported); demo
  ``wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2``.
* Subscribe (VERIFIED): ``{"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta"],
  "market_tickers":[...]}}`` (singular ``market_ticker`` also accepted).
* Auth (VERIFIED): handshake headers ``KALSHI-ACCESS-KEY`` (key id),
  ``KALSHI-ACCESS-SIGNATURE``, ``KALSHI-ACCESS-TIMESTAMP`` (unix ms). Signature = sign of
  ``timestamp + "GET" + "/trade-api/ws/v2"`` with the private key (RSA-PSS/SHA-256 for RSA, or
  Ed25519), base64-encoded. Even public channels require an authenticated session; we read only.
* Frame shape (VERIFIED, margin-ws orderbook schema): ``{"type", "sid", "seq", "msg": {...}}``
  where ``sid`` = server-generated subscription identifier and ``seq`` = "Sequential number that
  should be checked if you want to guarantee you received all the messages."

UNVERIFIED (could NOT confirm from the official main event-markets WS reference this session —
the `/websockets` reference page and `getting_started/websocket_connection` 404'd / were not
captured; see report. Treat the following as PARTIAL until the lead closes them):
* The orderbook_snapshot PAYLOAD for KXBTC15M event markets. The margin/perps schema uses
  ``msg.bid`` / ``msg.ask`` arrays of ``[price, count]`` strings; a secondary client wrapper
  reports the event-markets snapshot uses ``yes`` / ``no`` arrays. We therefore accept BOTH side
  vocabularies below, but which one KXBTC15M emits is UNVERIFIED.
* ``orderbook_delta`` payload: fields ``market_ticker``, ``price``, ``delta``, ``side`` are
  VERIFIED in the margin schema with ``side`` ∈ {``bid``,``ask``} as STRINGS; for event markets
  the ``yes``/``no`` side tokens and whether price is cents-int vs dollars-string are UNVERIFIED.
* SEQ GAP SEMANTICS: the docs say seq "should be checked"; they do NOT state it is per-sid or
  strictly +1. The +1-per-sid gap rule below is a reasonable inference, flagged UNVERIFIED.

So prices/sizes are coerced tolerantly (int cents OR decimal-dollar strings) and both side
vocabularies are accepted.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any

from .base import IoConfig, MessageProcessor, as_dict

# READS ONLY. Order endpoints are forbidden here (guard enforces demo-only for orders).
ENDPOINT_PROD = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"  # VERIFIED (quick_start)
ENDPOINT_PROD_LEGACY = "wss://api.elections.kalshi.com/trade-api/ws/v2"  # VERIFIED (still ok)
ENDPOINT_DEMO = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"  # VERIFIED
WS_SIGN_PATH = "/trade-api/ws/v2"  # path signed for the handshake (VERIFIED)

_LIFECYCLE = {"market_lifecycle", "market_lifecycle_v2", "market_lifecycle_update"}
# Side keys accepted on the book (event-markets yes/no UNVERIFIED; perps bid/ask VERIFIED).
_SIDE_KEYS = ("yes", "no", "bid", "ask")


def _num(x: Any) -> Any:
    """Coerce a price/size that may be an int, a cents-int string, or a dollar string."""
    if isinstance(x, (int,)):
        return x
    s = str(x)
    try:
        return int(s)
    except ValueError:
        try:
            return Decimal(s)
        except InvalidOperation:
            return s


class KalshiProcessor(MessageProcessor):
    venue = "kalshi"

    def __init__(self, product: str = "KXBTC15M", channel: str = "orderbook_delta",
                 **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        # Book per market_ticker: {side_label: {price: size}}. Side labels are whatever the feed
        # uses — yes/no (event markets, UNVERIFIED) or bid/ask (perps, VERIFIED).
        self.books: dict[str, dict[str, dict[Any, Any]]] = {}
        self._seq_by_sid: dict[int, int] = {}
        self.trades: list[dict] = []
        self.lifecycle: list[dict] = []

    def book_for(self, ticker: str) -> dict[str, dict[Any, Any]]:
        return self.books.setdefault(ticker, {})

    def known_sids(self) -> list[int]:
        """Subscription ids currently tracked — used to unsubscribe the old subscription before a
        resync resubscribes (so a resync never opens a second concurrent sub → double-count)."""
        return sorted(self._seq_by_sid.keys())

    def begin_resync(self) -> None:
        """In-band resync: drop ``synced`` and set ``awaiting_snapshot`` so frames from the OLD
        subscription arriving before the fresh ``orderbook_snapshot`` are dropped (not applied
        twice, not re-arming resync) until the new snapshot lands. UNVERIFIED (1.1b)."""
        self.synced = False
        self.awaiting_snapshot = True

    def _check_seq(self, sid: Any, seq: Any, recv_ts_ns: int) -> bool:
        """Return True if this message is in-sequence for its sid; emit a gap otherwise."""
        if sid is None or seq is None:
            return True  # frame without seq (some lifecycle frames)
        sid, seq = int(sid), int(seq)
        last = self._seq_by_sid.get(sid)
        if last is None:
            self._seq_by_sid[sid] = seq
            return True
        expected = last + 1
        if seq < expected:
            return False  # duplicate / out-of-order — caller ignores (no gap)
        if seq > expected:
            self._emit_gap("seq jump", recv_ts_ns, expected=expected, got=seq)
            self._seq_by_sid[sid] = seq  # advance so we don't refire every subsequent msg
            return False
        self._seq_by_sid[sid] = seq
        return True

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict):
            return []
        typ = msg.get("type")
        sid, seq = msg.get("sid"), msg.get("seq")
        body = msg.get("msg", {})

        # After a resubscribe, suppress everything until the fresh orderbook_snapshot lands: this
        # drops any trailing frames from the OLD subscription (so no double-apply) and prevents
        # resubscribe-frame thrash. UNVERIFIED (1.1b).
        if self.awaiting_snapshot and typ != "orderbook_snapshot":
            return []

        if typ == "orderbook_snapshot":
            # Snapshot always (re)baselines seq for this sid and rebuilds the book.
            if sid is not None and seq is not None:
                self._seq_by_sid[int(sid)] = int(seq)
            self._apply_snapshot_body(body)
            self.synced = True
            self.needs_resnapshot = False
            self.awaiting_snapshot = False  # fresh snapshot landed; resume
            return [{"kind": "snapshot", "ticker": body.get("market_ticker")}]

        if typ == "orderbook_delta":
            if not self._check_seq(sid, seq, recv_ts_ns):
                return []
            self._apply_delta(body)
            return [{"kind": "book", "ticker": body.get("market_ticker")}]

        if typ == "trade":
            if not self._check_seq(sid, seq, recv_ts_ns):
                return []
            self.trades.append(body)
            return [{"kind": "trade", **body}]

        if typ in _LIFECYCLE:
            self.lifecycle.append(body)
            return [{"kind": "lifecycle", **body}]

        return []  # subscribed/ok/error control frames

    def _apply_snapshot_body(self, body: dict) -> None:
        ticker = body.get("market_ticker", self.product)
        book: dict[str, dict[Any, Any]] = {}
        for side in _SIDE_KEYS:
            rows = body.get(side)
            if not rows:
                continue
            book.setdefault(side, {})
            for price, size in rows:
                book[side][_num(price)] = _num(size)
        self.books[ticker] = book

    def _apply_delta(self, body: dict) -> None:
        ticker = body.get("market_ticker", self.product)
        book = self.book_for(ticker)
        side = body["side"]
        level = book.setdefault(side, {})
        price = _num(body["price"])
        new = level.get(price, 0) + _num(body["delta"])
        if new <= 0:
            level.pop(price, None)
        else:
            level[price] = new

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Resync entry point: feed the fresh orderbook_snapshot frame from a resubscribe."""
        self.needs_resnapshot = False
        self.process(snapshot, recv_ts_ns or 0)


def auth_headers(timestamp_ms: str, signer) -> dict[str, str]:
    """Build the VERIFIED handshake headers from env + a caller-supplied ``signer``.

    ``signer(message: bytes) -> bytes`` signs ``timestamp + "GET" + "/trade-api/ws/v2"`` with the
    account private key (RSA-PSS/SHA-256 or Ed25519); the result is base64-encoded. The private
    key is never read or logged here — only the key id comes from ``KALSHI_API_KEY_ID``. We sign
    solely to open a read-only market-data session; no order path exists in a recorder.
    """
    import base64

    key_id = os.environ["KALSHI_API_KEY_ID"]
    message = f"{timestamp_ms}GET{WS_SIGN_PATH}".encode()
    signature = base64.b64encode(signer(message)).decode()
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
    }


def subscribe_messages(tickers: list[str] | None = None,
                       channels: tuple[str, ...] = ("orderbook_delta", "trade")) -> list[dict]:
    params: dict[str, Any] = {"channels": list(channels)}
    if tickers:
        params["market_tickers"] = tickers
    return [{"id": 1, "cmd": "subscribe", "params": params}]


def unsubscribe_messages(sids: list[int]) -> list[dict]:
    """Unsubscribe the given subscription ids before a resync resubscribes.

    UNVERIFIED (1.1b): the event-markets WS reference could not be read this session, so the
    ``{"cmd":"unsubscribe","params":{"sids":[...]}}`` envelope is inferred (the margin/perps docs
    show ``sid`` as the subscription identifier). Reads only — never an order path.
    """
    return [{"id": 1, "cmd": "unsubscribe", "params": {"sids": list(sids)}}]


def resubscribe_messages(tickers: list[str] | None = None) -> list[dict]:
    """Resubscribe to the orderbook channel to pull a fresh ``orderbook_snapshot``.

    UNVERIFIED (1.1b): the official event-markets WS reference could not be read this session,
    so neither the unsubscribe-by-``sid`` envelope nor the guarantee that a resubscribe re-emits
    an ``orderbook_snapshot`` is confirmed. We therefore send a plain ``subscribe`` for the
    orderbook channel as the resync trigger and keep the flag UNVERIFIED. Reads only — this is
    NEVER an order path (CLAUDE.md hard rule 1).
    """
    return subscribe_messages(tickers, channels=("orderbook_delta",))


def io_config(tickers: list[str] | None = None) -> IoConfig:
    return IoConfig(
        venue="kalshi",
        channel="orderbook_delta",
        urls=[ENDPOINT_PROD, ENDPOINT_PROD_LEGACY],  # reads only; never an order endpoint
        subscribe_messages=subscribe_messages(tickers),
        ping_interval_s=None,
    )
