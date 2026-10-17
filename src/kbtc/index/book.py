"""Order-book data structures and as-of reconstruction (ROADMAP 2.2 Part A step 1).

A ``Level`` is one aggregated price level: an EXACT ``Decimal`` price and a float BTC size (the
volume axis is float per the precision policy in ``params.py``). An ``AsOfBook`` is one
constituent's book reconstructed as of an Effective Time ``T``.

As-of reconstruction (brti-spec implies, normalize/schema.py pins down the mechanics): given a
constituent's event-driven L2 snapshots (rows carry ``recv_ts_ns`` + strictly-monotonic
``snap_id``, all rows of one book state sharing one ``snap_id``), the book as of ``T`` is the
snapshot with the greatest ``(recv_ts_ns, snap_id)`` whose ``recv_ts_ns <= T``. The tie-break on
``snap_id`` is load-bearing: two snapshots can share a ``recv_ts_ns`` (schema.py), so ordering by
time alone is wrong.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from decimal import Decimal

BID = "bid"
ASK = "ask"


@dataclass(frozen=True)
class Level:
    """One aggregated price level. ``price`` is exact Decimal; ``size`` is BTC volume (float)."""

    price: Decimal
    size: float


@dataclass(frozen=True)
class AsOfBook:
    """A constituent's book as of an Effective Time. ``bids`` desc by price, ``asks`` asc.

    ``recv_ts_ns`` is the receipt time of the reconstructing snapshot (None if the venue had no
    snapshot at/before T). ``synced`` is that snapshot's completeness flag (schema.py): False
    means the book is from an incomplete baseline or mid-resync and must be disregarded (§5).
    """

    venue: str
    recv_ts_ns: int | None
    synced: bool
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    @property
    def is_empty(self) -> bool:
        return not self.bids or not self.asks

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        """(best_bid + best_ask)/2 as exact Decimal, or None if a side is empty."""
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2


def _levels_from_rows(rows: list[dict]) -> tuple[tuple[Level, ...], tuple[Level, ...]]:
    """Split one snapshot's rows into (bids desc, asks asc), dropping non-positive entries (§5.2.2).

    Each row is a normalize ``books`` row: ``side`` in {"bid","ask"}, ``price``/``size`` Decimal.
    An individual level with non-numeric / non-positive price or size is dropped (just that entry),
    per brti-spec §5.2.2 (erroneous prices/entries).
    """
    bids: list[Level] = []
    asks: list[Level] = []
    for r in rows:
        price = r["price"]
        size = r["size"]
        if price is None or size is None:
            continue
        price = price if isinstance(price, Decimal) else Decimal(str(price))
        size = float(size)
        if price <= 0 or size <= 0:
            continue
        lvl = Level(price, size)
        if r["side"] == BID:
            bids.append(lvl)
        elif r["side"] == ASK:
            asks.append(lvl)
    bids.sort(key=lambda lv: lv.price, reverse=True)
    asks.sort(key=lambda lv: lv.price)
    return tuple(bids), tuple(asks)


class SnapshotSeries:
    """One constituent's event-driven L2 snapshots, indexed for O(log n) as-of reconstruction.

    Built once per venue from normalize ``books`` rows; ``as_of(T)`` then returns the book state
    in effect at any Effective Time. Snapshots are ordered by ``(recv_ts_ns, snap_id)``.
    """

    def __init__(self, venue: str, snaps: list[tuple[int, int, bool, list[dict]]]) -> None:
        # snaps: list of (recv_ts_ns, snap_id, synced, rows) already sorted by (recv_ts_ns, snap_id)
        self.venue = venue
        self._snaps = snaps
        # parallel key list for bisect on (recv_ts_ns, snap_id)
        self._keys = [(s[0], s[1]) for s in snaps]

    @classmethod
    def from_rows(cls, venue: str, rows: list[dict]) -> SnapshotSeries:
        """Group flat ``books`` rows into snapshots keyed by ``snap_id`` (all rows of one state).

        ``synced`` and ``recv_ts_ns`` are taken from the snapshot's rows (constant within a
        snap_id by the schema contract; we take the first row's values).
        """
        by_snap: dict[int, dict] = {}
        for r in rows:
            sid = int(r["snap_id"])
            g = by_snap.get(sid)
            if g is None:
                g = {"recv_ts_ns": int(r["recv_ts_ns"]), "synced": bool(r["synced"]), "rows": []}
                by_snap[sid] = g
            g["rows"].append(r)
        snaps = [
            (g["recv_ts_ns"], sid, g["synced"], g["rows"])
            for sid, g in by_snap.items()
        ]
        snaps.sort(key=lambda s: (s[0], s[1]))
        return cls(venue, snaps)

    def as_of(self, effective_time_ns: int) -> AsOfBook:
        """Reconstruct the book as of ``T`` (last snapshot with ``recv_ts_ns <= T``, tie snap_id).

        Returns an empty, unsynced ``AsOfBook`` (recv_ts_ns=None) if there is no snapshot at/before
        T — the caller's staleness/erroneous filters then disregard it.
        """
        # greatest key with recv_ts_ns <= T: bisect on (T, +inf) sentinel.
        idx = bisect.bisect_right(self._keys, (effective_time_ns, _INF)) - 1
        if idx < 0:
            return AsOfBook(self.venue, None, False, (), ())
        recv_ts_ns, _sid, synced, rows = self._snaps[idx]
        bids, asks = _levels_from_rows(rows)
        return AsOfBook(self.venue, recv_ts_ns, synced, bids, asks)


_INF = float("inf")
