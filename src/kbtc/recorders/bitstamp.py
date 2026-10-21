"""Bitstamp `live_orders_{pair}` recorder (BRTI constituent).

feeds.md §3 (endpoint, subscribe envelope, and gap mechanism VERIFIED from the primary Bitstamp
WebSocket v2 doc, 2026-10-16). Order-by-order book keyed by order id. Events: ``order_created`` /
``order_changed`` / ``order_deleted``, each a frame with top-level ``event``, ``channel``,
``event_id`` (UUID string), ``pre_event_id`` (UUID string), and ``data`` {id, id_str,
order_type (0 = buy, 1 = sell), datetime, microtimestamp, amount, amount_str, amount_at_create,
amount_traded, price, price_str, order_source}.

**Gap detection (VERIFIED): the `pre_event_id` → `event_id` chain.** Each new event's
``pre_event_id`` must equal the previous event's ``event_id`` (exactly analogous to Binance
futures ``pu == prev.u``); a mismatch means a dropped event → resync. ``microtimestamp`` is kept
as a secondary monotonic-ordering sanity check (a regression still logs a gap), but the chain is
the primary signal. Resync path: REST order-book seed + ``POST /api/v2/order_data/`` ``since_id``
(MarketEventID) backfill.

NOTE: the ``microtimestamp``-sequenced "discard buffered events older than the snapshot" behaviour
belongs to the SEPARATE ``detail/diff_order_book`` channel — NOT ``live_orders`` — do not conflate.
``live_orders`` has no snapshot message; seed the book from the REST order book.

RESEED CORRECTNESS: on reseed we record the REST snapshot's top-level ``microtimestamp`` as a
discard FLOOR and drop every subsequent ``live_orders`` event with ``microtimestamp <= floor``
(already contained in the snapshot) before applying/anchoring. Without this floor the events that
arrive right after a reseed — which all pre-date the snapshot because run_recorder suspends the
read loop during the HTTP fetch — would double-apply and corrupt the book. This is the true
analogue of Coinbase's ``sequence <= S`` live-path guard.

RESEED-RESURRECTION GUARD (REV-026 / study-2.2 crossed-book root cause): the forward-only floor
above is necessary but NOT sufficient. Bitstamp's REST ``/order_book/`` endpoint **lags the live
WS stream** (UNVERIFIED venue cause — inferred from the study-2.2 trace, not from a Bitstamp
primary doc; the fix is robust regardless of the exact mechanism), so the snapshot's microtimestamp
``S`` can be OLDER than events we already applied *before* the reseed landed. A concrete traced
case: an order was ``order_created`` then ``order_deleted`` on the live stream (both at micro
> ``S``, both processed before the resync's
snapshot frame), yet the stale REST snapshot — taken at ``S`` < the delete's micro — still listed
the order as resting. Blindly ``clear()``-ing and re-adding every snapshot order therefore
RESURRECTS an order the stream already deleted; the forward-only floor can't help (the delete is in
the past and never replays — the documented ``order_data`` ``since_id`` backfill is not recorded),
so the phantom rests forever and self-crosses the book. Fix: we track recently-deleted order ids
with their delete microtimestamp (bounded window — see ``RESEED_DELETE_WINDOW_US``) and, on a
reseed with snapshot micro ``S``, DO NOT re-add any snapshot order whose recorded delete micro is
``>= S`` (a stale resurrection). The ``>=`` (not strictly ``>``) MATCHES the forward-only floor
convention — :meth:`process` treats an event with ``micro <= S`` as already-in-the-snapshot — so a
delete at exactly ``micro == S`` that a stale REST book still lists (the "stale-tie" case) is also
suppressed; re-adding it would cross the book (REV-026 F2). This is reconstruction-only: the raw
recording already contains the create+delete+snapshot, so an offline re-normalize recovers a
correct, uncrossed book. The 6 h replay counterfactual took crossing from 43.0% -> ~2.1% with only
~76 suppressed re-adds across 4 reseeds (the resurrected phantoms sit exactly at the touch, so a
handful dominate top-of-book).

CORRECTNESS-CRITICAL ASSUMPTION (REV-026 F1): the guard keys purely on order id, so it is
load-bearing that **Bitstamp never reuses an order id within the guard window**. If an id were
reused, a stale ``_recent_deletes[id]`` could drop a genuinely-resting snapshot order (book shows
LESS than reality, does not cross, no alarm). See ``docs/facts.md`` ("Bitstamp order-id reuse")
for the verification status of this assumption.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .base import ASK, BID, IoConfig, MessageProcessor, OrderIdBook, as_dict

ENDPOINT = "wss://ws.bitstamp.net"  # VERIFIED 2026-10-16 (Bitstamp WS v2 doc)
SUBSCRIBE_EVENT = "bts:subscribe"  # VERIFIED 2026-10-16
UNSUBSCRIBE_EVENT = "bts:unsubscribe"  # VERIFIED 2026-10-16
MAX_CHANNELS_PER_CONNECTION = 1024  # VERIFIED 2026-10-16

REST_ORDER_BOOK = "https://www.bitstamp.net/api/v2/order_book/{pair}/"  # seed (group=2 detail)
REST_ORDER_DATA = "https://www.bitstamp.net/api/v2/order_data/"  # since_id backfill (VERIFIED)

_EVENTS = {"order_created", "order_changed", "order_deleted"}

# Reseed-resurrection guard (see module docstring). Window of recently-deleted order ids (by delete
# microtimestamp) retained so a reseed can reject orders the stream already deleted at a micro newer
# than the (lagging) REST snapshot's micro ``S``.
#
# WINDOW JUSTIFICATION: a resurrection can only happen for a delete whose micro is > ``S`` yet was
# applied before the reseed — i.e. a delete lying in the REST-vs-WS lag of ``S``. That lag was ~0.15 s
# in the traced incident and is, per the venue, sub-second to a few seconds (UNVERIFIED cause — see
# docstring). 120 s is a deliberately generous multiple so a slow REST endpoint can never age a still-
# relevant delete out of the map before the reseed lands; correctness never depends on the window being
# tight. MEMORY: deletes run ~60/s on this feed (≈215k/h), so a 120 s window holds ~7k ids — small; even
# a sustained ~1k/s burst is clamped by the hard ``MAX_RECENT_DELETES`` cap below. We prune by micro-age
# on every delete AND cap hard (defensive against microtimestamp regressions that could defeat age-
# pruning), so the structure is bounded over a 24 h run and cannot leak.
RESEED_DELETE_WINDOW_US = 120_000_000  # 120 s, in microseconds (Bitstamp micro units)
MAX_RECENT_DELETES = 50_000  # hard ceiling (~minutes of deletes even at burst rate); ~a few MB


class BitstampProcessor(MessageProcessor):
    venue = "bitstamp"

    def __init__(self, product: str = "btcusd", channel: str | None = None, **kw: Any) -> None:
        channel = channel or f"live_orders_{product}"
        super().__init__(product=product, channel=channel, **kw)
        self.book = OrderIdBook()
        self.last_event_id: str | None = None  # primary chain anchor (UUID string)
        self.last_microtimestamp: int | None = None  # secondary ordering sanity check
        # Post-reseed stale-event FLOOR (microtimestamp). Set by apply_snapshot; events at or
        # below it are already in the snapshot and MUST be dropped before applying/anchoring.
        self.snapshot_floor_micro: int | None = None
        # Reseed-resurrection guard: recently-deleted order id -> delete microtimestamp, kept in
        # (insertion ≈ micro) order and bounded (see RESEED_DELETE_WINDOW_US / MAX_RECENT_DELETES).
        self._recent_deletes: "OrderedDict[str, int]" = OrderedDict()
        # Observability: how many stale snapshot orders we refused to resurrect across reseeds.
        self.resurrections_suppressed = 0
        # --- Guard-disable observability (REV-026 F3): count the two paths that SILENTLY skip the
        # resurrection guard so the residual crossing can be attributed instead of being invisible.
        # (a) order_deleted with no usable microtimestamp -> delete unrecorded -> can't be
        #     suppressed on a later reseed. (b) reseed snapshot with no usable floor S -> guard
        #     skipped entirely, every snapshot row re-added. Both also emit a WARNING when they fire.
        self.deletes_missing_micro = 0
        self.snapshots_without_floor = 0

    def apply_snapshot(self, snapshot: Any, recv_ts_ns: int | None = None) -> None:
        """Seed from the REST detail order book:
        ``{"microtimestamp": "...", "timestamp": "...", "bids":[[price,amount,order_id],...], "asks":...}``.

        ``live_orders`` has no snapshot message and no sequence number, so the reseed uses the
        REST order book's top-level ``microtimestamp`` as a discard FLOOR: because run_recorder
        suspends the read loop during the fetch, the events that arrive next all pre-date the
        snapshot's effective time and are ALREADY in the reseeded book. Replaying them would
        double-apply (e.g. a stale ``order_created`` whose ``order_deleted`` fell in the gap would
        resurrect as a phantom order). The live guard in :meth:`process` drops every event with
        ``microtimestamp <= floor`` before touching the book or anchoring the chain.
        """
        snap = as_dict(snapshot)
        # Snapshot's effective exchange time S. Computed BEFORE rebuilding because the reseed-
        # resurrection guard needs it to reject snapshot orders the stream already deleted at a micro
        # AT OR NEWER than this (lagging) REST snapshot.
        # FALLBACK BIAS NOTE: ``timestamp`` is whole SECONDS, so ``ts * 1_000_000`` yields the micro
        # at the START of that second, i.e. an UNDER-estimate of the true snapshot micro. A smaller S
        # makes MORE recorded deletes satisfy ``delete_micro >= S``, so this fallback biases toward
        # OVER-suppression (dropping a row that might be legitimately resting) — the safe direction
        # for crossing, never a resurrection. Behavior left as-is; documented so the asymmetry is known.
        floor = _int_or_none(snap.get("microtimestamp"))
        if floor is None:
            ts = _int_or_none(snap.get("timestamp"))
            floor = ts * 1_000_000 if ts is not None else None
        if floor is None:
            # Guard-disable path (b): with no usable S the resurrection guard is skipped for the
            # WHOLE reseed (every snapshot row re-added), so any stale row can cross. Count + warn
            # (REV-026 F3). The real REST detail book always carries a microtimestamp; this firing
            # signals an empty/synthetic seed (e.g. the test's bare {"bids":[],"asks":[]}) or a
            # malformed REST response — residual crossing after such a reseed is attributable here.
            self.snapshots_without_floor += 1
            if snap.get("bids") or snap.get("asks"):  # don't warn on a deliberate empty seed
                self.log.warning(
                    "bitstamp %s: reseed snapshot with no microtimestamp/timestamp floor -> "
                    "resurrection guard skipped for this reseed (count=%d)",
                    self.channel, self.snapshots_without_floor)
        self.book.clear()
        for row in snap.get("bids", []):
            self._seed_order(str(row[2]), BID, row[0], row[1], floor)
        for row in snap.get("asks", []):
            self._seed_order(str(row[2]), ASK, row[0], row[1], floor)
        self.synced = True
        self.needs_resnapshot = False
        self.snapshot_floor_micro = floor
        self.last_microtimestamp = floor  # secondary check also respects the floor
        # A fresh snapshot breaks the old chain; re-anchor on the first live event past the floor.
        self.last_event_id = None

    def _seed_order(self, oid: str, side: str, price: Any, qty: Any,
                    snap_micro: int | None) -> None:
        """Re-add a snapshot order UNLESS it is a stale resurrection (the stream already deleted it
        at a micro AT OR newer than the snapshot's ``S``; the lagging REST book listed it as still
        resting). Suppressing it is what keeps a reseeded book consistent (and uncrossed).

        The condition is ``delete_micro >= S`` (not strictly ``>``) to MATCH the forward-only
        floor convention in :meth:`process`, which treats an event with ``micro <= S`` as
        already-contained-in-the-snapshot. A delete at exactly ``micro == S`` that a stale REST
        book still lists is the "stale-tie" case: the floor would also have dropped a live replay
        of that delete, so re-adding the row would CROSS the book (REV-026 F2, confirmed
        empirically). Using ``>=`` cannot over-suppress a legitimately-resting order: it only
        fires for an id that is BOTH in the snapshot rows AND has a recorded delete at ``>= S``."""
        if snap_micro is not None:
            dmicro = self._recent_deletes.get(oid)
            if dmicro is not None and dmicro >= snap_micro:
                self.resurrections_suppressed += 1
                return
        self.book.add(oid, side, price, qty)

    def _note_delete(self, oid: str, micro: int | None) -> None:
        """Record an order_deleted's microtimestamp for the reseed-resurrection guard, pruning by
        age window and a hard cap so the structure stays bounded over a 24 h run."""
        if micro is None:
            # Guard-disable path (a): without a delete micro we cannot judge staleness on a reseed,
            # so this id can be resurrected and cross the book. Make it visible/countable (REV-026
            # F3) rather than silently dropping it. Bitstamp always sends a microtimestamp, so this
            # firing at all signals a malformed/partial frame worth attributing residual crossing to.
            self.deletes_missing_micro += 1
            self.log.warning(
                "bitstamp %s: order_deleted id=%s with no microtimestamp -> resurrection guard "
                "disabled for this id (count=%d)", self.channel, oid, self.deletes_missing_micro)
            return
        rd = self._recent_deletes
        rd[oid] = micro
        rd.move_to_end(oid)  # newest at the end; front stays oldest for age-pruning
        cutoff = micro - RESEED_DELETE_WINDOW_US
        while rd:
            oldest = next(iter(rd))
            if rd[oldest] < cutoff:
                rd.popitem(last=False)
            else:
                break
        while len(rd) > MAX_RECENT_DELETES:  # defensive cap (micro regressions can defeat age-prune)
            rd.popitem(last=False)

    def process(self, raw: Any, recv_ts_ns: int) -> list[dict]:
        msg = as_dict(raw)
        if not isinstance(msg, dict):
            return []
        event = msg.get("event")
        if event == "bts:request_reconnect":
            # Bitstamp control event: it wants us to reconnect (and reseed).
            self._emit_gap("request_reconnect", recv_ts_ns)
            return []
        if event not in _EVENTS:
            return []  # subscription-succeeded / heartbeat frames
        data = msg.get("data") or {}
        event_id = msg.get("event_id")
        pre_event_id = msg.get("pre_event_id")
        micro = _int_or_none(data.get("microtimestamp"))

        # --- POST-RESEED STALE-EVENT GUARD (the analogue of Coinbase seq<=S): events already in
        # the reseed snapshot carry microtimestamp <= floor. Drop them BEFORE applying or
        # anchoring, with no gap — they are expected, not missing. The floor is consumed once a
        # live event passes it (subsequent events are strictly newer). ---
        if self.snapshot_floor_micro is not None and micro is not None:
            if micro <= self.snapshot_floor_micro:
                return []
            self.snapshot_floor_micro = None  # first event past the floor: resume normal checks

        # --- PRIMARY gap signal: pre_event_id must chain off the previous event_id ---
        chain_broke = False
        if self.last_event_id is not None:
            if pre_event_id is None:
                # Once anchored, a missing pre_event_id can't be verified -> treat as a gap
                # rather than silently re-anchoring (the doc says it's always present).
                self._emit_gap("missing pre_event_id", recv_ts_ns,
                               expected=self.last_event_id, got=None)
                chain_broke = True
            elif pre_event_id != self.last_event_id:
                self._emit_gap("event_id chain break", recv_ts_ns,
                               expected=self.last_event_id, got=pre_event_id)
                chain_broke = True
        if event_id is not None:
            self.last_event_id = event_id  # re-anchor (seeds on first event)

        # --- SECONDARY: microtimestamp monotonic sanity check. Only run when the chain check
        # didn't already flag this event, so one dropped event logs at most one gap. ---
        micro = _int_or_none(data.get("microtimestamp"))
        if (not chain_broke and micro is not None and self.last_microtimestamp is not None
                and micro < self.last_microtimestamp):
            self._emit_gap("microtimestamp regression", recv_ts_ns,
                           expected=f">{self.last_microtimestamp}", got=micro)
        if micro is not None:
            self.last_microtimestamp = micro

        self._apply(event, data)
        return [{"kind": "book", "event": event}]

    def _apply(self, event: str, data: dict) -> None:
        oid = str(data["id"])
        side = BID if int(data["order_type"]) == 0 else ASK
        if event == "order_created":
            self.book.add(oid, side, data["price"], data["amount"])
        elif event == "order_changed":
            self.book.modify(oid, qty=data.get("amount"), price=data.get("price"))
        elif event == "order_deleted":
            self.book.delete(oid)
            # Remember this delete (bounded) so a later stale REST reseed can't resurrect the order.
            self._note_delete(oid, _int_or_none(data.get("microtimestamp")))


def _int_or_none(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def subscribe_messages(product: str = "btcusd") -> list[dict]:
    return [{"event": SUBSCRIBE_EVENT, "data": {"channel": f"live_orders_{product}"}}]


def io_config(product: str = "btcusd") -> IoConfig:
    return IoConfig(
        venue="bitstamp",
        channel=f"live_orders_{product}",
        urls=[ENDPOINT],
        subscribe_messages=subscribe_messages(product),
        ping_interval_s=None,  # heartbeat/limits beyond 1024 channels/conn not needed here
    )
