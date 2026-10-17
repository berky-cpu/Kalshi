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
import pyarrow.parquet as pq

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


class PartitionedParquetWriter:
    """Streaming, date-partitioned parquet writer — bounded memory regardless of total rows.

    The buffered :func:`write_partitioned` holds a whole dataset in RAM before writing once, so peak
    memory scales with total rows and OOM-kills a multi-hour/day normalize on a small VPS. This
    writer instead appends each incoming chunk as a parquet **row group** into the SAME single
    ``part.parquet`` per ``date=`` partition (one open ``pyarrow.parquet.ParquetWriter`` per date),
    so the clean dataset layout and the downstream reader (``index/loader.py`` globs ``*.parquet``;
    tests read ``part.parquet`` directly) are UNCHANGED. Row order is preserved: chunks arrive in
    replay order and row groups are appended in order.

    Call :meth:`write_df` with successive chunks (any size), then :meth:`close` to PUBLISH, or
    :meth:`abort` to discard. Partition directories are created lazily (only when a matching-date
    chunk is actually written), so a date-filtered run that produces no rows writes nothing.

    ATOMICITY (data-integrity: the index loader globs ``*.parquet`` over ``data/clean`` and must
    never read a half-written file). Row groups stream to a per-partition TEMP file
    ``part.parquet.tmp`` (bounded memory — row groups hit disk, not RAM). The glob-visible
    ``part.parquet`` is produced ONLY by ``os.replace(tmp, part.parquet)`` on a SUCCESSFUL
    :meth:`close`, after the footer is finalized — an atomic rename on the same filesystem. So:

    * a crash mid-stream (no ``close``) leaves only a ``.tmp`` (which ``*.parquet`` never matches)
      and, on a RE-RUN, the previous complete ``part.parquet`` stays intact (never truncated —
      we only touch the tmp until the final rename);
    * a successful re-run atomically REPLACES the old ``part.parquet`` (idempotent).

    A stale ``.tmp`` left by an earlier crash is removed when its partition writer (re)opens, so
    retries don't accumulate cruft.
    """

    def __init__(self, out_root: str | Path, dataset: str, venue: str | None = None,
                 channel: str | None = None, date: str | None = None) -> None:
        self.out_root = Path(out_root)
        self.dataset = dataset
        self.venue = venue
        self.channel = channel
        self.date_filter = date
        self._writers: dict[str, pq.ParquetWriter] = {}
        self._tmp: dict[str, Path] = {}      # date -> in-flight temp path
        self._final: dict[str, Path] = {}    # date -> glob-visible destination
        self._schemas: dict[str, object] = {}  # date -> pyarrow.Schema that opened the writer

    def _part_dir(self, d: str) -> Path:
        subdir = self.out_root / self.dataset
        if self.venue is not None:
            subdir = subdir / f"venue={self.venue}"
        if self.channel is not None:
            subdir = subdir / f"channel={self.channel}"
        subdir = subdir / f"date={d}"
        subdir.mkdir(parents=True, exist_ok=True)
        return subdir

    def write_df(self, df: pl.DataFrame) -> None:
        """Append ``df`` (one chunk) as row group(s) to the appropriate per-date TEMP file."""
        if df.is_empty():
            return
        df = df.with_columns(
            pl.from_epoch(pl.col("recv_ts_ns"), time_unit="ns").dt.strftime("%Y-%m-%d")
            .alias("_date")
        )
        for (d,), part in df.partition_by("_date", as_dict=True).items():
            if self.date_filter is not None and d != self.date_filter:
                continue
            table = part.drop("_date").to_arrow()
            w = self._writers.get(d)
            if w is None:
                # First chunk for this date defines the on-disk schema; every chunk comes from the
                # same enforced polars schema, so later chunks' arrow schema is identical.
                subdir = self._part_dir(d)
                tmp = subdir / "part.parquet.tmp"
                if tmp.exists():  # clear a stale tmp from an earlier crashed run
                    tmp.unlink()
                w = pq.ParquetWriter(tmp, table.schema)
                self._writers[d] = w
                self._tmp[d] = tmp
                self._final[d] = subdir / "part.parquet"
                self._schemas[d] = table.schema
            elif table.schema != self._schemas[d]:
                table = table.cast(self._schemas[d])  # reconcile (no-op in practice)
            w.write_table(table)

    def close(self) -> list[Path]:
        """PUBLISH: finalize each temp file's footer, then atomically rename it into place.

        Returns the glob-visible ``part.parquet`` paths. After this the instance holds no open
        writers, so a subsequent :meth:`abort` (e.g. in a ``finally``) is a no-op.
        """
        import os

        written: list[Path] = []
        for d, w in self._writers.items():
            w.close()  # finalize footer in the tmp file
            os.replace(self._tmp[d], self._final[d])  # atomic publish (same filesystem)
            written.append(self._final[d])
        self._writers.clear()
        self._tmp.clear()
        return written

    def abort(self) -> None:
        """Discard any UNcommitted partitions: close handles and delete temp files, publishing
        nothing. Idempotent and safe to call after :meth:`close` (no-op). Never touches an existing
        ``part.parquet`` — a crashed run leaves a prior complete part intact."""
        for d, w in self._writers.items():
            try:
                w.close()
            except Exception:  # noqa: BLE001 - releasing a handle on an aborted write
                pass
            tmp = self._tmp.get(d)
            if tmp is not None and tmp.exists():
                tmp.unlink()
        self._writers.clear()
        self._tmp.clear()
