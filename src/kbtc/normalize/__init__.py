"""kbtc.normalize — raw recordings → clean parquet (ROADMAP 2.1).

Public API:

* :func:`pipeline.normalize_crypto` — a crypto feed → event-driven top-N L2 book rows + trade
  rows + a checkpoint report.
* :func:`pipeline.normalize_kalshi` — a Kalshi feed → yes/no L2 book rows + trade rows.
* :func:`pipeline.settlement_rows` — settlement envelopes → rows.
* :mod:`schema` — the parquet schemas and the (documented) book-representation decision.
* :mod:`checkpoint` — per-venue checkpoint validation (Kraken CRC32 is the gold oracle).
* :mod:`writer` — date-partitioned parquet output.

Reads/transforms only: no network, no order path.
"""
from __future__ import annotations

from . import checkpoint, extract, pipeline, schema, writer

__all__ = ["checkpoint", "extract", "pipeline", "schema", "writer"]
