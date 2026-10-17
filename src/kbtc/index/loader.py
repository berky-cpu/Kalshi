"""Load normalized constituent book snapshots and reconstruct per-venue as-of books (2.2 Part A).

Reads the normalizer's clean parquet (schema.py ``books`` dataset):

    {clean}/books/venue={v}/channel={c}/date=YYYY-MM-DD/part.parquet

into a per-venue :class:`SnapshotSeries`, which the replica then samples as-of each Effective
Time. Reads only; no network, no order path. Prices come back as exact ``Decimal`` (polars
``Decimal`` column) and are kept exact; timestamps are integer ns.

CONSTITUENT-SUBSET handling: ``load_constituent_series`` takes the venue→channel map of whatever
we actually recorded and filters it to the BRTI constituents active at the as-of date (§1). A
venue we do not record simply never appears — the replica runs on the subset and the result
records which venues were used (``BrtiResult.venues_used``). The missing-venue set is returned so
the tracking-error note can quantify the gap (O-8).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from .book import SnapshotSeries
from .params import active_constituents

NS_PER_S = 1_000_000_000

# What we currently record of the BRTI constituents, as venue→L2 book channel. Coinbase pending
# the 1.1d adapter; gemini/lmax/bullish/cryptocom not recorded (O-8). Callers may override.
DEFAULT_CONSTITUENT_CHANNELS: dict[str, str] = {
    "bitstamp": "live_orders_btcusd",   # recorders/bitstamp.py channel token
    "kraken": "level3",                 # recorders/kraken.py channel token
    "coinbase": "full",                 # recorders/coinbase.py channel token (L3 "full"; 1.1d pending)
}


def _date_str(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS_PER_S, tz=UTC).strftime("%Y-%m-%d")


def dates_spanning(start_ns: int, end_ns: int) -> list[str]:
    """UTC date strings covering [start_ns, end_ns] inclusive (to pick parquet date partitions)."""
    out: list[str] = []
    d0 = datetime.fromtimestamp(start_ns // NS_PER_S, tz=UTC).date()
    d1 = datetime.fromtimestamp(end_ns // NS_PER_S, tz=UTC).date()
    cur = d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = cur.fromordinal(cur.toordinal() + 1)
    return out


def book_part_paths(clean_root: str | Path, venue: str, channel: str,
                    dates: list[str] | None = None) -> list[Path]:
    """All ``books`` parquet parts for a feed, optionally restricted to ``dates`` (sorted)."""
    base = Path(clean_root) / "books" / f"venue={venue}" / f"channel={channel}"
    out: list[Path] = []
    for datedir in sorted(base.glob("date=*")):
        d = datedir.name[len("date="):]
        if dates is not None and d not in dates:
            continue
        for part in sorted(datedir.glob("*.parquet")):
            out.append(part)
    return out


def load_series(clean_root: str | Path, venue: str, channel: str,
                dates: list[str] | None = None) -> SnapshotSeries:
    """Read a feed's book snapshots over ``dates`` into a :class:`SnapshotSeries`.

    Only the columns the replica needs are read. Rows are ordered (recv_ts_ns, snap_id) by the
    series builder. An empty/missing feed yields an empty series (every as_of → no-book).
    """
    paths = book_part_paths(clean_root, venue, channel, dates)
    cols = ["recv_ts_ns", "snap_id", "synced", "side", "price", "size"]
    if not paths:
        return SnapshotSeries.from_rows(venue, [])
    frames = [pl.read_parquet(p, columns=cols) for p in paths]
    df = pl.concat(frames) if len(frames) > 1 else frames[0]
    return SnapshotSeries.from_rows(venue, df.to_dicts())


def load_constituent_series(
    clean_root: str | Path,
    effective_time_ns: int,
    channels: dict[str, str] | None = None,
    dates: list[str] | None = None,
) -> tuple[dict[str, SnapshotSeries], frozenset[str]]:
    """Load SnapshotSeries for every recorded BRTI constituent active at the as-of date.

    Returns ``(series_by_venue, missing_constituents)`` where ``missing_constituents`` is the set
    of active BRTI constituents we do NOT have a series for (no channel mapping, or no parquet) —
    the honest O-8 coverage gap for the tracking-error note.
    """
    channels = channels or DEFAULT_CONSTITUENT_CHANNELS
    active = active_constituents(effective_time_ns)
    series: dict[str, SnapshotSeries] = {}
    for venue in sorted(active):
        chan = channels.get(venue)
        if chan is None:
            continue
        s = load_series(clean_root, venue, chan, dates)
        if s._snaps:  # only include venues with actual recorded data
            series[venue] = s
    missing = frozenset(active - set(series.keys()))
    return series, missing


def books_at(series_by_venue: dict[str, SnapshotSeries], effective_time_ns: int) -> dict:
    """Reconstruct every venue's as-of book at ``effective_time_ns`` (venue→AsOfBook)."""
    return {v: s.as_of(effective_time_ns) for v, s in series_by_venue.items()}
