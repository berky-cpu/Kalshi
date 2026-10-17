"""Shared recorder framework: gap events, order books, backoff, and the IO driver.

Design (per ROADMAP 1.1):

* **Pure message processors** (`MessageProcessor` subclasses, one per venue) parse a raw
  message, update sequence/checksum state, detect gaps, and maintain the order book. They
  touch no socket and no disk, so a replay test can drive them straight from a fixture.
  A gap is NEVER silent: the processor builds a structured `GapDetected`, logs it, records
  it on `self.gaps`, and raises `self.needs_resnapshot` so the IO layer resyncs.

* The **IO layer** (`WebSocketRecorder`) connects, subscribes, stamps every inbound payload
  with ``recv_ts_ns = time.time_ns()`` *on receipt, before parsing*, persists the raw bytes
  untouched via ``recorders/writer.py``, then feeds the processor. On ``needs_resnapshot`` (or
  disconnect) it refetches a snapshot and rebuilds. Reconnects use jittered exponential
  backoff; heartbeats are venue-configurable.

Prices/quantities are kept as ``Decimal`` for exact book maintenance (crypto venues send
decimal strings). This module never handles fees or Kalshi cents — those stay in ``fees.py``.
"""
from __future__ import annotations

import logging
import random
import time
import zlib
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger("kbtc.recorders")
logger.addHandler(logging.NullHandler())

now_ns = time.time_ns

# Normalized side labels used across every venue.
BID = "bid"
ASK = "ask"


def _dec(x: Any) -> Decimal:
    """Parse a price/size into Decimal, tolerating str/int/float/Decimal."""
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"not a numeric value: {x!r}") from exc


# --------------------------------------------------------------------------------------
# Gap events — the heart of the "no silent gap" rule.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class GapDetected:
    """A structured, loggable record that the live book diverged from the feed.

    ``expected``/``got`` are venue-specific (sequence numbers, checksums, update ids) and may
    be ``None`` for venues whose resync trigger is a forced snapshot (e.g. Bybit).
    """

    venue: str
    product: str
    channel: str
    reason: str
    recv_ts_ns: int
    expected: Any = None
    got: Any = None

    def __str__(self) -> str:
        return (
            f"GAP venue={self.venue} product={self.product} channel={self.channel} "
            f"reason={self.reason} expected={self.expected!r} got={self.got!r} "
            f"recv_ts_ns={self.recv_ts_ns}"
        )


def log_gap(gap: GapDetected, log: logging.Logger = logger) -> None:
    """Log a gap at WARNING with structured ``extra`` fields (never silent)."""
    log.warning(
        "%s",
        gap,
        extra={
            "event": "gap_detected",
            "venue": gap.venue,
            "product": gap.product,
            "channel": gap.channel,
            "reason": gap.reason,
            "expected": gap.expected,
            "got": gap.got,
            "recv_ts_ns": gap.recv_ts_ns,
        },
    )


# --------------------------------------------------------------------------------------
# Reconnect backoff.
# --------------------------------------------------------------------------------------
@dataclass
class BackoffPolicy:
    """Jittered exponential backoff. ``delay(n)`` is deterministic given ``rng``.

    Kraken caps reconnects at ~once/5 s after downtime (feeds.md §2) so ``base`` defaults to
    that floor; override per venue as needed.
    """

    base_s: float = 1.0
    factor: float = 2.0
    max_s: float = 60.0
    jitter: float = 0.5  # fractional full-jitter amplitude
    rng: random.Random = field(default_factory=random.Random)

    def delay(self, attempt: int) -> float:
        raw = min(self.max_s, self.base_s * (self.factor ** max(0, attempt)))
        # Full jitter in [raw*(1-jitter), raw*(1+jitter)], clamped to [0, max].
        lo, hi = raw * (1.0 - self.jitter), raw * (1.0 + self.jitter)
        return max(0.0, min(self.max_s, self.rng.uniform(lo, hi)))


