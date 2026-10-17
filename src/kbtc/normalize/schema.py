"""Normalized parquet schemas + the book-representation decision (ROADMAP 2.1).

BOOK REPRESENTATION (the load-bearing choice for Phase 2) — **event-driven L2 snapshots**.

We emit one top-``N``-levels-per-side L2 snapshot *whenever the aggregated top-N book changes*,
each stamped with the ``recv_ts_ns`` of the update that caused the change and a per-feed
monotonically increasing ``snap_id``. We do NOT resample onto a fixed time grid.

Why (justification vs. a fixed-cadence grid):

* The Phase-2 BRTI replica (``docs/brti-spec.md``) must consolidate every constituent book **at
  each Effective Time** (200 ms internal / 1 Hz standard feed). An event-driven top-N series lets
  the replica reconstruct each venue's book *as of* any Effective Time with a last-value (as-of)
  join — no interpolation, no information loss. The fixed grid is recoverable downstream from the
  event series, but not vice-versa: a grid chosen now would throw away the sub-cadence updates
  that later queue/liquidity and lead-lag (2.3) work needs.
* Each row carries the exact ``recv_ts_ns`` of its triggering update, so as-of sampling is exact.
* Emitting only on an actual top-N change keeps the series compact (a deep-book churn that does
  not touch the top N produces no row) while still representing the book state at every instant a
  consumer can observe.

``N`` is configurable (CLI ``--levels``, default 10). Note Kraken ``level3`` is already truncated
to ``depth=10`` per side by the recorder, so ``N>10`` yields ≤10 levels for Kraken.

PRICES ARE NEVER FLOAT. All price/size columns are parquet ``Decimal(38, 8)`` (pyarrow
decimal128), built from Python ``Decimal`` parsed from the venue's exact transmitted strings, so
there is no binary-float drift (CLAUDE.md conventions). Scale 8 covers every venue we record
(crypto prices ≤2 dp, sizes ≤8 dp; Kraken 1 dp prices). UNITS differ by dataset and are documented
per schema below — the ``venue``/dataset disambiguates, so no column ever mixes units.

Timestamps are integer nanoseconds UTC (``recv_ts_ns`` stamped on receipt; ``exch_ts_ns`` the
venue's own event time where the feed carries one, else null).

AS-OF JOIN TIE-BREAK (load-bearing for the 2.2 replica). Two consecutive snapshots CAN share the
same ``recv_ts_ns`` (a burst of updates stamped within the same nanosecond, or a venue event-time
collision). ``snap_id`` is strictly monotonic per feed, so a consumer sampling the book *as of* an
Effective Time MUST order by ``(recv_ts_ns, snap_id)`` and take the greatest such row — never by
``recv_ts_ns`` alone, which is not unique. All rows of one book state share one ``snap_id``.

COMPLETENESS MARKER (``synced``). Each snapshot carries a boolean ``synced`` = "this book state is
reconstructed from a COMPLETE baseline and is not mid-resync". It is False (a) from feed start until
a complete-book baseline is established — an in-band ``snapshot`` frame (Kraken/Bybit/Kalshi) or a
persisted REST snapshot (Coinbase/Binance/Bitstamp). As of ROADMAP 1.1c the recorder PERSISTS the
REST snapshot it fetches into the recording (a tagged envelope, both on initial sync and on gap
resync), so a replay of those venues reconstructs a COMPLETE book and goes ``synced=True`` from the
snapshot onward; an OLD, snapshot-less (WS-only) recording of those venues has no such baseline and
stays ``synced=False`` throughout (the honest result: the book is partial). It is also False (b)
while the feed is post-gap/pre-resync (``needs_resnapshot``/``awaiting_snapshot``), so a diverged
book is flagged, never silently emitted as valid. Rows are NEVER suppressed; downstream filters on
``synced`` as needed.
"""
from __future__ import annotations

import polars as pl

# Exact decimal money/qty type shared by every price/size column (no float, ever).
DEC = pl.Decimal(precision=38, scale=8)

