"""Recorder resync (the ``resnapshot`` seam of ``base.run_recorder``).

ROADMAP 1.1 open risk **F8**: the pure processors DETECT gaps (set ``needs_resnapshot``) but the
recovery step was stubbed, so a live run recovered nothing. This module is that recovery step —
wired into ``run_recorder`` via a ``resync_builder(send) -> resnapshot`` closure.

There are two resync modes, exactly as documented per venue in ``docs/feeds.md``:

**(1) REST-snapshot venues — Coinbase, Binance, Bitstamp.** Fetch a REST snapshot over HTTP and
rebuild the book. The stale-message discard is the venue's **post-snapshot live-path guard** on
the frames that arrive *after* the snapshot is applied — NOT a buffer replay. (In the live loop
the socket read is suspended while the HTTP fetch awaits, so no frames buffer during it; the
``_pending``/``_buffer`` replay in each ``apply_snapshot`` is only the initial-sync / offline
path.)

* **Coinbase** ``GET /products/BTC-USD/book?level=3`` → snapshot carries ``sequence``; the live
  guard drops frames with ``sequence <= snapshot sequence`` and applies the next (``+1``).
* **Binance USDⓈ-M** ``GET /fapi/v1/depth?symbol=BTCUSDT&limit=1000`` → ``lastUpdateId``; the live
  guard drops events with ``u < lastUpdateId``, gates the first event on
  ``U <= lastUpdateId <= u``, then chains on futures ``pu == prev.u``.
* **Bitstamp** ``GET /api/v2/order_book/btcusd/?group=2`` (order-level detail) reseeds the book
  AND records the snapshot's top-level ``microtimestamp`` as a FLOOR. The live guard drops any
  ``live_orders`` event with ``microtimestamp <= floor`` (already in the snapshot — the true
  analogue of Coinbase's ``seq <= S``); only the first event past the floor anchors the
  ``pre_event_id → event_id`` chain (no false gap, no double-apply). The
  ``POST /api/v2/order_data/`` ``since_id`` replay is a finer-grained backfill left as a
  follow-up (UNVERIFIED reconciliation).

**(2) In-band-resubscribe venues — Kraken, Bybit, Kalshi.** Push a resubscribe frame over the
existing socket; the venue answers with a fresh ``snapshot`` frame that the processor's normal
``process``/``apply_snapshot`` path rebuilds from:

* **Kraken v2 level3** unsubscribe + subscribe ``snapshot:true`` → a ``type:"snapshot"`` frame.
* **Bybit v5** unsubscribe + subscribe orderbook → a ``type:"snapshot"`` frame first.
* **Kalshi** resync is DISABLED (fail-safe no-op) pending ROADMAP 1.1b — see
  ``kalshi_resync_builder``: the UNVERIFIED ``sids`` protocol could double-count via a stray
  second subscription, so we log the gap and recover nothing (raw is still persisted).

Injectable seams (critical for unit-testing with NO socket): the HTTP GET (``fetch``) and the
WS send (``send``) are both injected. Tests pass a FAKE ``fetch`` returning a fixture snapshot and
a FAKE ``send`` recording the resubscribe frames. The default ``fetch`` is a thin stdlib
``urllib`` GET run in a thread executor (same shape as ``settlement.py``) — reads only, no deps
added, public snapshots need no auth. If a venue ever needed auth it would come from env vars
only, never logged (CLAUDE.md hard rule 2).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from . import binance, bitstamp, bybit, coinbase, coinbase_advanced, kalshi, kraken
from .base import MessageProcessor, logger, now_ns

JsonFetch = Callable[[str], Awaitable[Any]]
WsSend = Callable[[str], Awaitable[None]]
Resnapshot = Callable[[MessageProcessor], Awaitable[None]]
# ``persist(tagged_record_json: str, recv_ts_ns: int) -> None``: wired by ``run_recorder`` to
# ``writer.write`` so a fetched REST snapshot is written into the SAME recording (see below).
Persist = Callable[[str, int], None]


# --------------------------------------------------------------------------------------
# Persisted REST-snapshot envelope (ROADMAP 1.1c).
#
# PROBLEM: Coinbase/Binance/Bitstamp seed their book from a REST snapshot that the recorder used to
# apply only in memory. ``run_recorder`` wrote only inbound WS frames, so an offline replay (the
# normalizer) started from an empty book and could reconstruct only POST-recording deltas — those
# three BRTI constituents' books were structurally incomplete offline (``synced=False``).
#
# FIX: when a ``RestSnapshotResyncer`` fetches a snapshot (BOTH initial sync and gap resync), it
# also writes that snapshot into the recording via the SAME writer, wrapped in this tagged
# envelope and stamped with ``recv_ts_ns = time.time_ns()`` at FETCH time so it is ordered in the
# stream just before the deltas it precedes. On replay the normalizer routes ``body`` to
# ``processor.apply_snapshot`` to establish a COMPLETE baseline.
#
# TAG DESIGN: the sentinel key ``__kbtc_rest_snapshot__`` cannot collide with any real venue WS
# payload. Every crypto venue sends either a JSON object whose keys are venue field names
# (``type``/``e``/``event``/``channel``/``topic``/...) or a positional JSON array (Coinbase
# level3). None uses a dunder-wrapped sentinel key, so ``parse_rest_snapshot_record`` is
# unambiguous, and a WS-only (old-format) recording simply never contains this key — backward
# compatible. The envelope is public order-book data only; no secret is ever persisted.
# --------------------------------------------------------------------------------------
REST_SNAPSHOT_TAG = "__kbtc_rest_snapshot__"


def make_rest_snapshot_record(venue: str, channel: str, body: Any) -> str:
    """Wrap a fetched REST snapshot ``body`` in the tagged envelope and serialize to a JSON line.

    ``body`` is the raw REST snapshot JSON (as returned by ``fetch``), stored UNTOUCHED under
    ``body`` so replay feeds it straight to ``apply_snapshot``.
    """
    return json.dumps(
        {REST_SNAPSHOT_TAG: 1, "venue": venue, "channel": channel, "body": body},
        separators=(",", ":"),
        default=str,
    )


def parse_rest_snapshot_record(msg: Any) -> dict | None:
    """If ``msg`` is a persisted REST-snapshot envelope, return it (``venue``/``channel``/``body``);
    otherwise ``None``. ``msg`` is the already-parsed record (a dict for the envelope)."""
    if isinstance(msg, dict) and msg.get(REST_SNAPSHOT_TAG):
        return msg
    return None


# --------------------------------------------------------------------------------------
# Default HTTP GET — injectable; reads only; no auth (public snapshots); no new dependency.
# --------------------------------------------------------------------------------------
async def default_fetch(url: str, *, timeout: float = 20.0) -> Any:
    """Async read-only JSON GET using stdlib ``urllib`` in a thread executor.

    Kept injectable so unit tests never touch the network. No credentials are read, sent, or
    logged — these venue snapshot endpoints are public market data.
    """
    from urllib.request import Request, urlopen  # local import: keep import-time deps minimal

    def _blocking() -> Any:
        req = Request(url, method="GET", headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host, reads only
            return json.loads(resp.read().decode("utf-8"))

    return await asyncio.get_running_loop().run_in_executor(None, _blocking)


# --------------------------------------------------------------------------------------
# Resyncer implementations.
# --------------------------------------------------------------------------------------
class RestSnapshotResyncer:
    """Mode (1): fetch a REST snapshot over HTTP and rebuild via ``apply_snapshot``.

    The stale-message discard that makes this correct is the venue's **post-snapshot live-path
    guard** (Coinbase ``seq <= S``; Binance ``u < lastUpdateId`` + first-event boundary; Bitstamp
    ``microtimestamp <= snapshot microtimestamp``) applied to frames that arrive *after* the
    snapshot — because the live read loop is suspended while this HTTP fetch awaits, nothing
    buffers in ``_pending``/``_buffer`` mid-stream (that path is the initial-sync/offline one).

    ``resync_on_connect`` is True: these venues need the REST fetch to seed the initial book.
    """

    resync_on_connect = True

    def __init__(
        self, url: str, fetch: JsonFetch | None = None, *, persist: Persist | None = None
    ) -> None:
        self.url = url
        self.fetch = fetch or default_fetch
        # Injected by ``run_recorder`` (wired to ``writer.write``). When set, the fetched snapshot
        # is PERSISTED into the recording (tagged) so an offline replay can rebuild a COMPLETE
        # book. When None (initial offline construction / unit tests that only check the apply),
        # nothing is written — the in-memory apply is unchanged. The in-band resyncers below have
        # no ``persist`` attribute, so ``run_recorder`` never wires one to them (ROADMAP 1.1c).
        self.persist = persist

    async def __call__(self, processor: MessageProcessor) -> None:
        processor.begin_resync()
        snapshot = await self.fetch(self.url)
        recv_ts_ns = now_ns()  # stamp at FETCH time (orders the record before the deltas it precedes)
        # Persist BEFORE applying so the recording never reflects a book state whose seeding
        # snapshot was not written. Tagged with the processor's own venue/channel so replay routes
        # it to the right feed. Works identically for INITIAL sync and gap RESYNC (one code path).
        if self.persist is not None:
            record = make_rest_snapshot_record(processor.venue, processor.channel, snapshot)
            self.persist(record, recv_ts_ns)
        processor.apply_snapshot(snapshot, recv_ts_ns)


class ResubscribeResyncer:
    """Mode (2): push resubscribe frames over the live socket; the fresh ``snapshot`` frame that
    follows rebuilds the book through the processor's normal ``process`` path.

    ``send`` is the connection-bound async sender injected by ``run_recorder`` (a FAKE in tests).
    ``frames`` may be a static ``list[dict]`` (Kraken/Bybit) or a ``callable(processor)`` that
    builds them from live state (Kalshi needs the processor's known ``sid``s to unsubscribe the
    old subscription BEFORE resubscribing, so a resync never opens a second concurrent sub).

    ``resync_on_connect`` is False: in-band venues already get their snapshot from the initial
    ``subscribe`` sent by ``run_recorder``; firing a resubscribe at startup would double-subscribe.
    """

    resync_on_connect = False

    def __init__(
        self,
        frames: list[dict] | Callable[[MessageProcessor], list[dict]],
        send: WsSend,
    ) -> None:
        self._frames = frames
        self.send = send

    async def __call__(self, processor: MessageProcessor) -> None:
        processor.begin_resync()  # drops ``synced``, clears the book, sets ``awaiting_snapshot``
        frames = self._frames(processor) if callable(self._frames) else self._frames
        for frame in frames:
            await self.send(json.dumps(frame))


class DisabledResyncer:
    """Fail-safe no-op resyncer: log the gap once, clear the flag, send NOTHING.

    Used by Kalshi until ROADMAP 1.1b verifies the event-markets WS protocol. The hazard it
    avoids: Kalshi's ``sids`` unsubscribe is UNVERIFIED, so if the server ignored it a resubscribe
    could open a SECOND concurrent ``sid`` whose deltas would double-apply into the one per-ticker
    book after the fresh snapshot. Rather than prune sids on guessed semantics, we recover
    nothing here. No recorded data is lost — ``run_recorder`` persists every raw payload
    regardless — and the live Kalshi book is UNVERIFIED and not consumed yet, so leaving it
    un-recovered is safe and removes the double-count risk entirely.
    """

    resync_on_connect = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self._warned = False

    async def __call__(self, processor: MessageProcessor) -> None:
        if not self._warned:
            logger.warning(
                "%s venue=%s channel=%s (gap logged, book NOT recovered; raw still persisted)",
                self.reason, processor.venue, processor.channel,
                extra={"event": "resync_disabled", "venue": processor.venue,
                       "channel": processor.channel},
            )
            self._warned = True
        processor.clear_resnapshot()  # clear the flag without sending any frames


# --------------------------------------------------------------------------------------
# Per-venue REST snapshot URLs.
# --------------------------------------------------------------------------------------
def coinbase_snapshot_url(product: str = "BTC-USD") -> str:
    return coinbase.REST_SNAPSHOT.format(product=product)


def binance_snapshot_url(symbol: str = "BTCUSDT") -> str:
    return binance.REST_SNAPSHOT.format(symbol=symbol.upper())


def bitstamp_snapshot_url(pair: str = "btcusd") -> str:
    # group=2 = order-level detail (rows carry the order id as the 3rd element), which the
    # order-id book reseed in BitstampProcessor.apply_snapshot expects.
    return bitstamp.REST_ORDER_BOOK.format(pair=pair) + "?group=2"


# --------------------------------------------------------------------------------------
# Per-venue resyncer builders.
#
# Each returns ``resync_builder(send) -> resnapshot`` as expected by ``run_recorder``. REST
# venues ignore ``send``; in-band venues bind it. ``fetch`` is injectable for the REST venues.
# --------------------------------------------------------------------------------------
def coinbase_resync_builder(
    product: str = "BTC-USD", *, fetch: JsonFetch | None = None
) -> Callable[[WsSend], Resnapshot]:
    resyncer = RestSnapshotResyncer(coinbase_snapshot_url(product), fetch)
    return lambda _send: resyncer


def binance_resync_builder(
    symbol: str = "BTCUSDT", *, fetch: JsonFetch | None = None
) -> Callable[[WsSend], Resnapshot]:
    resyncer = RestSnapshotResyncer(binance_snapshot_url(symbol), fetch)
    return lambda _send: resyncer


def bitstamp_resync_builder(
    pair: str = "btcusd", *, fetch: JsonFetch | None = None
) -> Callable[[WsSend], Resnapshot]:
    resyncer = RestSnapshotResyncer(bitstamp_snapshot_url(pair), fetch)
    return lambda _send: resyncer


def kraken_resync_builder(
    product: str = "BTC/USD", depth: int = 10, *, token: str
) -> Callable[[WsSend], Resnapshot]:
    """Build the Kraken resyncer. ``token`` is REQUIRED and must be the real env-derived
    WebSocket token — a placeholder is rejected so a mis-wire fails loudly instead of causing a
    perpetual gap (feeds.md §2: level3 subscribe needs a valid token)."""
    if not token or token == "<token>":
        raise ValueError(
            "kraken resync requires a real WebSocket token (from GetWebSocketsToken via env), "
            "not a placeholder"
        )
    frames = kraken.resubscribe_messages(product, depth, token=token)
    return lambda send: ResubscribeResyncer(frames, send)


def bybit_resync_builder(
    product: str = "BTCUSDT", depth: int = 50
) -> Callable[[WsSend], Resnapshot]:
    frames = bybit.resubscribe_messages(product, depth)
    return lambda send: ResubscribeResyncer(frames, send)


def coinbase_advanced_resync_builder(
    product: str = "BTC-USD",
) -> Callable[[WsSend], Resnapshot]:
    """In-band: resubscribe to ``level2`` → a fresh ``snapshot`` rebuilds the book (no REST fetch)."""
    frames = coinbase_advanced.resubscribe_messages(product)
    return lambda send: ResubscribeResyncer(frames, send)


def kalshi_resync_builder(
    tickers: list[str] | None = None,
) -> Callable[[WsSend], Resnapshot]:
    """Kalshi resync is DISABLED (fail-safe) until ROADMAP 1.1b finalizes the adapter.

    The event-markets WS protocol is UNVERIFIED: the ``sids`` unsubscribe envelope and whether a
    resubscribe re-emits ``orderbook_snapshot`` are guesses. If the server ignored the unsubscribe
    and the resubscribe opened a second concurrent ``sid``, the new snapshot handler would add
    that sid without pruning the old one, so old-sid deltas would double-apply to the same book.
    Rather than prune sids on guessed semantics, the returned resnapshot is a safe no-op: it logs
    the gap once and clears the flag, sending NO frames. ``run_recorder`` still persists every raw
    payload, so no recorded data is lost; the live book is UNVERIFIED and not consumed yet.

    The ``unsubscribe_messages`` / ``resubscribe_messages`` / ``awaiting_snapshot`` machinery in
    ``kalshi.py`` is retained for 1.1b to wire up once the protocol + sid-pruning are verified.
    """
    disabled = DisabledResyncer(
        "Kalshi in-band resync disabled pending ROADMAP 1.1b sid-pruning"
    )
    return lambda send: disabled
