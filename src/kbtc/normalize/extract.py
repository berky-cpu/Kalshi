"""Pure extraction helpers: L2 levels from a reconstructed book, venue event-time, and the
per-venue trade-tape parsers. No IO, no network.

Prices/sizes are returned as ``Decimal`` (never float); the trade parsers read the venue's exact
transmitted strings. Side semantics are documented per venue — several carry an UNVERIFIED flag
inherited from ``docs/feeds.md`` (Binance aggTrade field list; Kalshi payload).
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from kbtc.recorders.base import ASK, BID, as_dict

logger = logging.getLogger(__name__)

NS_PER_MS = 1_000_000
NS_PER_US = 1_000


def _dec(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------------------
# L2 levels from a reconstructed book.
# ---------------------------------------------------------------------------------------
def l2_levels(book: Any, levels: int) -> list[tuple[str, int, Decimal, Decimal]]:
    """Top-``levels`` per side as ``(side, level_index, price, size)`` rows.

    Works for both :class:`OrderIdBook` (L3 aggregated to price levels) and
    :class:`PriceLevelBook` — both expose ``.levels(side, n)`` returning ``(price, size)`` sorted
    best-first (bids high→low, asks low→high). This is the single L3→L2 aggregation seam.

    ZERO-SIZE LEVELS ARE DROPPED here (red-team #5): ``OrderIdBook`` keeps a ``(price, 0)`` entry
    after a ``change``/``modify`` reduces an order to size 0 without a delete (unlike
    ``PriceLevelBook.set``, which removes a 0-qty level). Such a level is not real resting liquidity
    and must not appear in L2 — nor consume a top-N slot — so we filter ``size == 0`` before taking
    the top ``levels`` (dropping it in the normalize layer, not the recorder book).

    PERFORMANCE: we request a BOUNDED top-``want`` from ``book.levels`` rather than the whole side,
    so a deep L3 book no longer materializes thousands of levels on every event (the 2.2 normalize
    pathology). In the common case (no 0-size levels in the window) a single ``levels(side, levels)``
    call suffices; only when 0-size levels eat into the window do we widen it and retry, still
    yielding the top ``levels`` real levels. ``OrderIdBook.levels`` serves top-N in O(n) from its
    incremental price→qty aggregate.
    """
    out: list[tuple[str, int, Decimal, Decimal]] = []
    for side in (BID, ASK):
        want = levels
        while True:
            lv = book.levels(side, want)
            kept = [(price, size) for price, size in lv if size != 0]
            # Enough real levels, or we've already seen the whole (shorter) side.
            if len(kept) >= levels or len(lv) < want:
                break
            want *= 2  # 0-size levels consumed the window; widen and retry
        for i, (price, size) in enumerate(kept[:levels]):
            out.append((side, i, price, size))
    return out


def book_fingerprint(rows: list[tuple[str, int, Decimal, Decimal]]) -> tuple:
    """Hashable snapshot of the top-N book, to suppress no-op (unchanged top-N) emissions."""
    return tuple((s, lvl, str(p), str(q)) for s, lvl, p, q in rows)


# ---------------------------------------------------------------------------------------
# Venue event-time (exch_ts_ns) — nullable; only where the feed carries one.
# ---------------------------------------------------------------------------------------
def exch_ts_ns(venue: str, msg: Any) -> int | None:
    """Best-effort venue event time in ns, or None. Pure; tolerant of missing fields."""
    v = venue.lower()
    if not isinstance(msg, dict):
        return None
    if v == "binance":
        if "data" in msg and "stream" in msg:
            msg = msg.get("data", {})
        t = msg.get("E")  # event time ms
        return int(t) * NS_PER_MS if t is not None else None
    if v == "bybit":
        t = msg.get("ts")  # ms
        return int(t) * NS_PER_MS if t is not None else None
    if v == "bitstamp":
        micro = (msg.get("data") or {}).get("microtimestamp")
        return int(micro) * NS_PER_US if micro is not None else None
    if v == "coinbase":
        # level3 positional rows have no decoded time here; full-channel objects carry "time".
        t = msg.get("time")
        return _iso_ns(t) if isinstance(t, str) else None
    if v == "coinbase_advanced":
        # Advanced Trade l2_data envelope carries a top-level RFC3339 ``timestamp``.
        t = msg.get("timestamp")
        return _iso_ns(t) if isinstance(t, str) else None
    if v == "kraken":
        data = msg.get("data") or []
        if data and isinstance(data[0], dict):
            ts = data[0].get("timestamp")
            return _iso_ns(ts) if isinstance(ts, str) else None
    return None


def _iso_ns(s: str | None) -> int | None:
    """Parse an ISO-8601 UTC string to integer ns WITHOUT going through a float (red-team #6).

    ``int(dt.timestamp() * 1e9)`` loses ~hundreds of ns to binary-float rounding; instead we take
    the exact integer ``timedelta`` from the epoch (days/seconds/microseconds are all ints) and
    scale microseconds→ns. datetime parses at most microsecond precision, so this is exact for every
    ISO timestamp our feeds emit.
    """
    if not s:
        return None
    from datetime import UTC, datetime
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


# ---------------------------------------------------------------------------------------
# Trade-tape parsers (one per venue that has a tape in our recordings). Each returns a list of
# normalized trade dicts (price/size Decimal; side = taker/aggressor side). Empty for non-trade
# frames. ``side`` conventions are documented inline.
# ---------------------------------------------------------------------------------------
def trade_rows(venue: str, channel: str, raw: Any) -> list[dict]:
    v = venue.lower()
    msg = as_dict(raw)
    if v == "coinbase":
        return _coinbase_trades(msg, channel)
    if v == "binance":
        return _binance_trades(msg, channel)
    if v == "bybit":
        return _bybit_trades(msg, channel)
    if v == "kalshi":
        return _kalshi_trades(msg, channel)
    return []  # kraken level3 / bitstamp live_orders carry no trade tape in our feeds


def _row(recv: int | None, exch: int | None, venue: str, product: str, channel: str,
         trade_id: Any, side: str | None, price: Decimal | None, size: Decimal | None) -> dict:
    return {
        "recv_ts_ns": recv, "exch_ts_ns": exch, "venue": venue, "product": product,
        "channel": channel, "trade_id": None if trade_id is None else str(trade_id),
        "side": side, "price": price, "size": size,
    }


def _coinbase_trades(msg: Any, channel: str) -> list[dict]:
    """Coinbase ``full`` ``match``/``last_match``. Coinbase ``side`` on a match is the *maker*
    order's side; the taker/aggressor side is the OPPOSITE. We store the taker side."""
    if not isinstance(msg, dict) or msg.get("type") not in {"match", "last_match"}:
        return []
    maker_side = msg.get("side")
    taker = {"buy": "sell", "sell": "buy"}.get(str(maker_side).lower()) if maker_side else None
    return [_row(None, exch_ts_ns("coinbase", msg), "coinbase",
                 msg.get("product_id", "BTC-USD"), channel, msg.get("trade_id"),
                 taker, _dec(msg.get("price")), _dec(msg.get("size")))]


def _binance_trades(msg: Any, channel: str) -> list[dict]:
    """Binance ``aggTrade``. ``m`` = "buyer is maker": taker side = sell if m else buy. Field list
    UNVERIFIED (feeds.md F-2); names are standard Binance usage. ``a`` = aggregate trade id."""
    if isinstance(msg, dict) and "data" in msg and "stream" in msg:
        msg = msg["data"]
    if not isinstance(msg, dict) or msg.get("e") != "aggTrade":
        return []
    m = msg.get("m")
    side = ("sell" if m else "buy") if m is not None else None
    t = msg.get("T")
    exch = int(t) * NS_PER_MS if t is not None else None
    return [_row(None, exch, "binance", msg.get("s", "BTCUSDT"), channel, msg.get("a"),
                 side, _dec(msg.get("p")), _dec(msg.get("q")))]


def _bybit_trades(msg: Any, channel: str) -> list[dict]:
    """Bybit ``publicTrade``. ``S`` = taker side ("Buy"/"Sell"); ``i`` = trade id; ``T`` = ms."""
    if not isinstance(msg, dict) or not str(msg.get("topic", "")).startswith("publicTrade."):
        return []
    out: list[dict] = []
    for t in msg.get("data", []):
        s = t.get("S")
        side = str(s).lower() if s else None
        ts = t.get("T")
        exch = int(ts) * NS_PER_MS if ts is not None else None
        out.append(_row(None, exch, "bybit", t.get("s", "BTCUSDT"), channel, t.get("i"),
                        side, _dec(t.get("p")), _dec(t.get("v"))))
    return out


def _kalshi_trades(msg: Any, channel: str) -> list[dict]:
    """Kalshi ``trade`` — VERIFIED from a live authed capture (box, KXBTC15M, 2026-10-17; payload
    nested under ``msg``). Fields: ``trade_id`` (UUID str), ``market_ticker``, ``yes_price_dollars``
    / ``no_price_dollars`` (DOLLAR strings, complements), ``count_fp`` (fixed-point size string),
    ``taker_side`` ("yes"/"no"), ``taker_outcome_side``, ``taker_book_side`` ("bid"/"ask"),
    ``is_block_trade``, ``ts`` (s), ``ts_ms`` (ms).

    EMIT CONVENTION (documented, consistent across the trades dataset): ``price`` = the **YES**
    price in dollars (``yes_price_dollars``; ``no_price_dollars`` is its 1−p complement, not stored
    separately); ``size`` = ``count_fp`` (contracts, Decimal); ``side`` = ``taker_side`` (the
    aggressor's yes/no side, matching the trades-schema "taker side" semantics). ``exch_ts_ns`` from
    ``ts_ms``. All Decimal, never float."""
    if not isinstance(msg, dict) or msg.get("type") != "trade":
        return []
    # Kalshi nests the trade payload under ``msg`` (VERIFIED live); stay robust to a FLAT shape too.
    inner = msg.get("msg")
    body = inner if isinstance(inner, dict) else msg
    # VERIFIED (live wire 2026-10-17) dollar-string YES price + fixed-point size. If either verified
    # field is absent, this is schema drift (Kalshi renamed a field) — surface it LOUDLY and skip the
    # trade rather than silently mis-parsing a differently-scaled legacy field (e.g. cents-int
    # ``yes_price``), which would corrupt the tape without any error.
    price = body.get("yes_price_dollars")
    size = body.get("count_fp")
    if price is None or size is None:
        logger.warning(
            "kalshi trade missing verified field(s) yes_price_dollars/count_fp (schema drift?); "
            "skipping trade %s",
            body.get("trade_id"),
            extra={"event": "kalshi_trade_schema_drift", "venue": "kalshi"},
        )
        return []
    ts_ms = body.get("ts_ms")
    exch = int(ts_ms) * NS_PER_MS if ts_ms is not None else None
    return [_row(None, exch, "kalshi", body.get("market_ticker", "KXBTC15M"), channel,
                 body.get("trade_id"), body.get("taker_side"),
                 _dec(price), _dec(size))]
