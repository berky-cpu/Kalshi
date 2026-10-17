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


def _process_feed(root: str, out: str, venue: str, channel: str, levels: int,
                  date: str | None) -> ckpt.CheckpointReport | None:
    files = writer.gather_feed_files(root, venue, channel, date)
    if not files:
        return None

    # Settlements: a different shape entirely (REST envelopes, not a WS book replay).
    if venue == "kalshi" and channel == "settlement":
        rows = pipeline.settlement_rows(pipeline.read_envelopes(str(f) for f in files))
        df = schema.rows_to_df(rows, schema.SETTLEMENT_SCHEMA)
        writer.write_partitioned(out, schema.DS_SETTLEMENTS, df, date=date)
        return None

    if venue == "kalshi":
        res = pipeline.normalize_kalshi(channel, _records(files))
        books_ds = schema.DS_KALSHI_BOOKS
        book_schema = schema.KALSHI_BOOK_SCHEMA
    else:
        res = pipeline.normalize_crypto(venue, channel, _records(files), levels=levels)
        books_ds = schema.DS_BOOKS
        book_schema = schema.BOOK_SCHEMA

    writer.write_partitioned(out, books_ds, schema.rows_to_df(res.books, book_schema),
                             venue=venue, channel=channel, date=date)
    writer.write_partitioned(out, schema.DS_TRADES,
                             schema.rows_to_df(res.trades, schema.TRADE_SCHEMA),
                             venue=venue, channel=channel, date=date)
    return res.checkpoint


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
    args = p.parse_args(argv)

    if not Path(args.root).exists():
        print(f"error: raw root does not exist: {args.root}", file=sys.stderr)
        return 2

    reports: list[ckpt.CheckpointReport] = []
    for venue, channel in discover_feeds(args.root):
        rep = _process_feed(args.root, args.out, venue, channel, args.levels, args.date)
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
