"""Per-market data loader for the 4.2 gate (ROADMAP 4.2).

Turns recorded data into the engine's inputs, one 15-min KXBTC15M market at a time:

* ``market_from_settlement_row`` — a settled market's ground truth (ticker, open/close, strike,
  outcome) from the normalized ``settlements`` dataset → :class:`BacktestMarket`.
* ``cf_index_ticks`` — published CF BRTI value prints (the PRIMARY index) parsed from the raw
  ``kalshi/cfbenchmarks`` stream into :class:`IndexTick`s, stamped at ``recv_ts_ns`` (the causal
  instant WE observed the value — no look-ahead).
* ``market_records_in_window`` — the FULL multi-ticker ``kalshi/market`` raw stream sliced to a
  time window (the engine's ticker guard isolates our market); asserts global ``recv_ts_ns``
  ordering across concatenated files (the engine assumes sorted input).
* ``hour_files_covering`` — selects only the hourly files whose span overlaps the window, so one
  market reads ~1-2 files, not a whole day (bounded memory/IO on the box).

Pure helpers take iterables of ``(recv_ts_ns, raw)`` records so they unit-test without files; the
file wrappers glob + ``read_records`` + call the pure helper. Reads only — no network, no order path.
"""
from __future__ import annotations

import glob
import json
import re
from decimal import Decimal
from typing import Any, Iterable, Iterator

from kbtc.recorders.base import as_dict

from .engine import BacktestMarket
from .events import IndexTick

NS_PER_S = 1_000_000_000
NS_PER_MS = 1_000_000

Record = tuple[int, Any]


# ---------------------------------------------------------------------------------------
# Settlement -> BacktestMarket.
# ---------------------------------------------------------------------------------------
def market_from_settlement_row(row: dict) -> BacktestMarket | None:
    """Build a :class:`BacktestMarket` from a normalized settlement row; None if unusable.

    Requires a terminal ``result`` in {yes, no}, numeric ``open_value`` (the strike / open-window
    average) and ``close_value``, and integer open/close times. Mirrors the empirically-confirmed
    ``result == (close_value >= open_value)`` identity (docs/facts.md) but trusts the recorded
    ``result`` as ground truth.
    """
    result = (row.get("result") or "").strip().lower()
    if result not in {"yes", "no"}:
        return None
    ticker = row.get("market_ticker")
    open_ns, close_ns = row.get("open_time_ns"), row.get("close_time_ns")
    strike = row.get("open_value")
    # strike (open_value) is REQUIRED — the pull-rule z is computed against it; a market without it
    # cannot be gated safely, so skip it rather than quote blind.
    if not ticker or open_ns is None or close_ns is None or strike is None:
        return None
    exp = row.get("close_value")
    return BacktestMarket(
        ticker=str(ticker), open_time_ns=int(open_ns), close_time_ns=int(close_ns),
        strike=Decimal(str(strike)), result_yes=(result == "yes"),
        expiration_value=(Decimal(str(exp)) if exp is not None else None),
    )


# ---------------------------------------------------------------------------------------
# CF BRTI value feed -> IndexTicks.
# ---------------------------------------------------------------------------------------
def cf_value_from_msg(msg: Any) -> float | None:
    """Extract the BRTI level (USD) from one ``cfbenchmarks_value`` frame, or None.

    The payload nests under ``msg``; ``data`` is a JSON STRING ``{"type":"value","time":..,
    "value":"<2dp>"}`` (VERIFIED, docs/facts.md). We read the current ``value`` (the index level fed
    to the fair value), NOT ``avg_60s_data`` (the forming settlement average).
    """
    m = as_dict(msg)
    if not isinstance(m, dict) or m.get("type") != "cfbenchmarks_value":
        return None
    inner = m.get("msg")
    body = inner if isinstance(inner, dict) else m
    data = body.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if isinstance(data, dict) and data.get("value") is not None:
        try:
            return float(data["value"])
        except (TypeError, ValueError):
            return None
    return None


def cf_index_ticks(records: Iterable[Record], lo_ns: int, hi_ns: int) -> list[IndexTick]:
    """Parse windowed CF value records into IndexTicks (stamped at recv_ts_ns), sorted + de-duped.

    Drops frames outside ``[lo_ns, hi_ns]`` and any with a non-positive / unparseable value. Output is
    sorted by ``recv_ts_ns``; consecutive duplicate timestamps keep the last value.
    """
    ticks: list[IndexTick] = []
    for recv, raw in records:
        if recv < lo_ns or recv > hi_ns:
            continue
        v = cf_value_from_msg(raw)
        if v is not None and v > 0:
            ticks.append(IndexTick(recv_ts_ns=recv, price=v))
    ticks.sort(key=lambda t: t.recv_ts_ns)
    out: list[IndexTick] = []
    for t in ticks:
        if out and out[-1].recv_ts_ns == t.recv_ts_ns:
            out[-1] = t
        else:
            out.append(t)
    return out


