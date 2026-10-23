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

from sortedcontainers import SortedDict

logger = logging.getLogger("kbtc.recorders")
logger.addHandler(logging.NullHandler())

now_ns = time.time_ns

_ZERO = Decimal(0)

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
# Data-staleness watchdog — the self-heal for a "connected but no data" stall.
# --------------------------------------------------------------------------------------
@dataclass
class DataStalenessMonitor:
    """Decide when a connection that is still UP but delivering no DATA must be force-reconnected.

    The 2026-10-18 incident: after a restart Kraken/Kalshi stayed connected and emitted only
    heartbeats/acks for ~11 h; the socket never dropped, so disconnect-based reconnect never fired
    and nothing self-healed. This monitor measures time since the last DATA frame and declares the
    feed stale after ``timeout_s`` — forcing ``run_recorder`` to reconnect (which re-runs
    header_factory / subscribe_factory / on_connect: Kraken re-fetches its token, Kalshi
    re-discovers + re-subscribes the live market).

    ``classifier(raw) -> bool`` is the venue data-frame classifier (``health.data_classifier``), so
    "DATA" means EXACTLY what the health report means — heartbeats, subscription acks, pongs and
    status/error frames do NOT reset the clock (that heartbeat-only state IS the failure). A
    ``None`` classifier counts every frame (health's fallback for an unclassified venue).

    Pure and socket-free (unit-tested like the gap logic). The baseline resets on each (re)connect
    (:meth:`on_connect`), so a fresh connection always gets the full ``timeout_s`` of grace before
    it can trip: it cannot false-trip a healthy feed, only one genuinely silent of data.
    """

    timeout_s: float | None
    classifier: Any = None  # Callable[[Any], bool] | None
    last_data_ns: int = 0
    connected_since_ns: int = 0

    def on_connect(self, now_ns: int) -> None:
        """Reset the baseline for a fresh connection (no data yet; grace starts now)."""
        self.connected_since_ns = now_ns
        self.last_data_ns = 0

    def observe(self, raw: Any, recv_ns: int) -> bool:
        """Record a frame. Returns True (and advances the freshness clock) iff it is a DATA frame."""
        if self.classifier is None or self.classifier(raw):
            self.last_data_ns = recv_ns
            return True
        return False

    def is_stale(self, now_ns: int) -> bool:
        """True once no DATA frame has arrived for ``timeout_s`` (measured from the last data frame,
        or from connect if none yet). ``timeout_s=None`` disables (never stale)."""
        if self.timeout_s is None:
            return False
        if self.connected_since_ns <= 0:
            return False  # on_connect not called yet — no baseline, cannot be stale
        # Measure from the last DATA frame, or from connect if none has arrived yet (so a fresh
        # connection gets the full timeout of grace). ``now_ns`` is time.time_ns() in the live loop.
        baseline = self.last_data_ns or self.connected_since_ns
        return (now_ns - baseline) >= int(self.timeout_s * 1_000_000_000)


