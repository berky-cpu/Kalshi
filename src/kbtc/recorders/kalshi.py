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
* Orderbook WS schema (VERIFIED, docs.kalshi.com/websockets/orderbook-updates, read 2026-10-17):
  Envelope top-level ``type`` ("orderbook_snapshot" | "orderbook_delta"), ``sid`` (int, required),
  ``seq`` (int, required — "Sequential number that should be checked"), optional ``id``,
  ``sending_ts_ms``.
  - **orderbook_snapshot**: ``market_ticker`` (str), ``market_id`` (str UUID),
    ``yes_dollars_fp`` (OPTIONAL array — absent if no yes offers), ``no_dollars_fp`` (OPTIONAL).
    Each level is a 2-item array of STRINGS ``[price_in_dollars, count_fp]`` e.g.
    ``["0.0800","300.00"]`` — price is a DOLLAR string, size a fixed-point count string.
  - **orderbook_delta**: ``market_ticker`` (str), ``market_id`` (str), ``price_dollars`` (str,
    dollars), ``delta_fp`` (str, SIGNED fixed-point contract delta, 2 decimals — e.g. "-54.00",
    a signed change to resting size at that price/side), ``side`` ("yes" | "no"), optional
    ``client_order_id``, ``ts`` (deprecated RFC3339), ``ts_ms`` (int), ``subaccount``.

UNVERIFIED — need a LIVE capture to close (Kalshi WS requires auth; the box has no Kalshi creds
yet — flagged to Berk):
* ``msg`` WRAPPER AMBIGUITY. The margin-ws schema nests the payload under ``msg``
  (``{"type","sid","seq","msg":{...}}``); the official event-markets orderbook-updates example
  shows the payload fields FLAT (top-level). We parse ROBUSTLY to BOTH (look under ``msg`` if
  present, else top-level) via :func:`_event_body`. Which one KXBTC15M actually emits is
  UNVERIFIED until a real frame is captured.
* SEQ SCOPE + RESET. The doc says ``seq`` "should be checked" but does NOT state per-sid vs
  shared-across-subscription scope, nor reset-on-reconnect behaviour. A community source (dev.to)
  reports ``seq`` is shared across the whole subscription and a gap should resubscribe the whole
  subscription (which matches our in-band resync). We keep per-sid +1 tracking below; scope and
  reset semantics remain UNVERIFIED pending a live capture.