# ---------------------------------------------------------------------------------------
# Market raw stream windowing + ordering guarantee.
# ---------------------------------------------------------------------------------------
class RecvOrderError(ValueError):
    """Raised when concatenated records are not globally sorted by recv_ts_ns (engine assumes sorted)."""


def market_records_in_window(records: Iterable[Record], lo_ns: int, hi_ns: int,
                             *, check_order: bool = True) -> list[Record]:
    """Slice ``(recv_ts_ns, raw)`` records to ``[lo_ns, hi_ns]`` and (by default) VERIFY global order.

    The engine merges and assumes non-decreasing ``recv_ts_ns``; across concatenated hourly files a
    mis-ordered boundary would silently corrupt the queue sim, so we check the RAW input order (before
    windowing) and raise :class:`RecvOrderError` on any decrease. Returns the windowed records.
    """
    out: list[Record] = []
    last = None
    for recv, raw in records:
        if check_order and last is not None and recv < last:
            raise RecvOrderError(f"recv_ts_ns went backwards: {recv} < {last}")
        last = recv
        if lo_ns <= recv <= hi_ns:
            out.append((recv, raw))
    return out


_TS_IN_NAME = re.compile(r"_(\d{16,19})\.jsonl\.zst$")


def _file_start_ns(path: str) -> int | None:
    m = _TS_IN_NAME.search(path)
    return int(m.group(1)) if m else None


def hour_files_covering(paths: Iterable[str], lo_ns: int, hi_ns: int) -> list[str]:
    """Select only the hourly files whose time span overlaps ``[lo_ns, hi_ns]``.

    Files are named ``HH_<startns>.jsonl.zst``; sorted by the embedded start ns, a file covers the
    window iff ``start <= hi`` and the NEXT file's start is ``> lo`` (so the window could fall inside
    this file). Files without a parseable start ns are always included (conservative). This keeps a
    per-market read to ~1-2 files instead of a whole day.
    """
    stamped = sorted(((p, _file_start_ns(p)) for p in paths),
                     key=lambda ps: (ps[1] is None, ps[1] if ps[1] is not None else 0))
    known = [(p, s) for p, s in stamped if s is not None]
    out = [p for p, s in stamped if s is None]
    for i, (p, s) in enumerate(known):
        nxt = known[i + 1][1] if i + 1 < len(known) else None
        if s <= hi_ns and (nxt is None or nxt > lo_ns):
            out.append(p)
    return out


# ---------------------------------------------------------------------------------------
# File-backed wrappers (exercised on the box; thin over the pure helpers above).
# ---------------------------------------------------------------------------------------
def _read_records(path: str) -> Iterator[Record]:
    from kbtc.recorders.writer import read_records
    for rec in read_records(path):
        yield (rec["recv_ts_ns"], rec["raw"])


def load_market_records(market_root: str, dates: list[str], lo_ns: int, hi_ns: int) -> list[Record]:
    """Read the raw ``kalshi/market`` stream for ``dates``, covering-files only, sliced to the window."""
    paths: list[str] = []
    for d in dates:
        paths += glob.glob(f"{market_root}/date={d}/*.jsonl.zst")
    cover = hour_files_covering(paths, lo_ns, hi_ns)

    def gen() -> Iterator[Record]:
        for p in sorted(cover, key=lambda q: (_file_start_ns(q) or 0)):
            yield from _read_records(p)

    return market_records_in_window(gen(), lo_ns, hi_ns)


def load_cf_index_ticks(cf_root: str, dates: list[str], lo_ns: int, hi_ns: int) -> list[IndexTick]:
    """Read raw CF ``cfbenchmarks`` value frames for ``dates`` and build windowed IndexTicks."""
    paths: list[str] = []
    for d in dates:
        paths += glob.glob(f"{cf_root}/date={d}/*.jsonl.zst")
    cover = hour_files_covering(paths, lo_ns, hi_ns)

    def gen() -> Iterator[Record]:
        for p in sorted(cover, key=lambda q: (_file_start_ns(q) or 0)):
            yield from _read_records(p)

    return cf_index_ticks(gen(), lo_ns, hi_ns)