# --------------------------------------------------------------------------------------
# Order books.
# --------------------------------------------------------------------------------------
class PriceLevelBook:
    """Aggregated price→size book (Binance/Bybit diff-depth style)."""

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    def _side(self, side: str) -> dict[Decimal, Decimal]:
        return self.bids if side == BID else self.asks

    def set(self, side: str, price: Any, qty: Any) -> None:
        """Absolute set: qty 0 removes the level (Binance step 8 / Bybit amount 0)."""
        d = self._side(side)
        p, q = _dec(price), _dec(qty)
        if q == 0:
            d.pop(p, None)
        else:
            d[p] = q

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def levels(self, side: str, n: int | None = None) -> list[tuple[Decimal, Decimal]]:
        d = self._side(side)
        rev = side == BID  # bids high→low, asks low→high
        out = sorted(d.items(), key=lambda kv: kv[0], reverse=rev)
        return out[:n] if n is not None else out


@dataclass
class Order:
    side: str
    price: Decimal
    qty: Decimal
    price_str: str  # exact transmitted string (needed for Kraken CRC32)
    qty_str: str
    # Exchange order timestamp (ISO-8601 string, as transmitted). Kraken level3 orders a price
    # level by **time priority** (oldest first) for the CRC32, NOT by the order we happened to
    # receive/insert them; a ``modify`` that raises qty or changes price gets a fresh timestamp
    # (loses priority). ``None`` for venues/fixtures that carry no per-order timestamp, in which
    # case ``kraken_crc32`` falls back to insertion order (see that function).
    ts: str | None = None


class OrderIdBook:
    """Order-by-order book keyed by order id (Coinbase level3 / Kraken level3 / Bitstamp).

    Insertion order within a price level is preserved (an ``OrderedDict``), which Kraken's
    CRC32 checksum relies on ("orders in queue order").
    """

    def __init__(self) -> None:
        self.orders: OrderedDict[str, Order] = OrderedDict()

    def add(self, order_id: str, side: str, price: Any, qty: Any,
            ts: str | None = None) -> None:
        ps, qs = str(price), str(qty)
        self.orders[order_id] = Order(side, _dec(price), _dec(qty), ps, qs, ts)

    def modify(self, order_id: str, qty: Any | None = None, price: Any | None = None,
               ts: str | None = None) -> bool:
        o = self.orders.get(order_id)
        if o is None:
            return False  # modify for an order we don't hold — normal, ignore
        if qty is not None:
            o.qty, o.qty_str = _dec(qty), str(qty)
        if price is not None:
            o.price, o.price_str = _dec(price), str(price)
        if ts is not None:
            # Kraken sends the authoritative (possibly fresh) order timestamp on every modify;
            # adopt it so time-priority ordering in the CRC stays correct when priority is lost.
            o.ts = ts
        return True

    def delete(self, order_id: str) -> bool:
        return self.orders.pop(order_id, None) is not None

    def clear(self) -> None:
        self.orders.clear()

    def side_orders(self, side: str) -> list[Order]:
        return [o for o in self.orders.values() if o.side == side]

    def grouped(self, side: str) -> "OrderedDict[Decimal, list[Order]]":
        """price → orders (insertion order preserved within each price)."""
        out: OrderedDict[Decimal, list[Order]] = OrderedDict()
        for o in self.orders.values():
            if o.side == side:
                out.setdefault(o.price, []).append(o)
        return out

    def levels(self, side: str, n: int | None = None) -> list[tuple[Decimal, Decimal]]:
        agg: dict[Decimal, Decimal] = {}
        for o in self.side_orders(side):
            agg[o.price] = agg.get(o.price, Decimal(0)) + o.qty
        rev = side == BID
        out = sorted(agg.items(), key=lambda kv: kv[0], reverse=rev)
        return out[:n] if n is not None else out

    def best_bid(self) -> Decimal | None:
        lv = self.levels(BID, 1)
        return lv[0][0] if lv else None

    def best_ask(self) -> Decimal | None:
        lv = self.levels(ASK, 1)
        return lv[0][0] if lv else None


# --------------------------------------------------------------------------------------
# Kraken v2 level3 CRC32 checksum (feeds.md §2).
# --------------------------------------------------------------------------------------
def _crc_token(s: str) -> str:
    """Remove the decimal point and strip leading zeros (feeds.md worked examples:
    ``44939.5`` → ``449395``; ``4.52308393`` → ``452308393``)."""
    t = s.replace(".", "").lstrip("0")
    return t if t else "0"