# ---------------------------------------------------------------------------------------
# books  (crypto consolidated-constituent L2; UNITS: price & size in the venue's native
# asset terms — USD(C)/BTC dollars for price, BTC for size — exact to 1e-8).
# ---------------------------------------------------------------------------------------
BOOK_SCHEMA: dict[str, pl.DataType] = {
    "recv_ts_ns": pl.Int64,     # stamped on receipt, before parsing
    "exch_ts_ns": pl.Int64,     # venue event time (nullable)
    "venue": pl.Utf8,
    "product": pl.Utf8,
    "channel": pl.Utf8,
    "snap_id": pl.Int64,        # monotonic per feed; rows sharing (venue,channel,snap_id) = 1 book
    "synced": pl.Boolean,       # book is from a complete baseline AND not mid-resync (see header)
    "side": pl.Utf8,            # "bid" | "ask"
    "level": pl.Int32,          # 0 = best
    "price": DEC,
    "size": DEC,
}

# ---------------------------------------------------------------------------------------
# kalshi_books  (Kalshi yes/no order book — SAME row shape as books but different UNITS:
# price is INTEGER CENTS (0..100), size is CONTRACT COUNT. ``side`` is the Kalshi side token
# ("yes"/"no", or "bid"/"ask" on perps) as received — NOT a crypto bid/ask. Schema is flagged
# UNVERIFIED per ROADMAP 1.1b: the KXBTC15M orderbook payload and seq semantics are not yet
# confirmed against the official channel reference, so we normalize exactly what we record.)
# ---------------------------------------------------------------------------------------
KALSHI_BOOK_SCHEMA = dict(BOOK_SCHEMA)

# ---------------------------------------------------------------------------------------
# trades  (every venue's tape, one schema. ``side`` = taker/aggressor side: "buy"/"sell" for
# crypto, "yes"/"no" for Kalshi. UNITS mirror the venue's book dataset.)
# ---------------------------------------------------------------------------------------
TRADE_SCHEMA: dict[str, pl.DataType] = {
    "recv_ts_ns": pl.Int64,
    "exch_ts_ns": pl.Int64,     # nullable
    "venue": pl.Utf8,
    "product": pl.Utf8,
    "channel": pl.Utf8,
    "trade_id": pl.Utf8,        # nullable; string to hold both ints and uuids
    "side": pl.Utf8,            # taker side (nullable where the venue omits it)
    "price": DEC,
    "size": DEC,
}

# ---------------------------------------------------------------------------------------
# settlements  (normalized SettlementRecord; ROADMAP 1.2 ground truth. Monetary values Decimal,
# never float. ``settlement_value`` = per-contract YES payout $ (0/1); ``close_value`` =
# expiration_value = close-window 60 s BRTI avg; ``open_value`` = floor_strike = open ref/strike.)
# ---------------------------------------------------------------------------------------
SETTLEMENT_SCHEMA: dict[str, pl.DataType] = {
    "market_ticker": pl.Utf8,
    "event_ticker": pl.Utf8,
    "status": pl.Utf8,
    "result": pl.Utf8,
    "settlement_value": DEC,    # nullable (payout $, 0/1)
    "close_value": DEC,         # nullable (expiration_value)
    "open_value": DEC,          # nullable (floor_strike)
    "open_time_ns": pl.Int64,   # nullable
    "close_time_ns": pl.Int64,  # nullable
    "settle_time_ns": pl.Int64,  # nullable
    "recv_ts_ns": pl.Int64,
    "raw_json": pl.Utf8,        # untouched provenance (the stored envelope)
}

# Dataset directory names under the clean root.
DS_BOOKS = "books"
DS_KALSHI_BOOKS = "kalshi_books"
DS_TRADES = "trades"
DS_SETTLEMENTS = "settlements"


def empty_df(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def rows_to_df(rows: list[dict], schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """Build a typed DataFrame from row dicts, enforcing the exact schema (incl. Decimal).

    Empty input returns a correctly-typed empty frame so downstream writes stay schema-stable.
    """
    if not rows:
        return empty_df(schema)
    cols = {name: [r.get(name) for r in rows] for name in schema}
    return pl.DataFrame(cols, schema=schema)
