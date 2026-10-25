"""Normalizer CLI: ``python -m kbtc.normalize --root data/raw --out data/clean [--date ...]``.

Discovers every recorded feed under ``--root``, replays each through its venue processor, and
writes date-partitioned parquet (books / kalshi_books / trades / settlements) under ``--out``.
Prints a per-feed checkpoint-validation report (Kraken CRC32 exact-match is the acceptance gate).

Reads/transforms only — no network, no order path, no secrets.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kbtc.recorders.health import discover_feeds

from . import checkpoint as ckpt
from . import pipeline, schema, writer


def _records(paths):
    """Yield ``(recv_ts_ns, raw)`` across files, chronologically, for a continuous replay."""
    from kbtc.recorders.writer import read_records

    for p in paths:
        for rec in read_records(p):
            ts = rec.get("recv_ts_ns")
            if ts is None:
                continue
            yield ts, rec.get("raw")


# Rows buffered per dataset before a parquet row group is flushed. Bounds peak memory to ~a few
# chunks (one open writer per active date) independent of total data size — the OOM-at-scale fix.
CHUNK_ROWS = 50_000


def _process_feed(root: str, out: str, venue: str, channel: str, levels: int,
                  date: str | None, kalshi_top_of_book: bool = False) -> ckpt.CheckpointReport | None:
    files = writer.gather_feed_files(root, venue, channel, date)
    if not files:
        return None

    # Settlements: a different shape entirely (REST envelopes, not a WS book replay). Small per day
    # (one row per market), so a single buffered write is fine.
    if venue == "kalshi" and channel == "settlement":
        rows = pipeline.settlement_rows(pipeline.read_envelopes(str(f) for f in files))
        df = schema.rows_to_df(rows, schema.SETTLEMENT_SCHEMA)
        writer.write_partitioned(out, schema.DS_SETTLEMENTS, df, date=date)
        return None

    if venue == "kalshi":
        report = pipeline.kalshi_report(channel)
        # Day-7 fast path: the Brier harness only needs best yes/no, so by default (for the
        # orderbook ``market`` channel) emit SLIM top-of-book rows via the incremental extractor —
        # multi-day in minutes, not hours. ``--kalshi-full-depth`` restores the full-depth emit.
        if kalshi_top_of_book and not channel.lower().startswith("cfbenchmarks"):
            stream = pipeline.stream_kalshi_tob(channel, _records(files))
        else:
            stream = pipeline.stream_kalshi(channel, _records(files))
        books_ds = schema.DS_KALSHI_BOOKS
        book_schema = schema.KALSHI_BOOK_SCHEMA
    else:
        report = ckpt.new_report(venue, channel)
        stream = pipeline.stream_crypto(venue, channel, _records(files), report, levels=levels)
        books_ds = schema.DS_BOOKS
        book_schema = schema.BOOK_SCHEMA

    # Stream rows straight to parquet in bounded chunks (never hold the whole run in memory).
    book_w = writer.PartitionedParquetWriter(out, books_ds, venue=venue, channel=channel, date=date)
    trade_w = writer.PartitionedParquetWriter(out, schema.DS_TRADES, venue=venue, channel=channel,
                                              date=date)
    book_buf: list[dict] = []
    trade_buf: list[dict] = []
    try:
        for kind, row in stream:
            if kind == pipeline.BOOK:
                book_buf.append(row)
                if len(book_buf) >= CHUNK_ROWS:
                    book_w.write_df(schema.rows_to_df(book_buf, book_schema))
                    book_buf.clear()
            else:
                trade_buf.append(row)
                if len(trade_buf) >= CHUNK_ROWS:
                    trade_w.write_df(schema.rows_to_df(trade_buf, schema.TRADE_SCHEMA))
                    trade_buf.clear()
        if book_buf:
            book_w.write_df(schema.rows_to_df(book_buf, book_schema))
        if trade_buf:
            trade_w.write_df(schema.rows_to_df(trade_buf, schema.TRADE_SCHEMA))
        # Publish atomically ONLY after the whole feed replayed without error. A crash before here
        # leaves temp files (never a glob-visible part.parquet) and any prior complete part intact.
        book_w.close()
        trade_w.close()
    finally:
        book_w.abort()   # no-op after a successful close(); else discards in-flight temp files
        trade_w.abort()
    return report


def _render(reports: list[ckpt.CheckpointReport]) -> str:
    rows = [("VENUE", "CHANNEL", "METHOD", "CHECKPTS", "MATCH", "MISMATCH",
             "MED-DEV", "P95-DEV", "MAX-DEV")]
    for r in reports:
        rows.append((r.venue, r.channel, r.method, str(r.n_checkpoints), str(r.n_matches),
                     str(r.n_mismatches), f"{r.median_deviation:.4g}", f"{r.p95_deviation:.4g}",
                     f"{r.max_deviation:.4g}"))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for ri, r in enumerate(rows):
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
        if ri == 0:
            out.append("  ".join("-" * widths[i] for i in range(len(widths))))
    notes = [f"  note {r.venue}/{r.channel}: {r.notes}" for r in reports if r.notes]
    return "\n".join(out + ([""] + notes if notes else []))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m kbtc.normalize",
        description="Normalize raw recordings → clean parquet (books, trades, settlements).",
    )
    p.add_argument("--root", default="data/raw", help="raw data root (default: data/raw)")
    p.add_argument("--out", default="data/clean", help="clean output root (default: data/clean)")
    p.add_argument("--date", default=None, help="restrict to a single UTC date YYYY-MM-DD")
    p.add_argument("--levels", type=int, default=10, help="top-N L2 levels per side (default: 10)")
    p.add_argument("--kalshi-full-depth", action="store_true",
                   help="emit the full Kalshi order book (slow). Default: slim top-of-book only "
                        "(best yes/no), which is all the Brier harness reads (day-7 fast path).")
    args = p.parse_args(argv)

    if not Path(args.root).exists():
        print(f"error: raw root does not exist: {args.root}", file=sys.stderr)
        return 2

    reports: list[ckpt.CheckpointReport] = []
    for venue, channel in discover_feeds(args.root):
        rep = _process_feed(args.root, args.out, venue, channel, args.levels, args.date,
                            kalshi_top_of_book=not args.kalshi_full_depth)
        if rep is not None:
            reports.append(rep)

    if reports:
        print(_render(reports))

    # Checkpoint MISMATCHES are reported, never silent — but they are NOT a CLI failure: a Kraken
    # CRC mismatch on live data is a genuine dropped-frame gap that the recorder resynced from (it
    # should line up with the health report's sequence-gap count), not necessarily a normalizer
    # bug. The acceptance "reconstructed L2 matches venue snapshots at checkpoints" is asserted on
    # a clean fixture by the tests (Kraken clean frames match the CRC exactly). Exit is 0 unless a
    # usage/IO error occurred above.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
