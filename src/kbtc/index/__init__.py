"""BRTI replica + tracking-error harness (ROADMAP 2.2).

Implements the CME CF Bitcoin Real-Time Index strictly per ``docs/brti-spec.md`` (methodology
v17.0). The replica is constituent-agnostic: :func:`compute_index` runs on whatever constituent
books are supplied (a SUBSET is fine — we currently record Bitstamp + Kraken of the 7 BRTI
constituents) and the result records which venues were used.

Pipeline (brti-spec §3.8): per-venue contingency filters (§4/§5) → consolidate + order-size cap
C_T + uncross (§3.2/§3.7) → dynamic spacing s_T (§3.6) → price-volume curves (§3.3) → utilized
depth v_T (§3.4) → exponential depth-weighted mean, λ=1/(0.3·v_T) (§3.5).
"""
from __future__ import annotations

from .book import AsOfBook, Level, SnapshotSeries
from .brti import BrtiResult, compute_index
from .filters import DeviationFilter
from .params import BRTI_CONSTITUENTS, WINDOW_ENDPOINTS, BrtiParams, active_constituents

__all__ = [
    "AsOfBook",
    "Level",
    "SnapshotSeries",
    "BrtiResult",
    "compute_index",
    "DeviationFilter",
    "BrtiParams",
    "BRTI_CONSTITUENTS",
    "WINDOW_ENDPOINTS",
    "active_constituents",
]
