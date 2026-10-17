"""Contingency / input-handling filters (brti-spec §4, i.e. methodology §5.1–5.5).

Order of application (per the §3.8 pseudocode):
  1. per-venue staleness (§5.1), unsynced, and erroneous-book (§5.2) drops → ``prefilter_books``
  2. potentially-erroneous median-deviation filter with stateful hysteresis (§5.3) →
     ``DeviationFilter`` (carried across Effective Times)

Each dropped venue is reported with a reason; if nothing survives, the caller emits a gap (§5.5).
"""
from __future__ import annotations

from decimal import Decimal

from .book import AsOfBook
from .params import BrtiParams


# Drop-reason tokens (also what the BrtiResult records per venue).
R_NO_BOOK = "no_book"            # no snapshot at/before T
R_UNSYNCED = "unsynced"          # book from an incomplete baseline / mid-resync (schema.py)
R_STALE = "stale"                # retrieval lag > 10 s (§5.1)
R_EMPTY_SIDE = "empty_side"      # no bids or no asks (§5.2.1)
R_SELF_CROSSED = "self_crossed"  # own best bid ≥ best ask (§5.2.1)
R_DEVIATION = "deviation"        # |mid/median − 1| > PED, stateful hysteresis (§5.3)


def prefilter_books(
    books_by_venue: dict[str, AsOfBook], effective_time_ns: int, params: BrtiParams
) -> tuple[dict[str, AsOfBook], dict[str, str]]:
    """Apply §5.1 (stale) + §5.2 (erroneous) per-venue drops. Returns (kept, dropped{venue:reason}).

    * No book at/before T              → ``no_book``.
    * ``synced=False``                 → ``unsynced`` (incomplete/mid-resync book, disregard).
    * retrieval lag > threshold        → ``stale`` (§5.1). The lag proxy is T − last-snapshot
      recv_ts_ns (see note below).
    * no bids or no asks               → ``empty_side`` (§5.2.1).
    * self-crossed (best_bid≥best_ask) → ``self_crossed`` (§5.2.1).

    STALENESS PROXY (documented divergence): §5.1 defines a websocket book's RetrievalTime as
    "the most recent time as of which the Calc Agent has a valid Order Book from an unbroken
    connection" — i.e. a quiet-but-live book is NOT stale. We only have per-snapshot receipt
    times, so we proxy staleness as the lag since the last snapshot (conservative: it can drop a
    genuinely-live but quiet venue). The recorder's ``synced`` flag already captures gap/resync
    invalidity. ``stale_from_last_update=False`` disables the proxy (trust ``synced`` alone).
    """
    kept: dict[str, AsOfBook] = {}
    dropped: dict[str, str] = {}
    for venue, bk in books_by_venue.items():
        if bk.recv_ts_ns is None:
            dropped[venue] = R_NO_BOOK
            continue
        if not bk.synced:
            dropped[venue] = R_UNSYNCED
            continue
        if params.stale_from_last_update and (
            effective_time_ns - bk.recv_ts_ns > params.retrieval_lag_threshold_ns
        ):
            dropped[venue] = R_STALE
            continue
        if bk.is_empty:
            dropped[venue] = R_EMPTY_SIDE
            continue
        if bk.best_bid >= bk.best_ask:  # self-crossed (§5.2.1c)
            dropped[venue] = R_SELF_CROSSED
            continue
        kept[venue] = bk
    return kept, dropped


def median(values: list[Decimal]) -> Decimal:
    """Exact median of Decimals (even count ⇒ average of the two middle values)."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty set")
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


class DeviationFilter:
    """Stateful potentially-erroneous-data filter (§5.3) with hysteresis (§5.3.4).

    A venue is disregarded when its mid deviates from the cross-venue median mid by more than the
    PED parameter (5%); once dropped it stays dropped until its deviation falls below 50% of the
    parameter (2.5%), then it is reinstated. State (the set of currently-dropped venues) is carried
    across Effective Times — the filter must be constructed once and reused over a time series.

    Median base (``params.median_base``, O-9): the spec literally says "median of ALL venues'
    mids", so the default base is ``"all"`` (every venue surviving the §5.1/§5.2 drops, dropped-
    state or not). The alternative ``"active"`` uses only currently-non-dropped venues, so a
    diverging venue cannot drag the median toward itself. After any drop the two bases differ and
    change which venues drop/reinstate → change the index, so this is a tune-on-train / report-OOS
    lever (close empirically against published BRTI), NOT a free choice.
    """

    def __init__(self, params: BrtiParams) -> None:
        self.params = params
        self.dropped: set[str] = set()

    def apply(self, mids: dict[str, Decimal]) -> tuple[dict[str, Decimal], dict[str, str]]:
        """Filter ``mids`` (venue→mid) for one Effective Time; update state. Returns (kept, dropped).

        ``kept`` is venue→mid for venues included this tick; ``dropped`` is venue→"deviation".
        """
        if not mids:
            return {}, {}
        ped = Decimal(str(self.params.ped))
        reinstate = Decimal(str(self.params.ped_reinstate))

        if self.params.median_base == "active":
            active = {v: m for v, m in mids.items() if v not in self.dropped}
            base_mids = active if active else mids  # if everyone's dropped, fall back to all
        else:  # "all" — the literal §5.3 reading (median over all surviving venues' mids)
            base_mids = mids
        med = median(list(base_mids.values()))

        kept: dict[str, Decimal] = {}
        dropped_now: dict[str, str] = {}
        for venue, mid in mids.items():
            dev = abs(mid / med - 1) if med != 0 else Decimal(0)
            if venue in self.dropped:
                # reinstate only once back inside the tighter hysteresis band
                if dev < reinstate:
                    self.dropped.discard(venue)
                    kept[venue] = mid
                else:
                    dropped_now[venue] = R_DEVIATION
            else:
                if dev > ped:
                    self.dropped.add(venue)
                    dropped_now[venue] = R_DEVIATION
                else:
                    kept[venue] = mid
        return kept, dropped_now