# --------------------------------------------------------------------------------------
# Order books.
# --------------------------------------------------------------------------------------
class PriceLevelBook:
    """Aggregated price→size book (Binance/Bybit diff-depth / Coinbase level2 style).

    PERFORMANCE (REV-013, extended from ``OrderIdBook``): ``bids``/``asks`` are kept sorted by
    price in a ``SortedDict`` (keyed Decimal price ascending), which IS the authoritative
    price→qty aggregate — there is no separate order store to reconcile. So ``levels(side, n)``
    reads the top-``n`` off the maintained ends in O(n), and ``best_bid``/``best_ask`` read a single
    end, instead of the old ``sorted(d.items())`` over ALL price levels on EVERY event. That full
    re-sort pegged a core normalizing Coinbase level2 (its opening snapshot alone is ~40k levels,
    and ``_emit_book`` calls ``levels`` per event). ``SortedDict`` subclasses ``dict``, so
    ``bids``/``asks`` keep dict semantics (``[]``/``in``/``== {...}``) for existing callers/tests.

    NOTE: iterating ``bids``/``asks`` directly now yields keys in ascending-price order (SortedDict),
    not plain-dict insertion order. No current caller relies on insertion order (REV-024 F1); use
    ``levels()`` for ordered top-of-book rather than assuming the attribute's iteration order.
    """

    def __init__(self) -> None:
        self.bids: SortedDict = SortedDict()
        self.asks: SortedDict = SortedDict()

    def _side(self, side: str) -> SortedDict:
        return self.bids if side == BID else self.asks

    def set(self, side: str, price: Any, qty: Any) -> None:
        """Absolute set: qty 0 removes the level (Binance step 8 / Bybit amount 0)."""
        d = self._side(side)
        p, q = _dec(price), _dec(qty)
        if q == 0:
            d.pop(p, None)  # removal path: drop the level from the sorted aggregate
        else:
            d[p] = q

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def best_bid(self) -> Decimal | None:
        return self.bids.keys()[-1] if self.bids else None  # highest bid price

    def best_ask(self) -> Decimal | None:
        return self.asks.keys()[0] if self.asks else None  # lowest ask price

    def levels(self, side: str, n: int | None = None) -> list[tuple[Decimal, Decimal]]:
        """Top-``n`` ``(price, qty)`` best-first (bids high→low, asks low→high); O(n) off the
        maintained sorted aggregate. ``n is None`` → all; ``n <= 0`` → ``[]``."""
        d = self._side(side)
        keys = d.keys()
        if n is not None and n <= 0:
            return []
        if side == BID:  # highest prices first
            sel = reversed(keys if n is None else keys[-n:])
        else:  # lowest prices first
            sel = iter(keys if n is None else keys[:n])
        return [(p, d[p]) for p in sel]

    def trim(self, side: str, n: int) -> None:
        """Keep only the top-``n`` levels on ``side`` (best-first), evicting the rest.

        For a DEPTH-CAPPED venue feed (e.g. Crypto.com ``book.BTC_USD.50``) the venue only ever
        transmits its top-``n`` by price; a level pushed OUT of that window (a bid that drops below
        rank-``n`` after an uptick, say) arrives as NO event — it is simply absent, not a qty-0
        remove — so an absolute-SET book keeps it as a stale "ghost" until the next reseed, where it
        can resurface as a false ``best_bid``/``best_ask`` (crossed book; REV-029 F1 / the
        Bitstamp-reseed class). Trimming back to the venue's window after every apply evicts it.

        Efficient: slices the FAR end off the maintained ``SortedDict`` — O(k) in the number
        evicted, not a re-sort. A no-op when the side already holds ≤ ``n`` levels, so it can never
        drop real in-window liquidity. ``n <= 0`` clears the side.
        """
        d = self._side(side)
        excess = len(d) - n
        if excess <= 0:
            if n <= 0:
                d.clear()
            return
        keys = d.keys()
        # Drop the WORST levels (the far end): bids keep the highest prices (evict the lowest, the
        # low end); asks keep the lowest prices (evict the highest, the high end). Materialise the
        # slice before deleting — mutating a SortedDict invalidates its live key view.
        drop = list(keys[:excess]) if side == BID else list(keys[-excess:])
        for p in drop:
            del d[p]


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

    PERFORMANCE (offline-normalize pathology fix): ``self.orders`` is the authoritative store and
    keeps its ordering/semantics (``grouped``/``side_orders`` feed ``kraken_crc32`` by time
    priority within a level — do not change that). On top of it we maintain a DERIVED, per-side
    ``price → summed-qty`` aggregate (``_agg``) in a ``SortedDict``, updated INCREMENTALLY on every
    ``add``/``modify``/``delete``/``clear``. This makes ``levels(side, n)`` cheap top-N (no full
    O(N-orders) scan + re-sort per event, which pegged a core on multi-hour L3 replays). A separate
    ``_count`` (price → number of resting orders) tells us when a price level empties, so a price
    key survives even while its summed qty is 0 (a ``modify`` to qty 0 without a delete keeps the
    order resting; the 0-size level is dropped later in ``normalize.extract.l2_levels``, matching the
    old full-scan behaviour). The aggregate is a cache kept EXACTLY in sync with ``self.orders`` —
    the property test in ``tests/test_recorders_base.py`` asserts it equals a from-scratch scan at
    every step.
    """

    def __init__(self) -> None:
        self.orders: OrderedDict[str, Order] = OrderedDict()
        # Derived top-N cache (see class docstring). Keyed by Decimal price, ascending; bids read
        # from the high end, asks from the low end.
        self._agg: dict[str, SortedDict] = {BID: SortedDict(), ASK: SortedDict()}
        self._count: dict[str, dict[Decimal, int]] = {BID: {}, ASK: {}}

    # -- incremental aggregate maintenance (private) ------------------------------------
    def _agg_add(self, side: str, price: Decimal, qty: Decimal) -> None:
        agg, cnt = self._agg[side], self._count[side]
        agg[price] = agg.get(price, _ZERO) + qty
        cnt[price] = cnt.get(price, 0) + 1

    def _agg_remove(self, side: str, price: Decimal, qty: Decimal) -> None:
        agg, cnt = self._agg[side], self._count[side]
        c = cnt.get(price, 0) - 1
        if c <= 0:
            cnt.pop(price, None)
            agg.pop(price, None)
        else:
            cnt[price] = c
            agg[price] = agg[price] - qty

    def add(self, order_id: str, side: str, price: Any, qty: Any,
            ts: str | None = None) -> None:
        old = self.orders.get(order_id)
        if old is not None:
            # Re-add under an existing id (overwrite): drop the old contribution first so the
            # aggregate stays coherent. ``self.orders[id] = ...`` below keeps the OrderedDict slot
            # (reassigning an existing key does not move it), preserving queue order for the CRC.
            self._agg_remove(old.side, old.price, old.qty)
        ps, qs = str(price), str(qty)
        o = Order(side, _dec(price), _dec(qty), ps, qs, ts)
        self.orders[order_id] = o
        self._agg_add(side, o.price, o.qty)

    def modify(self, order_id: str, qty: Any | None = None, price: Any | None = None,
               ts: str | None = None) -> bool:
        o = self.orders.get(order_id)
        if o is None:
            return False  # modify for an order we don't hold — normal, ignore
        old_side, old_price, old_qty = o.side, o.price, o.qty
        if qty is not None:
            o.qty, o.qty_str = _dec(qty), str(qty)
        if price is not None:
            o.price, o.price_str = _dec(price), str(price)
        if ts is not None:
            # Kraken sends the authoritative (possibly fresh) order timestamp on every modify;
            # adopt it so time-priority ordering in the CRC stays correct when priority is lost.
            o.ts = ts
        # Keep the derived aggregate in sync with whatever changed.
        if o.price != old_price:
            self._agg_remove(old_side, old_price, old_qty)
            self._agg_add(o.side, o.price, o.qty)
        elif o.qty != old_qty:
            self._agg[old_side][old_price] = self._agg[old_side][old_price] + (o.qty - old_qty)
        return True

    def delete(self, order_id: str) -> bool:
        o = self.orders.pop(order_id, None)
        if o is None:
            return False
        self._agg_remove(o.side, o.price, o.qty)
        return True

    def clear(self) -> None:
        self.orders.clear()
        self._agg[BID].clear()
        self._agg[ASK].clear()
        self._count[BID].clear()
        self._count[ASK].clear()

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
        """Top-``n`` ``(price, summed-qty)`` best-first (bids high→low, asks low→high).

        Reads the incremental ``_agg`` cache (sorted by price), so this is O(n) for a bounded ``n``
        and O(levels) for ``n is None`` — never the old O(N-orders) scan + re-sort. A price key with
        summed qty 0 (order reduced to 0 but not deleted) is retained here, exactly as the old
        full-scan did; ``normalize.extract.l2_levels`` is what drops 0-size L2 levels.
        """
        agg = self._agg[side]
        keys = agg.keys()
        if n is not None and n <= 0:
            return []
        if side == BID:  # highest prices first
            sel = reversed(keys if n is None else keys[-n:])
        else:  # lowest prices first
            sel = iter(keys if n is None else keys[:n])
        return [(p, agg[p]) for p in sel]

    def best_bid(self) -> Decimal | None:
        agg = self._agg[BID]
        return agg.keys()[-1] if agg else None

    def best_ask(self) -> Decimal | None:
        agg = self._agg[ASK]
        return agg.keys()[0] if agg else None


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
        # recv_ts_ns of the last DATA frame seen on this processor's connection (0 until the first).
        # Set by ``run_recorder`` via the venue data-frame classifier (``health.data_classifier`` —
        # heartbeats/acks/control do NOT count); the data-staleness watchdog reads it. Exposed for
        # observability; the watchdog's own clock is :class:`DataStalenessMonitor`.
        self.last_data_ns: int = 0
        # Outbound frames the processor wants sent back over THIS connection in direct response to an
        # inbound frame (e.g. Crypto.com's ``public/respond-heartbeat``, which the server requires
        # within 5 s or it disconnects — an id-echoing reply that the fixed-cadence ``ping_message``
        # cannot express). ``run_recorder`` drains this via :meth:`take_outbound` and sends each over
        # the live socket right after ``process``. Empty for every venue that needs no such reply, so
        # existing adapters are unaffected. Pure/socket-free: the processor only QUEUES; the IO layer
        # sends (keeps the processor unit-testable with no socket).
        self.outbound: list[Any] = []

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

    def take_outbound(self) -> list[Any]:
        """Return and clear any frames the processor queued to send back (see ``self.outbound``).

        Called by ``run_recorder`` right after ``process`` so an id-echoing reply (e.g. Crypto.com's
        ``public/respond-heartbeat``) is sent over the live socket with ~ms latency. Returns an empty
        list for every venue that queues nothing.
        """
        out = self.outbound
        self.outbound = []
        return out

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
    # Max inbound WS frame size (bytes) passed straight to ``websockets.connect(max_size=...)``.
    # Default 2**20 (1 MiB) PRESERVES the websockets library default, so every existing feed's
    # effective limit is unchanged. A venue whose snapshot legitimately exceeds 1 MiB (Coinbase
    # Advanced Trade's ~40k-level L2 snapshot is one ~1.6 MiB frame) sets this to ``None``
    # (unbounded) so the complete baseline frame is received instead of silently rejected →
    # reconnect-loop. These are trusted exchange feeds, so unbounded is safe where needed.
    max_size: int | None = 2 ** 20
    # Data-staleness watchdog (base.DataStalenessMonitor): force a reconnect if the connection stays
    # UP but delivers no DATA frame for this many seconds (heartbeats/acks do NOT count). Default
    # 120 s suits every continuous crypto book/trade feed (they update many times/second — 120 s of
    # silence is unambiguously broken). ``None`` disables. Overridden per feed in run.py where a
    # feed can be legitimately quieter (Kalshi). Chosen so it CANNOT false-trip a healthy feed.
    stale_timeout_s: float | None = 120.0
    # WebSocket PROTOCOL-level keepalive — passed straight to ``websockets.connect`` and DISTINCT
    # from the application-level ``ping_message``/``ping_interval_s`` above (that sends a venue JSON
    # ping; this is the RFC6455 PING/PONG the library drives). This is the TCP-dead-peer detector:
    # with it set, a half-closed connection (server FIN → our socket stuck in CLOSE-WAIT) or a
    # silently-dropped idle TCP is detected within ~``ws_ping_interval_s + ws_ping_timeout_s`` — the
    # library sends a PING every interval and, getting no PONG, fails the connection with
    # ``ConnectionClosed``, which unblocks the read loop so the EXISTING reconnect+rediscover path
    # runs. (Incident 2026-10-22: a Kalshi connection sat in CLOSE-WAIT for ~50 min with no
    # reconnect.) The defaults EQUAL the websockets-library defaults (20/20/10), so making them
    # explicit here changes NO venue's behaviour — it only removes the silent dependency on the
    # library's implicit default (which can drift across versions or between the legacy and asyncio
    # APIs) and makes the dead-peer detection a tested, documented guarantee rather than an accident
    # of the installed library version. Safe for EVERY venue: a healthy idle peer answers the PING
    # with a PONG so the keepalive keeps the connection UP (no reconnect storm during a quiet
    # market-closed window); only a genuinely dead/half-closed peer trips it. Venues that ALSO run an
    # app-level ping (kraken/bybit/bullish) are unaffected — the two pings are orthogonal. Set
    # ``ws_ping_interval_s=None`` ONLY for a feed that must never be pinged (none today); never do
    # this for a venue that can half-close silently.
    ws_ping_interval_s: float | None = 20.0
    ws_ping_timeout_s: float | None = 20.0
    ws_close_timeout_s: float | None = 10.0


async def _force_close_ws(ws: Any, close_timeout_s: float | None) -> None:
    """Tear a (possibly half-dead) socket down so the blocked read loop raises and reconnects.

    The data-staleness watchdog calls this to force a reconnect on a "connected but no data" stall.
    A graceful ``ws.close()`` performs the WebSocket closing handshake and can STALL on a half-dead
    peer (the peer FIN'd and will never send its close frame), so we bound it with ``wait_for`` and,
    if it overruns (or raises), hard-``abort()`` the transport. Either way the connection ends and
    the outer loop reconnects — the self-heal can never itself hang on the dead peer it is healing.

    Unit-tested directly (``run_recorder`` is live-only / pragma-no-cover): a fake ws whose
    ``close()`` hangs must still get its transport aborted within the bound.
    """
    import asyncio

    bound = (close_timeout_s if close_timeout_s is not None else 10.0) + 0.5
    try:
        await asyncio.wait_for(ws.close(), timeout=bound)
    except Exception:  # noqa: BLE001 - timeout OR a close error: force the socket down regardless
        transport = getattr(ws, "transport", None)
        if transport is not None:
            try:
                transport.abort()
            except Exception:  # pragma: no cover - defensive; abort is best-effort
                pass


async def run_recorder(  # pragma: no cover - requires a live socket
    config: IoConfig,
    processors: MessageProcessor | list[MessageProcessor],
    writer: Any,
    resync_builder,
    *,
    backoff: BackoffPolicy | None = None,
    stop_event=None,
    on_connect=None,
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
                url, open_timeout=config.open_timeout_s, additional_headers=headers,
                max_size=config.max_size,
                # Protocol-level keepalive (see IoConfig): detect a dead/half-closed peer and raise
                # ConnectionClosed so the reconnect loop runs. Explicit so it never depends on the
                # library's implicit default. Equal to the library defaults, so behaviour-preserving.
                ping_interval=config.ws_ping_interval_s,
                ping_timeout=config.ws_ping_timeout_s,
                close_timeout=config.ws_close_timeout_s,
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

                # Data-staleness watchdog (self-heal for "connected but no data", incident
                # 2026-10-18). "DATA" = exactly what the health report counts: use that venue's
                # classifier so heartbeats/acks/control do NOT reset the clock. Baseline reset now.
                from .health import data_classifier  # local import: health imports base (no cycle)

                monitor = DataStalenessMonitor(
                    config.stale_timeout_s, classifier=data_classifier(config.venue))
                monitor.on_connect(now_ns())
                for p in procs:
                    p.last_data_ns = 0  # fresh connection: no data yet

                async def _watchdog() -> None:
                    if config.stale_timeout_s is None:
                        return
                    check_s = max(1.0, min(config.stale_timeout_s / 4.0, 30.0))
                    while True:
                        await asyncio.sleep(check_s)
                        if monitor.is_stale(now_ns()):
                            logger.warning(
                                "recorder %s/%s data-stale: no data frame for >= %.0fs while "
                                "connected; forcing reconnect",
                                config.venue, config.channel, config.stale_timeout_s,
                                extra={"event": "data_stale", "venue": config.venue,
                                       "channel": config.channel,
                                       "stale_timeout_s": config.stale_timeout_s},
                            )
                            # Force the socket down (bounded; hard-abort if the graceful close
                            # stalls on the half-dead peer) → unblocks the read loop → reconnect.
                            await _force_close_ws(ws, config.ws_close_timeout_s)
                            return

                # F3: create the heartbeat/watchdog and launch the per-connect background tasks
                # INSIDE the try, so the finally always cancels every task even if the on_connect
                # hook or the initial resync raises (no leaked tasks on a failed connect).
                hb = None
                wd = None
                bg_tasks: list = []
                try:
                    hb = asyncio.create_task(_heartbeat())
                    wd = asyncio.create_task(_watchdog())
                    # Optional per-connect background tasks (Kalshi dynamic-ticker poller, ROADMAP
                    # 1.3): ``on_connect(send, procs, stop_event) -> list[task]`` gets the
                    # connection-bound ``_send`` so it can push frames (e.g. ``update_subscription``)
                    # over THIS socket. Cancelled below so a reconnect starts a fresh poller.
                    if on_connect:
                        bg_tasks = list(on_connect(_send, procs, stop_event))
                    # On (re)connect, REST-snapshot venues (Coinbase/Binance/Bitstamp) MUST fetch the
                    # initial book over HTTP. In-band venues (Kraken/Bybit/Kalshi) already received a
                    # snapshot from ``config.subscribe_messages`` above, so we must NOT also fire the
                    # resubscribe resyncer here — that would open a SECOND subscription and
                    # double-apply deltas. ``resync_on_connect`` makes this correct-by-construction.
                    if getattr(resnapshot, "resync_on_connect", True):
                        for p in procs:
                            if p.resync_on_gap:
                                await resnapshot(p)
                    async for raw in ws:
                        recv = now_ns()  # stamp BEFORE parsing (CLAUDE.md convention)
                        writer.write(raw, recv_ts_ns=recv)  # write once, untouched
                        # Reset the staleness clock ONLY on a real DATA frame (not heartbeats/acks).
                        if monitor.observe(raw, recv):
                            for p in procs:
                                p.last_data_ns = recv
                        for p in procs:
                            p.process(raw, recv)
                            # Send any direct reply the processor queued (e.g. Crypto.com's
                            # ``public/respond-heartbeat``) over THIS socket, before resync handling.
                            for frame in p.take_outbound():
                                await _send(frame if isinstance(frame, str) else json.dumps(frame))
                            if p.needs_resnapshot:
                                p.clear_resnapshot()
                                await resnapshot(p)
                        if stop_event is not None and stop_event.is_set():
                            break
                finally:
                    if hb is not None:
                        hb.cancel()
                    if wd is not None:
                        wd.cancel()
                    for t in bg_tasks:
                        t.cancel()
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
