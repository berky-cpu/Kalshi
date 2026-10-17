"""Binance USDⓈ-M perpetual-futures recorder (lead signal, NOT a BRTI constituent).

feeds.md §4. Diff-depth book (`<symbol>@depth`) is a price-level book maintained by the
**futures** sync algorithm (VERIFIED from the "How to manage a local order book correctly"
page). Gap detection is on **`pu`**: each event's ``pu`` must equal the previous event's ``u``
for the symbol; otherwise the book is out of sync → resync from REST
``GET /fapi/v1/depth?limit=1000``.

IMPORTANT: this is the FUTURES rule (``pu == prev.u``), NOT spot's ``U == prev.u + 1``.

Snapshot alignment (verbatim steps 3–6): drop buffered events with ``u < lastUpdateId``; the
first processed event must satisfy ``U <= lastUpdateId <= u``; thereafter chain on ``pu``.
Quantities are absolute (overwrite), qty 0 removes a level, and removing a level you don't hold
is normal.

Diff-depth stream speeds and the U/u/pu field *example*, plus the aggTrade field list and
connection/heartbeat limits, are PARTIAL (feeds.md F-2/F-9) and marked UNVERIFIED below.
Public, no auth.
"""
from __future__ import annotations

from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, PriceLevelBook, as_dict

ENDPOINT = "wss://fstream.binance.com"
REST_SNAPSHOT = "https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=1000"
# UNVERIFIED (feeds.md F-9): stream speeds from a search excerpt, page body did not render.
DEPTH_STREAM = "{symbol}@depth"  # 250 ms; @depth@500ms / @depth@100ms also exist (UNVERIFIED)
AGGTRADE_STREAM = "{symbol}@aggTrade"


class BinanceDepthProcessor(MessageProcessor):
    venue = "binance"

    def __init__(self, product: str = "BTCUSDT", channel: str = "depth", **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self.book = PriceLevelBook()
        self.last_update_id: int | None = None  # REST snapshot lastUpdateId
        self.prev_u: int | None = None  # previous event final update id
        self._buffer: list[dict] = []

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """REST depth snapshot: {"lastUpdateId": N, "bids":[[p,q],...], "asks":[[p,q],...]}.

        Rebuilds the book and replays buffered events per steps 4–6.
        """
        snap = as_dict(snapshot)
        self.book.clear()
        for p, q in snap.get("bids", []):
            self.book.set(BID, p, q)
        for p, q in snap.get("asks", []):
            self.book.set(ASK, p, q)
        self.last_update_id = int(snap["lastUpdateId"])
        self.prev_u = None
        self.synced = True
        self.needs_resnapshot = False
        buffered, self._buffer = self._buffer, []
        for ev in buffered:
            self._apply_event(ev, recv_ts_ns or 0)

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if isinstance(msg, dict) and "data" in msg and "stream" in msg:
            msg = msg["data"]  # combined-stream envelope
        if not isinstance(msg, dict) or msg.get("e") != "depthUpdate":
            return []
        if not self.synced:
            self._buffer.append(msg)
            return []
        return self._apply_event(msg, recv_ts_ns)

    def _apply_event(self, ev: dict, recv_ts_ns: int) -> list[dict]:
        assert self.last_update_id is not None
        U, u, pu = int(ev["U"]), int(ev["u"]), ev.get("pu")

        if self.prev_u is None:
            # First processed event: drop stale, require U <= lastUpdateId <= u (step 4–5).
            if u < self.last_update_id:
                return []
            if not (U <= self.last_update_id <= u):
                self._emit_gap("first-event window", recv_ts_ns,
                               expected=f"U<={self.last_update_id}<=u", got=(U, u))
                return []
        else:
            # Subsequent events chain on pu == prev.u (step 6) — FUTURES rule.
            if pu is None or int(pu) != self.prev_u:
                self._emit_gap("pu chain break", recv_ts_ns, expected=self.prev_u, got=pu)
                return []

        for p, q in ev.get("b", []):
            self.book.set(BID, p, q)  # qty 0 removes (step 8)
        for p, q in ev.get("a", []):
            self.book.set(ASK, p, q)
        self.prev_u = u
        return [{"kind": "book", "u": u}]


class BinanceAggTradeProcessor(MessageProcessor):
    """aggTrade stream. Trade-gap detection on the monotonic aggregate-trade id ``a``.

    UNVERIFIED (feeds.md F-2): the aggTrade field list could not be read from primary this
    session; field names are standard Binance usage.
    """

    venue = "binance"
    resync_on_gap = False  # a trade tape has no book/REST snapshot — log gaps, never resync

    def __init__(self, product: str = "BTCUSDT", channel: str = "aggTrade", **kw: Any) -> None:
        super().__init__(product=product, channel=channel, **kw)
        self.last_agg_id: int | None = None

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if isinstance(msg, dict) and "data" in msg and "stream" in msg:
            msg = msg["data"]
        if not isinstance(msg, dict) or msg.get("e") != "aggTrade":
            return []
        agg_id = int(msg["a"])
        if self.last_agg_id is not None and agg_id <= self.last_agg_id:
            # Out-of-order / duplicate aggTrade id. Log-only (resync_on_gap=False): the IO layer
            # logs it and continues the trade tape without forcing a book resync.
            self._emit_gap("aggTrade id non-monotonic", recv_ts_ns,
                           expected=f">{self.last_agg_id}", got=agg_id)
        self.last_agg_id = agg_id
        return [{"kind": "trade", "price": msg.get("p"), "qty": msg.get("q"),
                 "maker": msg.get("m"), "ts": msg.get("T")}]


def subscribe_messages(symbol: str = "btcusdt") -> list[dict]:
    return [{"method": "SUBSCRIBE",
             "params": [DEPTH_STREAM.format(symbol=symbol),
                        AGGTRADE_STREAM.format(symbol=symbol)],
             "id": 1}]


def io_config(symbol: str = "btcusdt") -> IoConfig:
    return IoConfig(
        venue="binance",
        channel="depth",
        urls=[f"{ENDPOINT}/stream?streams={DEPTH_STREAM.format(symbol=symbol)}/"
              f"{AGGTRADE_STREAM.format(symbol=symbol)}"],
        subscribe_messages=[],  # streams named in the URL; SUBSCRIBE frame optional
        ping_interval_s=None,  # server sends ping frames; websockets auto-pongs (limits UNVERIFIED)
    )