def kraken_crc32(book: OrderIdBook, top_levels: int = 10) -> int:
    """CRC32 over the top-``top_levels`` price levels per side, asks then bids.

    Asks are taken price-ascending, bids price-descending; each order contributes ``price`` then
    ``qty`` with the decimal point removed and leading zeros stripped, using the venue's
    *transmitted* strings. Result is cast to unsigned 32-bit. (feeds.md §2.)

    QUEUE ORDER WITHIN A LEVEL (VERIFIED against the real recorded sample, not just the hand-built
    fixture): the orders at a price are taken in **time priority — oldest ``ts`` first**, which is
    the queue order Kraken checksums, NOT the order in which we received/inserted them. These differ
    whenever an order re-enters a level or a ``modify`` refreshes an order's timestamp (an increase
    or price change loses priority and goes to the back). Sorting is stable, so equal timestamps
    keep insertion order. When a level's orders carry no ``ts`` (the string-valued ``level3.jsonl``
    fixture and the official worked example below), we fall back to insertion order — identical to
    the old behaviour, so those oracles are unaffected.

    VERIFIED against Kraken's published L3-guide worked example: the real 21-bid/14-ask input
    reproduces checksum 1063832831 — asserted by
    ``tests/test_recorders_kraken.py::test_kraken_crc32_matches_official_worked_example`` (and
    re-exercised through the normalizer emit path in ``tests/test_normalize_books.py``). Also
    VERIFIED to reproduce the venue checksum on every update of a real recorded hour — see
    ``tests/test_recorders_kraken.py::test_kraken_crc32_matches_real_recording``.
    """
    parts: list[str] = []
    for side, reverse in ((ASK, False), (BID, True)):
        grouped = book.grouped(side)
        prices = sorted(grouped.keys(), reverse=reverse)[:top_levels]
        for price in prices:
            level = grouped[price]
            # Time priority: oldest order first. ``sorted`` is stable, so None/equal timestamps
            # keep insertion order; only order when every order in the level has a timestamp so a
            # mixed (partially-timestamped) level can never be silently misordered.
            if level and all(o.ts is not None for o in level):
                level = sorted(level, key=lambda o: o.ts)
            for o in level:
                parts.append(_crc_token(o.price_str))
                parts.append(_crc_token(o.qty_str))
    payload = "".join(parts).encode("ascii")
    return zlib.crc32(payload) & 0xFFFFFFFF


# --------------------------------------------------------------------------------------
# Pure message-processor base.
# --------------------------------------------------------------------------------------
class MessageProcessor(ABC):
    """Base for a venue's pure, socket-free message processor.

    Subclasses implement :meth:`process` (and usually :meth:`apply_snapshot`). They MUST call
    :meth:`_emit_gap` whenever they detect divergence — that both records/logs the gap and
    raises ``needs_resnapshot`` so the IO layer resyncs.
    """

    venue: str = "base"
    # Book processors resync on a gap; trade/tape processors only LOG the gap (a trade tape has
    # no book to rebuild and no REST snapshot, so flagging needs_resnapshot would be spurious).
    resync_on_gap: bool = True

    def __init__(self, product: str, channel: str, log: logging.Logger = logger) -> None:
        self.product = product
        self.channel = channel
        self.log = log
        self.gaps: list[GapDetected] = []
        self.needs_resnapshot = False
        self.synced = False
        # In-band resync (Kraken/Bybit/Kalshi): True between sending a resubscribe and the fresh
        # snapshot landing. While set, the processor DROPS pre-snapshot frames instead of
        # re-arming ``needs_resnapshot`` — otherwise the IO loop would re-fire resync on every
        # pre-snapshot delta and spam resubscribe frames (Kraken Cloudflare ~150/10 min cap).
        self.awaiting_snapshot = False

    def _emit_gap(
        self, reason: str, recv_ts_ns: int, expected: Any = None, got: Any = None
    ) -> GapDetected:
        gap = GapDetected(
            venue=self.venue,
            product=self.product,
            channel=self.channel,
            reason=reason,
            recv_ts_ns=recv_ts_ns,
            expected=expected,
            got=got,
        )
        self.gaps.append(gap)
        log_gap(gap, self.log)
        if self.resync_on_gap:
            self.needs_resnapshot = True
        return gap

    def clear_resnapshot(self) -> None:
        self.needs_resnapshot = False

    def begin_resync(self) -> None:
        """Enter resync mode before a fresh snapshot is applied.

        Called by the resyncer (see ``recorders/resync.py``) just before it fetches a REST
        snapshot or sends an in-band resubscribe. The default flips ``synced`` off.

        IMPORTANT (how stale frames are actually discarded): in the live loop
        (:func:`run_recorder`) the socket read is *suspended* while the REST fetch awaits, so no
        frames are fed to ``process()`` during the fetch — the ``_pending``/``_buffer`` replay
        inside :meth:`apply_snapshot` is therefore the INITIAL-SYNC / offline-replay path, NOT
        the live-resync discard. The real live-resync discard is the **post-snapshot live-path
        guard** each REST venue runs on the frames that arrive *after* ``apply_snapshot``:
        Coinbase drops ``sequence <= snapshot sequence``; Binance drops ``u < lastUpdateId`` and
        gates the first event on ``U <= lastUpdateId <= u``; Bitstamp drops events whose
        ``microtimestamp <= snapshot microtimestamp``. Venues that resync in-band override this
        to also clear the book and set ``awaiting_snapshot`` (Kraken/Bybit/Kalshi).
        """
        self.synced = False

    @abstractmethod
    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        """Process one raw inbound message; return normalized events (may be empty)."""

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:  # noqa: D401
        """Rebuild state from a freshly fetched snapshot. Default: no-op."""
        raise NotImplementedError


