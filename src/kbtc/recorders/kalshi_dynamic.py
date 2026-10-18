"""Dynamic ticker management for continuous 24/7 KXBTC15M capture (ROADMAP 1.3).

KXBTC15M rolls a NEW 15-minute market every quarter hour, so a STATIC ticker list only ever
covers one window. This module keeps ONE stable authenticated WS session subscribed to exactly
the currently-open markets: on connect it discovers the open markets and subscribes; a background
poller re-discovers on an interval, diffs against the subscribed set, and sends
``update_subscription`` ``add_markets`` for newly-open markets and ``delete_markets`` for closed
ones — WITHOUT reconnecting (same ``sid`` per channel). The ``KalshiProcessor`` already keys its
book per ``market_ticker``, so multiple markets on one sid are supported.

HARD RULES (CLAUDE.md):
* **Reads only / NO order path.** Discovery is Kalshi's PUBLIC market-data REST (no auth); the WS
  stays the authed READ session. No order endpoint is referenced here.
* **Credentials from env only, never logged.** This module reads none directly; the WS auth lives
  in ``run.make_kalshi_header_factory``. The REST discovery is public.

VERIFIED (docs.kalshi.com/websockets/websocket-connection, 2026-10-17):
* ``update_subscription`` adds/removes markets on a live subscription without reconnecting, keeping
  the SAME sid; ``sids`` is a one-element array; ``action`` ∈ add_markets/delete_markets/
  get_snapshot; adding a market emits an ``orderbook_snapshot`` for it on that sid.
* Per-subscription market-limit → error code 26, command rate-limit → error code 27 (limit VALUES
  undocumented). The live poller handles an error frame gracefully (logs, retries next poll).
* Max 5 concurrent WS connections per user — one dynamic session stays well under it.

Open-market discovery (PUBLIC REST, no auth):
``GET api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open`` →
``markets[].ticker``. Reuses ``settlement.enumerate_markets`` (cursor pagination) + its fetcher.

TESTABILITY: every piece of decision logic is pure and socket-free — ``diff_tickers`` (open-set →
add/remove), :class:`DynamicSubscriptionManager` (sid capture, message building, applied-state), and
``discover_open_tickers`` (driven by an injected fetcher). Only :func:`run_poller` touches the live
socket and is ``# pragma: no cover`` (exercised by the 1.3 VPS run + the live box validation).
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from . import kalshi
from .base import logger
from .settlement import (
    PROD_BASE,
    SERIES,
    WINDOW_S,
    Fetcher,
    enumerate_markets,
    _default_fetch,
)

# The per-market channels tracked dynamically. orderbook_delta + trade are VERIFIED on the wire for
# KXBTC15M; configurable (``KBTC_KALSHI_CHANNELS``) so a verified lifecycle channel can be added.
DYNAMIC_CHANNELS: tuple[str, ...] = ("orderbook_delta", "trade")

DEFAULT_POLL_INTERVAL_S = 45.0  # re-discover cadence (markets roll every 900 s)
# PRE-SUBSCRIBE lookahead: how many UPCOMING (not-yet-open) windows to subscribe ahead of their
# open. 1 covers the next boundary with zero gap at the roll; hard-capped at 2 to bound the live
# subscription (runaway guard). KXBTC15M's strike is the OPEN-WINDOW 60 s BRTI average, so missing a
# new market's first seconds (the old poll-interval hole) dropped the most important book window —
# pre-subscribing eliminates that hole.
DEFAULT_MAX_UPCOMING = 1
_MAX_UPCOMING_CAP = 2
# Keep a market subscribed for a grace period AFTER it closes so its final frames (late trades /
# book changes around settlement) are still captured — don't drop it the instant it leaves the open
# set.
DEFAULT_REMOVE_GRACE_S = 120.0
_ID_START = 1000  # update_subscription frame ids start here (distinct from the initial subscribe id=1)


# --------------------------------------------------------------------------------------
# Open-market discovery (PUBLIC REST, no auth) — reuses settlement.py's paginating fetcher.
# --------------------------------------------------------------------------------------
def discover_open_tickers(
    fetch: Fetcher = _default_fetch, series: str = SERIES, base: str = PROD_BASE
) -> list[str]:
    """Return the currently-open market tickers for ``series`` (reads only, public).

    ``GET /markets?series_ticker=<series>&status=open`` fully paginated via ``cursor``. ``fetch``
    is injectable (a FAKE in tests) so this runs with no network.
    """
    params = {"series_ticker": series, "status": "open", "limit": 1000}
    out: list[str] = []
    for m in enumerate_markets(params, fetch, base):
        ticker = m.get("ticker")
        if ticker:
            out.append(ticker)
    return out


def discover_upcoming_tickers(
    fetch: Fetcher = _default_fetch,
    series: str = SERIES,
    base: str = PROD_BASE,
    *,
    now_s: float,
    max_upcoming: int = DEFAULT_MAX_UPCOMING,
) -> list[str]:
    """Return the next ``max_upcoming`` UPCOMING (``status=="initialized"``) windows, earliest first.

    The next market exists as ``initialized`` before it opens (VERIFIED by the live probe: 1 active
    + N initialized). We query a bounded close-time window (``[now, now + (max_upcoming+1)·900 s]``)
    so the fetch itself can't enumerate far-future windows, then client-side keep ``initialized``
    rows (the ``status`` VALUE is verified; we don't rely on ``status=initialized`` being a valid
    server *filter*), sort by ``close_time``, and cap at ``max_upcoming`` (hard-capped at 2). Reads
    only, public. ``fetch``/``now_s`` are injectable so this runs with no network.
    """
    max_upcoming = max(0, min(int(max_upcoming), _MAX_UPCOMING_CAP))
    if max_upcoming == 0:
        return []
    horizon_s = WINDOW_S * (max_upcoming + 1)
    params = {
        "series_ticker": series,
        "min_close_ts": int(now_s),
        "max_close_ts": int(now_s + horizon_s),
        "limit": 1000,
    }
    rows = [
        m for m in enumerate_markets(params, fetch, base)
        if m.get("status") == "initialized" and m.get("ticker")
    ]
    rows.sort(key=lambda m: m.get("close_time") or "")
    return [m["ticker"] for m in rows[:max_upcoming]]


def discover_target_tickers(
    fetch: Fetcher = _default_fetch,
    series: str = SERIES,
    base: str = PROD_BASE,
    *,
    now_s: float,
    max_upcoming: int = DEFAULT_MAX_UPCOMING,
) -> list[str]:
    """Desired subscription set = currently-OPEN markets PLUS the imminent UPCOMING window(s).

    Open markets first (order preserved), then any upcoming ticker not already open. Subscribing the
    upcoming market BEFORE it opens means its data flows with ZERO gap at the :00/:15/:30/:45 roll.
    """
    open_tickers = discover_open_tickers(fetch, series, base)
    upcoming = discover_upcoming_tickers(
        fetch, series, base, now_s=now_s, max_upcoming=max_upcoming
    )
    seen = set(open_tickers)
    out = list(open_tickers)
    for t in upcoming:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


# --------------------------------------------------------------------------------------
# Pure diff: current subscribed set vs freshly-discovered open set.
# --------------------------------------------------------------------------------------
def diff_tickers(
    subscribed: set[str], open_tickers: set[str]
) -> tuple[list[str], list[str]]:
    """``(to_add, to_remove)`` — open-but-not-subscribed, and subscribed-but-no-longer-open.

    Both lists are sorted for deterministic frames/tests. Pure; no IO.
    """
    to_add = sorted(open_tickers - subscribed)
    to_remove = sorted(subscribed - open_tickers)
    return to_add, to_remove


# --------------------------------------------------------------------------------------
# Subscription manager — pure state + message building (NO socket, NO network).
# --------------------------------------------------------------------------------------
class DynamicSubscriptionManager:
    """Tracks the subscribed ticker set + per-channel ``sid`` and builds ``update_subscription``.

    Lifecycle across one connection:
    1. :meth:`reset_for_connect` — called by the per-connect ``subscribe_factory`` with the open
       tickers used for the INITIAL subscribe; seeds ``subscribed`` and clears stale sids (a
       reconnect gets a brand-new subscription, so sids are re-captured from the fresh acks).
    2. The ``KalshiProcessor`` captures channel→sid from the ``subscribed`` acks; the poller copies
       that into :meth:`capture_sids`.
    3. Each poll: :meth:`plan_update` diffs the open set, :meth:`build_update_messages` builds the
       frames, and — only after they are sent — :meth:`apply_update` moves the applied state.

    All methods are pure/deterministic; the live send loop is in :func:`run_poller`.
    """

    def __init__(
        self,
        channels: tuple[str, ...] = DYNAMIC_CHANNELS,
        id_start: int = _ID_START,
        remove_grace_s: float = DEFAULT_REMOVE_GRACE_S,
    ) -> None:
        self.channels = tuple(channels)
        self.subscribed: set[str] = set()
        self.sid_by_channel: dict[str, int] = {}
        self.remove_grace_s = remove_grace_s
        # ticker -> poll time it was FIRST seen no-longer-desired (closed). Removal waits out
        # ``remove_grace_s`` from here so a just-closed market's final frames are still captured.
        self._absent_since: dict[str, float] = {}
        self._next_id = id_start

    def reset_for_connect(self, initial_tickers: list[str]) -> None:
        """Seed the applied state for a fresh connection (initial subscribe tickers; sids cleared)."""
        self.subscribed = set(initial_tickers)
        self.sid_by_channel = {}
        self._absent_since = {}

    def capture_sids(self, sid_by_channel: dict[str, int]) -> None:
        """Copy channel→sid captured by the processor from the ``subscribed`` acks."""
        self.sid_by_channel.update(sid_by_channel)

    def ready(self) -> bool:
        """True once every managed channel has an acked sid (so updates can target them)."""
        return all(ch in self.sid_by_channel for ch in self.channels)

    def _alloc_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def plan_update(
        self, desired: list[str] | set[str], now_s: float
    ) -> tuple[list[str], list[str]]:
        """``(to_add, to_remove)`` for the DESIRED set (open + imminent-upcoming) at time ``now_s``.

        * ``to_add`` — desired markets not yet subscribed (pre-subscribes upcoming windows; already
          idempotent, since an upcoming market stays subscribed once added and so is not re-added).
        * ``to_remove`` — subscribed markets that have been NOT desired (closed) for at least
          ``remove_grace_s``. A just-closed market is kept until the grace elapses so its final
          frames survive; a market that re-enters ``desired`` before the grace clears its timer.

        Updates the internal removal timers; idempotent for repeated calls at the same ``now_s``.
        """
        desired_set = set(desired)
        to_add = sorted(desired_set - self.subscribed)
        absent = self.subscribed - desired_set
        # Clear timers for anything no longer absent (came back, or already removed).
        for t in list(self._absent_since):
            if t not in absent:
                del self._absent_since[t]
        # Start/keep a timer for each currently-absent subscribed market.
        for t in absent:
            self._absent_since.setdefault(t, now_s)
        to_remove = sorted(
            t for t in absent if now_s - self._absent_since[t] >= self.remove_grace_s
        )
        return to_add, to_remove

    def build_update_messages(
        self, to_add: list[str], to_remove: list[str]
    ) -> list[dict]:
        """One ``update_subscription`` frame per (channel, non-empty action).

        Channel-major, add before delete, so frames are deterministic. Each frame targets that
        channel's acked sid. Returns ``[]`` if there is nothing to do.
        """
        msgs: list[dict] = []
        for ch in self.channels:
            sid = self.sid_by_channel.get(ch)
            if sid is None:
                continue  # sid not acked yet; this channel is skipped (caller gates on ready())
            if to_add:
                msgs.append(
                    kalshi.update_subscription_message(sid, to_add, "add_markets", self._alloc_id())
                )
            if to_remove:
                msgs.append(
                    kalshi.update_subscription_message(
                        sid, to_remove, "delete_markets", self._alloc_id()
                    )
                )
        return msgs

    def apply_update(self, to_add: list[str], to_remove: list[str]) -> None:
        """Move the applied state AFTER the frames are sent."""
        self.subscribed |= set(to_add)
        self.subscribed -= set(to_remove)
        for t in to_remove:
            self._absent_since.pop(t, None)


def initial_subscribe_messages(
    tickers: list[str], channels: tuple[str, ...] = DYNAMIC_CHANNELS
) -> list[dict]:
    """Initial per-connect subscribe for the discovered open markets (reads only)."""
    return kalshi.subscribe_messages(tickers, channels=channels)


# --------------------------------------------------------------------------------------
# Live background poller — needs the connection-bound send; NOT unit-tested (live socket).
# --------------------------------------------------------------------------------------
async def run_poller(  # pragma: no cover - requires a live socket + REST
    send: Callable[[str], Any],
    processor: kalshi.KalshiProcessor,
    manager: DynamicSubscriptionManager,
    *,
    fetch: Fetcher = _default_fetch,
    series: str = SERIES,
    base: str = PROD_BASE,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
    max_upcoming: int = DEFAULT_MAX_UPCOMING,
    clock: Callable[[], float] = time.time,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Re-discover the TARGET set (open + imminent-upcoming) on ``interval_s`` and push
    ``update_subscription`` over ``send``.

    Runs as a background task started by ``run.py``'s ``on_connect`` hook on each connection, and is
    cancelled on disconnect/shutdown. The REST discovery runs in a thread executor so the blocking
    ``urllib`` GET never stalls the asyncio read loop. Pre-subscribing the upcoming window means the
    new market is live BEFORE its open, so there is no gap at the roll. Any error (network, discovery
    failure) is logged and retried next tick — never fatal, never silent.
    """
    loop = asyncio.get_running_loop()
    while stop_event is None or not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)  # type: ignore[union-attr]
            break  # stop_event set during the wait → shut down
        except (asyncio.TimeoutError, AttributeError):
            pass  # interval elapsed (or no stop_event) → poll

        # F4: the ENTIRE tick is wrapped so a transient bug or a malformed discovery response
        # logs and the poller survives to the next tick — it must never silently die for the rest
        # of the connection (that would freeze the subscription set across every later roll).
        try:
            # Pick up any sids acked since the last tick.
            manager.capture_sids(processor.sid_by_channel)
            if not manager.ready():
                logger.info(
                    "kalshi dynamic: awaiting subscribed acks for %s (have %s)",
                    manager.channels, sorted(manager.sid_by_channel),
                    extra={"event": "kalshi_dynamic_wait"},
                )
                continue
            now_s = clock()
            try:
                target = await loop.run_in_executor(
                    None,
                    lambda: discover_target_tickers(
                        fetch, series, base, now_s=now_s, max_upcoming=max_upcoming
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - discovery is best-effort; retry next tick
                logger.warning(
                    "kalshi dynamic: discovery failed (%s); retry next tick", exc,
                    extra={"event": "kalshi_dynamic_discovery_error"},
                )
                continue
            to_add, to_remove = manager.plan_update(target, now_s)
            if not to_add and not to_remove:
                continue
            msgs = manager.build_update_messages(to_add, to_remove)
            try:
                for m in msgs:
                    await send(json.dumps(m))
            except Exception as exc:  # noqa: BLE001 - socket error → let the reconnect loop handle it
                logger.warning(
                    "kalshi dynamic: send failed (%s); leaving applied-state unchanged", exc,
                    extra={"event": "kalshi_dynamic_send_error"},
                )
                continue
            # KNOWN LIMITATION (documented fast-follow, NOT implemented — improbable at KXBTC15M's
            # ~1-2 open markets, and now observable via F1's error-frame WARNING): applied-state is
            # advanced on a successful SEND, not on the server ``ok`` ack, and is NOT rolled back on
            # an error-26/27 (market-limit / rate-limit) reject. A partial send (F5) that delivers
            # the add frame for one channel but fails before the other leaves the channels'
            # subscribed sets briefly divergent (self-corrects: the failed send hits the except and
            # the next tick re-diffs). A deeper fix = reconcile against the ``ok``/``error`` frames
            # and roll back / retry.
            manager.apply_update(to_add, to_remove)
            logger.info(
                "kalshi dynamic: +%d -%d markets (now %d subscribed)",
                len(to_add), len(to_remove), len(manager.subscribed),
                extra={"event": "kalshi_dynamic_update", "added": to_add, "removed": to_remove},
            )
        except Exception as exc:  # noqa: BLE001 - never let one bad tick kill the poller
            logger.warning(
                "kalshi dynamic: poll tick failed (%s); continuing", exc,
                extra={"event": "kalshi_dynamic_tick_error"},
            )