Prices are DOLLAR strings and sizes fixed-point count strings, both parsed to exact ``Decimal``
(never float).
"""
from __future__ import annotations

import os
from collections import deque
from decimal import Decimal, InvalidOperation
from typing import Any

from .base import IoConfig, MessageProcessor, as_dict

# READS ONLY. Order endpoints are forbidden here (guard enforces demo-only for orders).
ENDPOINT_PROD = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"  # VERIFIED (quick_start)
ENDPOINT_PROD_LEGACY = "wss://api.elections.kalshi.com/trade-api/ws/v2"  # VERIFIED (still ok)
ENDPOINT_DEMO = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"  # VERIFIED
WS_SIGN_PATH = "/trade-api/ws/v2"  # path signed for the handshake (VERIFIED)

_LIFECYCLE = {"market_lifecycle", "market_lifecycle_v2", "market_lifecycle_update"}
# Terminal lifecycle statuses → evict the market's book (FIX 2). Conservative set: only
# unambiguously-settled states.
_SETTLED_LIFECYCLE_STATUSES = frozenset({"finalized", "settled", "closed", "determined"})
# Snapshot side arrays → internal book side label (VERIFIED field names).
_SNAPSHOT_SIDES = (("yes_dollars_fp", "yes"), ("no_dollars_fp", "no"))


def _event_body(msg: dict) -> dict:
    """Return the event payload fields. Robust to the UNVERIFIED ``msg``-wrapper ambiguity: use
    ``msg["msg"]`` if a dict is nested there (margin-ws style), else the top-level object itself
    (the official event-markets orderbook-updates example shows fields FLAT). A live capture must
    confirm which KXBTC15M emits (1.1b)."""
    inner = msg.get("msg")
    return inner if isinstance(inner, dict) else msg


def _dec(x: Any) -> Decimal:
    """Parse a dollar/fixed-point STRING (or number) to exact Decimal (never float)."""
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):  # pragma: no cover - defensive
        raise ValueError(f"not a numeric Kalshi value: {x!r}") from None


class KalshiProcessor(MessageProcessor):
    venue = "kalshi"

    # Memory bound (FIX 2, 11h-outage incident 2026-10-18): ``trades``/``lifecycle`` are live
    # introspection/debug tails only — the AUTHORITATIVE copy of every frame is the untouched raw
    # written by ``writer.py``, and offline ``normalize`` reads from those files, never from these.
    # In a long 24/7 run an unbounded ``list.append`` per message grew to GBs (the process peaked at
    # 2.3 GB → SIGKILL). Bounded ``deque``s keep only a recent tail; tests still index ``[0]`` and
    # read ``len`` normally.
    TRADES_MAXLEN = 4096
    LIFECYCLE_MAXLEN = 1024

    def __init__(self, product: str = "KXBTC15M", channel: str = "orderbook_delta",
                 **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        # Book per market_ticker: {side_label: {price: size}}. Side labels are whatever the feed
        # uses — yes/no (event markets, VERIFIED) or bid/ask (perps, VERIFIED). A market's book is
        # EVICTED (``evict_market``) when the dynamic manager drops it (delete_markets / grace) or
        # when a lifecycle frame reports it settled, so ``books`` stays bounded to live + upcoming +
        # recently-closed markets rather than growing by ~45 markets/11h forever (FIX 2).
        self.books: dict[str, dict[str, dict[Any, Any]]] = {}
        self._seq_by_sid: dict[int, int] = {}
        self.trades: deque[dict] = deque(maxlen=self.TRADES_MAXLEN)
        self.lifecycle: deque[dict] = deque(maxlen=self.LIFECYCLE_MAXLEN)
        # Dynamic-subscription support (ROADMAP 1.3): capture the server-assigned subscription id
        # per channel from the ``subscribed`` ack so the dynamic manager can target
        # ``update_subscription`` at the right ``sid`` without a reconnect. VERIFIED on the wire
        # (ack ``msg`` = ``{channel, sid}``); re-captured fresh on every (re)connect.
        self.sid_by_channel: dict[str, int] = {}

    def book_for(self, ticker: str) -> dict[str, dict[Any, Any]]:
        return self.books.setdefault(ticker, {})

    def evict_market(self, ticker: str) -> None:
        """Drop all per-market state for ``ticker`` (FIX 2: bounded memory across many rolls).

        Called by the dynamic poller when a market is removed from the subscription (after its
        grace window) and on a settled lifecycle frame. Idempotent — evicting an unknown ticker is a
        no-op. ``_seq_by_sid`` is keyed by SID (shared across markets on one subscription), so it is
        NOT touched here; it is re-anchored by each snapshot and bounded by the channel count."""
        self.books.pop(ticker, None)

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
        body = _event_body(msg)

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
            # Record the raw trade payload untouched (no field access → cannot crash on schema
            # drift); the VERIFIED trade schema (trade_id/yes_price_dollars/no_price_dollars/
            # count_fp/taker_side/..., live-captured 2026-10-17) is parsed in normalize.extract.
            self.trades.append(body)
            return [{"kind": "trade", **body}]

        if typ in _LIFECYCLE:
            self.lifecycle.append(body)
            # FIX 2: evict a market's book as soon as it settles, so finalized markets don't pile up
            # in ``books`` for hours (the grace-removal path also evicts, this is the earlier of the
            # two triggers). Status key/taxonomy may vary; only a clearly-terminal status evicts.
            status = body.get("status")
            ticker = body.get("market_ticker")
            if ticker and isinstance(status, str) and status in _SETTLED_LIFECYCLE_STATUSES:
                self.evict_market(ticker)
            return [{"kind": "lifecycle", **body}]

        if typ == "subscribed":
            # Capture channel → server sid from the ack (VERIFIED body ``{channel, sid}``). The sid
            # may also appear top-level; prefer the body's. Lets the dynamic manager add/remove
            # markets on this exact subscription with no reconnect (ROADMAP 1.3).
            self._capture_subscribed(body, sid)
            return []

        if typ == "error":
            # An app-level rejection (e.g. code 26 = per-subscription market limit, 27 = command
            # rate limit — ROADMAP 1.3 / docs.kalshi.com/websockets/websocket-connection). The raw
            # frame is already persisted by the IO layer; surfacing it at WARNING keeps CLAUDE.md's
            # "never silent" rule on the dynamic subscription path (a silently-dropped reject would
            # let the recorder believe it is subscribed to a market it is not). NOT a book gap: no
            # ``_emit_gap`` (would spuriously arm resync), no crash.
            self._log_error(body, msg)
            return []

        return []  # ok/unsubscribed control frames

    def _capture_subscribed(self, body: dict, top_sid: Any) -> None:
        channel = body.get("channel")
        ack_sid = body.get("sid", top_sid)
        if isinstance(channel, str) and ack_sid is not None:
            self.sid_by_channel[channel] = int(ack_sid)

    def reset_subscriptions(self) -> None:
        """Forget captured per-channel sids. MUST be called on every (re)connect: the processor
        instance is reused across reconnects, so a stale sid from the previous connection would let
        the dynamic poller target a DEAD subscription → silent rejection. Clearing here means
        ``DynamicSubscriptionManager.ready()`` correctly waits for the FRESH ``subscribed`` acks
        before any ``update_subscription`` is sent (ROADMAP 1.3 F2)."""
        self.sid_by_channel.clear()
        # FIX 2: a reconnect gets brand-new server sids; drop the old per-sid seq anchors so this
        # dict can't accumulate dead sids across many reconnects (each snapshot re-anchors anyway).
        self._seq_by_sid.clear()

    def _log_error(self, body: dict, msg: dict) -> None:
        code = body.get("code", msg.get("code"))
        detail = body.get("msg") or body.get("message")
        if isinstance(detail, dict):
            detail = detail.get("msg") or detail.get("message")
        self.log.warning(
            "kalshi WS error frame: code=%s detail=%s (raw persisted; subscription may be rejected)",
            code, detail,
            extra={"event": "kalshi_ws_error", "venue": self.venue, "code": code},
        )

    def _apply_snapshot_body(self, body: dict) -> None:
        """Rebuild the book from the VERIFIED snapshot: ``yes_dollars_fp``/``no_dollars_fp`` (both
        OPTIONAL), each a list of ``[price_dollars_str, count_fp_str]``. Prices/sizes → exact
        Decimal (dollars / fixed-point counts)."""
        ticker = body.get("market_ticker", self.product)
        book: dict[str, dict[Any, Any]] = {}
        for key, label in _SNAPSHOT_SIDES:
            rows = body.get(key)
            if not rows:
                continue  # OPTIONAL side: absent when there are no offers on it
            level = book.setdefault(label, {})
            for price_dollars, count_fp in rows:
                level[_dec(price_dollars)] = _dec(count_fp)
        self.books[ticker] = book

    def _apply_delta(self, body: dict) -> None:
        """Apply a VERIFIED delta: SIGNED ``delta_fp`` change to the resting size at
        ``(side, price_dollars)``; drop the level if it reaches <= 0."""
        ticker = body.get("market_ticker", self.product)
        book = self.book_for(ticker)
        side = body["side"]  # "yes" | "no"
        level = book.setdefault(side, {})
        price = _dec(body["price_dollars"])
        new = level.get(price, Decimal(0)) + _dec(body["delta_fp"])
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


# update_subscription actions (VERIFIED, docs.kalshi.com/websockets/websocket-connection,
# 2026-10-17): add/remove markets on a LIVE subscription without reconnecting — keeps the same sid.
UPDATE_ACTIONS = ("add_markets", "delete_markets", "get_snapshot")


def update_subscription_message(
    sid: int, market_tickers: list[str], action: str, msg_id: int
) -> dict:
    """Build a VERIFIED ``update_subscription`` frame for one subscription (``sid``).

    ``{"id":N,"cmd":"update_subscription","params":{"sids":[sid],"market_tickers":[...],
    "action":...}}`` — ``sids`` is an array of EXACTLY ONE sid (the doc allows singular ``sid`` or
    the one-element ``sids`` array, never both; we use the array form). ``action`` ∈
    ``add_markets`` / ``delete_markets`` / ``get_snapshot``. Adding a market makes Kalshi emit an
    ``orderbook_snapshot`` for it on the SAME sid (VERIFIED). Reads only — never an order path.
    """
    if action not in UPDATE_ACTIONS:
        raise ValueError(f"invalid update_subscription action: {action!r}")
    return {
        "id": int(msg_id),
        "cmd": "update_subscription",
        "params": {
            "sids": [int(sid)],
            "market_tickers": list(market_tickers),
            "action": action,
        },
    }


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