# --------------------------------------------------------------------------------------
# IO driver (integration-level; not unit-tested — the processors are the tested core).
# --------------------------------------------------------------------------------------
@dataclass
class IoConfig:
    """Everything the generic driver needs from a venue adapter.

    ``header_factory`` and ``subscribe_factory`` exist for feeds whose auth is bound to the
    *moment of connection* and must be refreshed on every (re)connect — they are called once per
    connection, never cached:

    * ``header_factory() -> dict`` supplies WebSocket **handshake** headers (Kalshi's
      timestamp+signature RSA-PSS/Ed25519 auth), recomputed per connect so a long-lived reconnect
      never replays a stale signature. ``None`` ⇒ no extra headers (all public crypto feeds).
    * ``subscribe_factory() -> list[dict]`` replaces the static ``subscribe_messages`` when the
      subscribe payload embeds a short-lived credential (Kraken's ``GetWebSocketsToken`` token,
      valid ~900 s) that must be re-fetched before each (re)connect. ``None`` ⇒ use the static
      ``subscribe_messages``.

    Both default to ``None``, so existing public-feed adapters are unaffected. Neither ever logs a
    credential (CLAUDE.md hard rule 2); they are reads-only auth for market-data subscriptions.
    """

    venue: str
    channel: str
    urls: list[str]  # tried in order with fallback
    subscribe_messages: list[dict]
    ping_message: str | None = None
    ping_interval_s: float | None = None
    open_timeout_s: float = 10.0
    header_factory: Any = None  # Callable[[], dict] | None — fresh handshake headers per connect
    subscribe_factory: Any = None  # Callable[[], list[dict]] | None — fresh subscribe per connect


