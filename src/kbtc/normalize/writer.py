"""Parquet output + feed-file discovery for the normalizer.

Writes date-partitioned parquet under the clean root:

    {out}/books/venue={v}/channel={c}/date=YYYY-MM-DD/part.parquet
    {out}/kalshi_books/venue=kalshi/channel={c}/date=.../part.parquet
    {out}/trades/venue={v}/channel={c}/date=.../part.parquet
    {out}/settlements/date=YYYY-MM-DD/part.parquet

Partitioning is by the UTC date of ``recv_ts_ns`` (each row carries its own date), so a continuous
per-feed replay (which preserves book continuity across hour/day boundaries) still lands in the
right day partition. Re-running overwrites a date's ``part.parquet`` deterministically (idempotent).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

NS_PER_S = 1_000_000_000


def iso_date(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS_PER_S, tz=UTC).strftime("%Y-%m-%d")


def gather_feed_files(root: str | Path, venue: str, channel: str,
                      date: str | None = None) -> list[Path]:
    """All raw ``.jsonl.zst`` files for a feed, chronologically (date, then hour, then open_ts)."""
    base = Path(root) / venue / channel
    out: list[tuple[str, str, Path]] = []
    for datedir in base.glob("date=*"):
        d = datedir.name[len("date="):]
        if date is not None and d != date:
            continue
        for f in datedir.glob("*.jsonl.zst"):
            out.append((d, f.name, f))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


def write_partitioned(out_root: str | Path, dataset: str, df: pl.DataFrame,
                      venue: str | None = None, channel: str | None = None,
                      date: str | None = None) -> list[Path]:
    """Write ``df`` as date-partitioned parquet; returns the paths written.

    ``df`` must have an ``recv_ts_ns`` column (used to derive the date partition). If ``date`` is
    given, only that date's rows are written; otherwise a part is written per distinct date.
    """
    if df.is_empty():
        return []
    df = df.with_columns(
        pl.from_epoch(pl.col("recv_ts_ns"), time_unit="ns").dt.strftime("%Y-%m-%d").alias("_date")
    )
    written: list[Path] = []
    for (d,), part in df.partition_by("_date", as_dict=True).items():
        if date is not None and d != date:
            continue
        part = part.drop("_date")
        subdir = Path(out_root) / dataset
        if venue is not None:
            subdir = subdir / f"venue={venue}"
        if channel is not None:
            subdir = subdir / f"channel={channel}"
        subdir = subdir / f"date={d}"
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / "part.parquet"
        part.write_parquet(path)
        written.append(path)
    return written