async def run_recorder(  # pragma: no cover - requires a live socket
    config: IoConfig,
    processors: MessageProcessor | list[MessageProcessor],
    writer: Any,
    resync_builder,
    *,
    backoff: BackoffPolicy | None = None,
    stop_event=None,
) -> None:
    """Connect→subscribe→persist→process loop with auto-reconnect and resnapshot.

    ``processors`` may be a single processor or a LIST sharing one connection (e.g. Binance
    diff-depth + aggTrade, or Bybit orderbook + publicTrade on one combined-stream socket).
    Every raw payload is written once (untouched) then routed to EVERY processor; each ignores
    frames it doesn't handle. Only a processor with ``resync_on_gap`` that raises
    ``needs_resnapshot`` triggers ``resnapshot(processor)`` — trade/tape processors only log.

    ``writer`` is a ``RotatingZstdWriter``. ``resync_builder`` is a callable
    ``resync_builder(send) -> resnapshot`` where ``send`` is an async ``send(payload: str)``
    bound to the live socket and ``resnapshot`` is an async ``resnapshot(processor) -> None``.
    Binding ``send`` here is what lets an in-band resyncer (Kraken/Bybit/Kalshi) push a
    resubscribe frame over *this* connection; REST resyncers (Coinbase/Binance/Bitstamp) ignore
    ``send`` and fetch over HTTP. See ``recorders/resync.py`` for the injectable seams (a FAKE
    ``send``/``fetch`` drives the unit tests with no socket). ``stop_event`` is an
    ``asyncio.Event`` for graceful shutdown; on exit the caller must ``writer.close()`` to flush
    the final zstd frame.

    Not exercised by unit tests (needs a live venue); the gap→resnapshot *decision* it relies
    on, and the resync re-anchoring itself, live in the pure processors + resyncers, which ARE
    tested.
    """
    import asyncio
    import json

    import websockets

    procs = list(_ensure_iterable(processors))
    backoff = backoff or BackoffPolicy()
    attempt = 0
    while stop_event is None or not stop_event.is_set():
        url = config.urls[attempt % len(config.urls)]
        try:
            # Fresh handshake headers per connect (Kalshi auth); None for public feeds. Computed
            # before connect and never logged.
            headers = config.header_factory() if config.header_factory else None
            async with websockets.connect(
                url, open_timeout=config.open_timeout_s, additional_headers=headers
            ) as ws:
                # Fresh subscribe per connect when a short-lived token is embedded (Kraken);
                # otherwise the static list. Never logged.
                subs = (
                    config.subscribe_factory()
                    if config.subscribe_factory
                    else config.subscribe_messages
                )
                for msg in subs:
                    await ws.send(json.dumps(msg))
                attempt = 0  # connected cleanly; reset backoff

                async def _send(payload: str) -> None:
                    await ws.send(payload)

                resnapshot = resync_builder(_send)  # bind the resyncer to this connection
                # ROADMAP 1.1c: let a REST-snapshot resyncer (Coinbase/Binance/Bitstamp) PERSIST
                # its fetched snapshot into this same recording (tagged), so an offline replay can
                # rebuild a COMPLETE book instead of starting empty. The snapshot record is stamped
                # at fetch time and written via the SAME ``writer`` as inbound frames, so it is
                # ordered in the stream just before the deltas it precedes. In-band resyncers
                # (Kraken/Bybit/Kalshi) expose no ``persist`` attribute and are left untouched —
                # their snapshot already arrives in the WS stream and is recorded as a normal frame.
                if hasattr(resnapshot, "persist"):
                    resnapshot.persist = lambda record, ts: writer.write(record, recv_ts_ns=ts)

                async def _heartbeat() -> None:
                    if not (config.ping_message and config.ping_interval_s):
                        return
                    while True:
                        await asyncio.sleep(config.ping_interval_s)
                        await ws.send(config.ping_message)

                hb = asyncio.create_task(_heartbeat())
                # On (re)connect, REST-snapshot venues (Coinbase/Binance/Bitstamp) MUST fetch the
                # initial book over HTTP. In-band venues (Kraken/Bybit/Kalshi) already received a
                # snapshot from ``config.subscribe_messages`` above, so we must NOT also fire the
                # resubscribe resyncer here — that would open a SECOND subscription and
                # double-apply deltas. ``resync_on_connect`` makes this correct-by-construction.
                if getattr(resnapshot, "resync_on_connect", True):
                    for p in procs:
                        if p.resync_on_gap:
                            await resnapshot(p)
                try:
                    async for raw in ws:
                        recv = now_ns()  # stamp BEFORE parsing (CLAUDE.md convention)
                        writer.write(raw, recv_ts_ns=recv)  # write once, untouched
                        for p in procs:
                            p.process(raw, recv)
                            if p.needs_resnapshot:
                                p.clear_resnapshot()
                                await resnapshot(p)
                        if stop_event is not None and stop_event.is_set():
                            break
                finally:
                    hb.cancel()
        except Exception as exc:  # noqa: BLE001 - reconnect on any socket/protocol error
            attempt += 1
            delay = backoff.delay(attempt)
            logger.warning(
                "recorder %s/%s disconnected (%s); reconnect in %.2fs",
                config.venue,
                config.channel,
                exc,
                delay,
                extra={"event": "reconnect", "venue": config.venue, "delay_s": delay},
            )
            await asyncio.sleep(delay)


def normalize_side(raw_side: Any) -> str:
    """Map a venue's side token to the internal BID/ASK labels."""
    s = str(raw_side).lower()
    if s in {"buy", "bid", "bids", "b", "0", "yes"}:
        return BID
    if s in {"sell", "ask", "asks", "a", "1", "no"}:
        return ASK
    raise ValueError(f"unknown side token: {raw_side!r}")


def as_dict(raw: Any) -> Any:
    """Parse JSON str/bytes to a Python object; pass dicts/lists through."""
    import json

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _ensure_iterable(x: Any) -> Iterable:  # small helper reused by adapters
    return x if isinstance(x, (list, tuple)) else [x]
