"""Recorder health report — the operator's monitoring view over the 24/7 recorders.

PURE analysis, no live sockets (ROADMAP 1.4). Given a data root and a time window it reads the
hourly-rotating ``.jsonl.zst`` files written by ``recorders/writer.py`` and computes, per feed
``{venue, channel}``:

DATA vs LIVENESS (the gating-correctness rule). Every metric below is based on **data frames
only** — actual book/trade/lifecycle events — NOT on control frames (subscription acks,
heartbeats, pongs, status/error). A feed whose book has stalled but whose connection still emits
a heartbeat inside ``max_quiet_s`` must NOT look healthy: counting the heartbeat as "up" is a
false pass on 1.3's gate. So a lightweight per-venue classifier (``is_data_frame``) strips
control frames before we measure coverage and rate. (A venue with no classifier falls back to
counting all frames and sets ``data_classified = False`` so the caller knows.)

* **Disk usage** — total bytes and per-day bytes from the ``.jsonl.zst`` file sizes (all files;
  disk is physical and not classified).
* **Message rate** — data-frame count and ``msgs/sec``. This is a *fencepost* rate over the DATA
  span: ``msgs_per_s = n / (last_data_ts - first_data_ts)`` (0 for < 2 data frames). Note the
  asymmetry (F3): the rate uses the data span while coverage below uses the full window — a
  deliberate choice so a single sparse burst is not mistaken for window-wide throughput; the
  ``healthy`` gate combines both so neither alone can give a false pass. Also per-hour data counts.
* **Uptime / coverage** — the fraction of the requested window actually covered by DATA and the
  largest no-data gaps. A *coverage gap* is any interval with no data frame longer than
  ``max_quiet_s`` (configurable); its full duration counts as downtime. Heartbeats do NOT fill a
  gap. ``gap_time_fraction = downtime / window``; ``uptime_fraction = 1 - gap_time_fraction``.
  The ``gap_time_fraction`` is the primary ROADMAP 1.3 "< 0.1 % gap time per feed" metric.
* **Sequence gaps** — we do NOT re-implement gap detection. We import each venue's pure
  ``MessageProcessor`` (the same ones the live recorders run) and REPLAY the recorded raw
  payloads through it, then count the structured ``GapDetected`` events it emits. A feed whose
  venue has no processor reports coverage/rate/disk with sequence gaps marked n/a.
* **``healthy`` decision** — ROADMAP 1.3 acceptance is the CONJUNCTION
  ``gap_time_fraction < gap_threshold AND msgs_per_s >= min_rate`` (both configurable), NOT
  uptime/presence alone. A heartbeat-only stall fails the gap-time clause; a connected-but-slow
  feed fails the rate clause. ``healthy`` is surfaced per feed.

Reads only. No network, no order path, no secrets.

Replay seeding note. The in-band-snapshot venues (Kraken, Bybit, Kalshi) carry their snapshot
frames inside the recorded WS stream, so their processors self-seed on replay. The REST-snapshot
venues (Coinbase ``sequence``, Binance ``pu``) take their baseline from a REST snapshot that is
NOT in the recording, so we seed their processor from the first relevant recorded message (see
``_PRIME``). When a book processor raises ``needs_resnapshot`` we re-seed it from the next
message — mirroring what the live IO layer does by resyncing — so one dropped message counts as
one gap rather than cascading. Kraken's CRC32 resync is in-band, so between a checksum mismatch
and the next recorded snapshot its gap count is an upper bound (documented limitation).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import MessageProcessor, as_dict
from .binance import BinanceAggTradeProcessor, BinanceDepthProcessor
from .bitstamp import BitstampProcessor
from .bybit import BybitOrderbookProcessor, BybitPublicTradeProcessor
from .coinbase import CoinbaseProcessor
from .kalshi import KalshiProcessor
from .kraken import KrakenProcessor
from .writer import read_records

NS_PER_S = 1_000_000_000
HOUR_NS = 3600 * NS_PER_S


# --------------------------------------------------------------------------------------
# Processor map + replay seeding ("priming") for REST-snapshot venues.
# --------------------------------------------------------------------------------------
# A prime function inspects the first relevant recorded message and seeds the processor so it is
# ready to detect gaps. It returns:
#   "skip"    — not a primable message yet (e.g. a subscription ack); process it and keep waiting;
#   "consume" — primed from this message, treat it as the already-applied baseline (do not reprocess);
#   "keep"    — primed from this message; it is at the snapshot boundary, process it normally.
PrimeFn = Callable[[MessageProcessor, Any], str]


def _prime_coinbase(proc: MessageProcessor, raw: Any) -> str:
    """Coinbase seeds its ``sequence`` from the REST snapshot; here, from the first sequenced msg."""
    msg = as_dict(raw)
    if (isinstance(msg, dict) and msg.get("type") not in {"subscriptions", "error"}
            and msg.get("sequence") is not None):
        proc.apply_snapshot({"sequence": int(msg["sequence"]), "bids": [], "asks": []})
        return "consume"
    return "skip"


def _prime_binance(proc: MessageProcessor, raw: Any) -> str:
    """Binance futures seeds ``lastUpdateId`` from REST; here, from the first depthUpdate's ``u``.

    Setting ``lastUpdateId = u`` puts that first event on the inclusive ``U <= lastUpdateId <= u``
    window boundary, so it is applied (not dropped) and establishes ``prev_u`` for the ``pu`` chain.
    """
    msg = as_dict(raw)
    if isinstance(msg, dict) and "data" in msg and "stream" in msg:
        msg = msg["data"]
    if isinstance(msg, dict) and msg.get("e") == "depthUpdate" and msg.get("u") is not None:
        proc.apply_snapshot({"lastUpdateId": int(msg["u"]), "bids": [], "asks": []})
        return "keep"
    return "skip"


def make_processor(venue: str, channel: str) -> tuple[MessageProcessor | None, PrimeFn | None]:
    """Map a feed ``{venue, channel}`` to its pure processor and (optional) replay prime function.

    Returns ``(None, None)`` for venues with no processor — those feeds report coverage/rate/disk
    only, with sequence gaps marked n/a.
    """
    v, c = venue.lower(), (channel or "").lower()
    if v == "coinbase":
        return CoinbaseProcessor(channel=channel), _prime_coinbase
    if v == "kraken":
        return KrakenProcessor(channel=channel), None
    if v == "binance":
        if "aggtrade" in c:
            return BinanceAggTradeProcessor(channel=channel), None  # trade tape self-anchors
        return BinanceDepthProcessor(channel=channel), _prime_binance
    if v == "bitstamp":
        return BitstampProcessor(channel=channel), None  # pre_event_id chain self-anchors
    if v == "bybit":
        if c.startswith("publictrade"):
            return BybitPublicTradeProcessor(channel=channel), None
        return BybitOrderbookProcessor(channel=channel), None  # in-band snapshot reset
    if v == "kalshi":
        return KalshiProcessor(channel=channel), None  # in-band orderbook_snapshot
    return None, None


# Kraken resyncs its CRC32 book in-band, so between a checksum mismatch and the next recorded
# snapshot its replayed gap count is an upper bound. Every other venue re-anchors cleanly (one
# gap per drop), so its count is exact.
_UPPER_BOUND_GAP_VENUES = {"kraken"}


# --------------------------------------------------------------------------------------
# Data-frame classifier — "is this a usable book/trade/lifecycle event, or just liveness?"
#
# We strip control frames (subscription acks, heartbeats, pongs, status/error) so that a feed
# whose book stalled but whose socket still heartbeats does NOT look healthy (the F1 false-pass
# vector). The control-frame vocabulary for each venue is exactly what the processors skip.
# --------------------------------------------------------------------------------------
_KALSHI_DATA_TYPES = {
    "orderbook_snapshot", "orderbook_delta", "trade",
    "market_lifecycle", "market_lifecycle_v2", "market_lifecycle_update",
}


def _is_data_coinbase(raw: Any) -> bool:
    msg = as_dict(raw)
    if isinstance(msg, list):
        return bool(msg)  # level3 positional data row
    if not isinstance(msg, dict):
        return False
    t = msg.get("type")
    if t == "level3" and "schema" in msg:
        return False  # one-off schema object (control)
    return t not in {"subscriptions", "error", "heartbeat"}


def _is_data_bitstamp(raw: Any) -> bool:
    msg = as_dict(raw)
    return (isinstance(msg, dict)
            and msg.get("event") in {"order_created", "order_changed", "order_deleted"})


def _is_data_binance(raw: Any) -> bool:
    msg = as_dict(raw)
    if isinstance(msg, dict) and "data" in msg and "stream" in msg:
        msg = msg["data"]
    return isinstance(msg, dict) and msg.get("e") in {"depthUpdate", "aggTrade"}


def _is_data_bybit(raw: Any) -> bool:
    msg = as_dict(raw)
    if not isinstance(msg, dict) or "data" not in msg:
        return False  # subscribe/pong acks carry no "data"
    topic = str(msg.get("topic", ""))
    return topic.startswith("orderbook.") or topic.startswith("publicTrade.")


def _is_data_kraken(raw: Any) -> bool:
    msg = as_dict(raw)
    return (isinstance(msg, dict) and msg.get("channel") == "level3"
            and msg.get("type") in {"snapshot", "update"})


def _is_data_kalshi(raw: Any) -> bool:
    msg = as_dict(raw)
    return isinstance(msg, dict) and msg.get("type") in _KALSHI_DATA_TYPES


_DATA_CLASSIFIERS: dict[str, Callable[[Any], bool]] = {
    "coinbase": _is_data_coinbase,
    "bitstamp": _is_data_bitstamp,
    "binance": _is_data_binance,
    "bybit": _is_data_bybit,
    "kraken": _is_data_kraken,
    "kalshi": _is_data_kalshi,
}


def data_classifier(venue: str) -> Callable[[Any], bool] | None:
    """Return the venue's data-frame classifier, or None (caller then counts all frames)."""
    return _DATA_CLASSIFIERS.get(venue.lower())


# --------------------------------------------------------------------------------------
# Records.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageGap:
    """An interval [start_ns, end_ns) with no recorded message (longer than ``max_quiet_s``)."""

    start_ns: int
    end_ns: int

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns

    @property
    def duration_s(self) -> float:
        return self.duration_ns / NS_PER_S

    def to_dict(self) -> dict[str, Any]:
        return {"start_ns": self.start_ns, "end_ns": self.end_ns,
                "duration_s": self.duration_s}


@dataclass
class FeedHealth:
    venue: str
    channel: str
    window_start_ns: int
    window_end_ns: int
    max_quiet_s: float
    # volume / rate — all based on DATA frames only (control frames excluded when classified)
    n_messages: int               # data frames
    n_control_frames: int         # subscription acks / heartbeats / pongs / status (excluded)
    data_classified: bool         # False => venue has no classifier; n_messages counts ALL frames
    first_ts_ns: int | None
    last_ts_ns: int | None
    msgs_per_s: float
    per_hour_counts: dict[str, int]
    # disk
    n_files: int
    disk_bytes: int
    disk_bytes_per_day: dict[str, int]
    # coverage / uptime
    uptime_fraction: float
    gap_time_fraction: float
    downtime_ns: int
    coverage_gaps: list[CoverageGap]  # sorted largest-first
    # sequence gaps
    sequence_gaps_supported: bool
    n_sequence_gaps: int | None
    sequence_gap_reasons: dict[str, int] | None
    sequence_gaps_exact: bool | None  # False => upper bound (Kraken); None => n/a
    sequence_gap_events: list[dict] | None
    # health gate (ROADMAP 1.3 acceptance)
    gap_threshold: float
    min_rate: float
    healthy: bool

    @property
    def uptime_pct(self) -> float:
        return 100.0 * self.uptime_fraction

    @property
    def gap_time_pct(self) -> float:
        return 100.0 * self.gap_time_fraction

    @property
    def largest_gap_s(self) -> float:
        return self.coverage_gaps[0].duration_s if self.coverage_gaps else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "channel": self.channel,
            "window_start_ns": self.window_start_ns,
            "window_end_ns": self.window_end_ns,
            "max_quiet_s": self.max_quiet_s,
            "n_messages": self.n_messages,
            "n_control_frames": self.n_control_frames,
            "data_classified": self.data_classified,
            "first_ts_ns": self.first_ts_ns,
            "last_ts_ns": self.last_ts_ns,
            "msgs_per_s": self.msgs_per_s,
            "per_hour_counts": self.per_hour_counts,
            "n_files": self.n_files,
            "disk_bytes": self.disk_bytes,
            "disk_bytes_per_day": self.disk_bytes_per_day,
            "uptime_fraction": self.uptime_fraction,
            "gap_time_fraction": self.gap_time_fraction,
            "downtime_ns": self.downtime_ns,
            "coverage_gaps": [g.to_dict() for g in self.coverage_gaps],
            "sequence_gaps_supported": self.sequence_gaps_supported,
            "n_sequence_gaps": self.n_sequence_gaps,
            "sequence_gaps_exact": self.sequence_gaps_exact,
            "sequence_gap_reasons": self.sequence_gap_reasons,
            "sequence_gap_events": self.sequence_gap_events,
            "gap_threshold": self.gap_threshold,
            "min_rate": self.min_rate,
            "healthy": self.healthy,
        }


@dataclass
class HealthReport:
    root: str
    window_start_ns: int
    window_end_ns: int
    max_quiet_s: float
    feeds: list[FeedHealth]

    @property
    def total_disk_bytes(self) -> int:
        return sum(f.disk_bytes for f in self.feeds)

    @property
    def total_messages(self) -> int:
        return sum(f.n_messages for f in self.feeds)

    @property
    def total_sequence_gaps(self) -> int:
        return sum(f.n_sequence_gaps or 0 for f in self.feeds)

    @property
    def min_uptime_fraction(self) -> float:
        return min((f.uptime_fraction for f in self.feeds), default=1.0)

    @property
    def max_gap_time_fraction(self) -> float:
        return max((f.gap_time_fraction for f in self.feeds), default=0.0)

    @property
    def unhealthy_feeds(self) -> list[str]:
        return [f"{f.venue}/{f.channel}" for f in self.feeds if not f.healthy]

    @property
    def all_healthy(self) -> bool:
        return all(f.healthy for f in self.feeds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "window_start_ns": self.window_start_ns,
            "window_end_ns": self.window_end_ns,
            "max_quiet_s": self.max_quiet_s,
            "feeds": [f.to_dict() for f in self.feeds],
            "summary": {
                "n_feeds": len(self.feeds),
                "total_disk_bytes": self.total_disk_bytes,
                "total_messages": self.total_messages,
                "total_sequence_gaps": self.total_sequence_gaps,
                "min_uptime_fraction": self.min_uptime_fraction,
                "max_gap_time_fraction": self.max_gap_time_fraction,
                "all_healthy": self.all_healthy,
                "unhealthy_feeds": self.unhealthy_feeds,
            },
        }


# --------------------------------------------------------------------------------------
# Filesystem discovery.
# --------------------------------------------------------------------------------------
def discover_feeds(root: str | Path) -> list[tuple[str, str]]:
    """Return sorted ``(venue, channel)`` pairs found under ``root`` (dirs with ``date=*`` data)."""
    root = Path(root)
    feeds: set[tuple[str, str]] = set()
    for datedir in root.glob("*/*/date=*"):
        if not datedir.is_dir():
            continue
        rel = datedir.relative_to(root).parts
        if len(rel) >= 3:
            feeds.add((rel[0], rel[1]))
    return sorted(feeds)


def _hour_start_ns(date: str, hour: str) -> int:
    dt = datetime.strptime(f"{date} {hour}", "%Y-%m-%d %H").replace(tzinfo=UTC)
    return int(dt.timestamp()) * NS_PER_S


def feed_files(root: str | Path, venue: str, channel: str,
               start_ns: int, end_ns: int) -> list[Path]:
    """Files for a feed whose hour overlaps ``[start_ns, end_ns)``, sorted by time."""
    base = Path(root) / venue / channel
    out: list[tuple[int, str, Path]] = []
    for datedir in base.glob("date=*"):
        date = datedir.name[len("date="):]
        for f in datedir.glob("*.jsonl.zst"):
            hour = f.name[:2]
            try:
                hs = _hour_start_ns(date, hour)
            except ValueError:
                continue
            if hs < end_ns and hs + HOUR_NS > start_ns:
                out.append((hs, f.name, f))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


def _hour_key(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // NS_PER_S, UTC).strftime("%Y-%m-%dT%H")


def _day_key(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // NS_PER_S, UTC).strftime("%Y-%m-%d")


def data_time_bounds(root: str | Path) -> tuple[int, int] | None:
    """Scan every feed and return ``(min_ts_ns, max_ts_ns + 1)`` across all records, or None."""
    root = Path(root)
    lo: int | None = None
    hi: int | None = None
    for f in root.glob("*/*/date=*/*.jsonl.zst"):
        for rec in read_records(f):
            ts = rec.get("recv_ts_ns")
            if ts is None:
                continue
            lo = ts if lo is None or ts < lo else lo
            hi = ts if hi is None or ts > hi else hi
    if lo is None or hi is None:
        return None
    return lo, hi + 1


# --------------------------------------------------------------------------------------
# Per-feed computation.
# --------------------------------------------------------------------------------------
def _coverage(timestamps: list[int], start_ns: int, end_ns: int,
              max_quiet_ns: int) -> tuple[int, list[CoverageGap]]:
    """Downtime (ns) and coverage gaps: any no-message interval longer than ``max_quiet_ns``.

    Considers the lead-in (window start -> first message), each inter-message interval, and the
    tail (last message -> window end). The full duration of each over-threshold interval is
    downtime.
    """
    downtime = 0
    gaps: list[CoverageGap] = []
    prev = start_ns
    for t in list(timestamps) + [end_ns]:
        d = t - prev
        if d > max_quiet_ns:
            downtime += d
            gaps.append(CoverageGap(prev, t))
        prev = t
    gaps.sort(key=lambda g: g.duration_ns, reverse=True)
    return downtime, gaps


def compute_feed_health(root: str | Path, venue: str, channel: str,
                        start_ns: int, end_ns: int, max_quiet_s: float = 5.0,
                        gap_threshold: float = 0.001, min_rate: float = 1.0) -> FeedHealth:
    """Compute a :class:`FeedHealth` for one feed over ``[start_ns, end_ns)``.

    Coverage and rate are measured over DATA frames only (control frames — subscription acks,
    heartbeats, pongs, status/error — are excluded via the venue classifier). ``healthy`` is the
    ROADMAP 1.3 conjunction ``gap_time_fraction < gap_threshold AND msgs_per_s >= min_rate``.
    """
    if end_ns <= start_ns:
        raise ValueError(f"empty window: start={start_ns} end={end_ns}")
    root = Path(root)
    files = feed_files(root, venue, channel, start_ns, end_ns)

    disk_bytes = 0
    disk_per_day: dict[str, int] = defaultdict(int)
    for f in files:
        size = f.stat().st_size
        disk_bytes += size
        date = f.parent.name[len("date="):]
        disk_per_day[date] += size

    proc, prime = make_processor(venue, channel)
    supported = proc is not None
    primed = supported and prime is None
    classify = data_classifier(venue)
    data_classified = classify is not None

    timestamps: list[int] = []  # DATA-frame timestamps only
    per_hour: dict[str, int] = defaultdict(int)
    n_control = 0

    for f in files:
        for rec in read_records(f):
            ts = rec.get("recv_ts_ns")
            if ts is None or ts < start_ns or ts >= end_ns:
                continue
            raw = rec.get("raw")
            # Metrics: data frames only (heartbeats et al. must NOT count as "up" or as rate).
            is_data = classify(raw) if classify is not None else True
            if is_data:
                timestamps.append(ts)
                per_hour[_hour_key(ts)] += 1
            else:
                n_control += 1
            # Sequence gaps: the processor still sees EVERY frame (it ignores control ones itself).
            if not supported:
                continue
            if not primed:
                action = prime(proc, raw)  # type: ignore[misc]
                if action == "skip":
                    proc.process(raw, ts)
                    continue
                primed = True
                if action == "consume":
                    continue
            proc.process(raw, ts)
            if proc.needs_resnapshot and prime is not None:
                # Mirror the live IO layer: a resync re-anchors the processor. Re-seed from the
                # next relevant message so one dropped message is one gap, not a cascade.
                proc.clear_resnapshot()
                primed = False

    timestamps.sort()
    n = len(timestamps)
    first_ts = timestamps[0] if n else None
    last_ts = timestamps[-1] if n else None
    span_ns = (last_ts - first_ts) if n >= 2 else 0
    # Fencepost rate (F3): n / data-span. The span is the data extent, not the window; the
    # healthy gate pairs this with gap_time so a single burst cannot pass as window-wide throughput.
    msgs_per_s = (n / (span_ns / NS_PER_S)) if span_ns > 0 else 0.0

    max_quiet_ns = int(max_quiet_s * NS_PER_S)
    downtime_ns, gaps = _coverage(timestamps, start_ns, end_ns, max_quiet_ns)
    window_ns = end_ns - start_ns
    gap_time_fraction = downtime_ns / window_ns
    uptime_fraction = 1.0 - gap_time_fraction

    reasons: dict[str, int] | None = None
    n_seq_gaps: int | None = None
    seq_exact: bool | None = None
    seq_events: list[dict] | None = None
    if supported:
        reasons = defaultdict(int)
        seq_events = []
        for g in proc.gaps:  # type: ignore[union-attr]
            reasons[g.reason] += 1
            seq_events.append({"reason": g.reason, "expected": g.expected,
                               "got": g.got, "recv_ts_ns": g.recv_ts_ns})
        reasons = dict(reasons)
        n_seq_gaps = len(proc.gaps)  # type: ignore[union-attr]
        seq_exact = venue.lower() not in _UPPER_BOUND_GAP_VENUES

    healthy = (gap_time_fraction < gap_threshold) and (msgs_per_s >= min_rate)

    return FeedHealth(
        venue=venue,
        channel=channel,
        window_start_ns=start_ns,
        window_end_ns=end_ns,
        max_quiet_s=max_quiet_s,
        n_messages=n,
        n_control_frames=n_control,
        data_classified=data_classified,
        first_ts_ns=first_ts,
        last_ts_ns=last_ts,
        msgs_per_s=msgs_per_s,
        per_hour_counts=dict(sorted(per_hour.items())),
        n_files=len(files),
        disk_bytes=disk_bytes,
        disk_bytes_per_day=dict(sorted(disk_per_day.items())),
        uptime_fraction=uptime_fraction,
        gap_time_fraction=gap_time_fraction,
        downtime_ns=downtime_ns,
        coverage_gaps=gaps,
        sequence_gaps_supported=supported,
        n_sequence_gaps=n_seq_gaps,
        sequence_gap_reasons=reasons,
        sequence_gaps_exact=seq_exact,
        sequence_gap_events=seq_events,
        gap_threshold=gap_threshold,
        min_rate=min_rate,
        healthy=healthy,
    )


def compute_health(root: str | Path, start_ns: int | None = None, end_ns: int | None = None,
                   max_quiet_s: float = 5.0, gap_threshold: float = 0.001,
                   min_rate: float = 1.0) -> HealthReport:
    """Compute a :class:`HealthReport` across every feed under ``root``.

    If ``start_ns``/``end_ns`` are omitted they default to the min/max ``recv_ts_ns`` in the data.
    """
    root = Path(root)
    if start_ns is None or end_ns is None:
        bounds = data_time_bounds(root)
        if bounds is None:
            start_ns = start_ns if start_ns is not None else 0
            end_ns = end_ns if end_ns is not None else (start_ns + 1)
        else:
            lo, hi = bounds
            start_ns = start_ns if start_ns is not None else lo
            end_ns = end_ns if end_ns is not None else hi
    feeds = [
        compute_feed_health(root, venue, channel, start_ns, end_ns, max_quiet_s,
                            gap_threshold=gap_threshold, min_rate=min_rate)
        for venue, channel in discover_feeds(root)
    ]
    return HealthReport(root=str(root), window_start_ns=start_ns, window_end_ns=end_ns,
                        max_quiet_s=max_quiet_s, feeds=feeds)


# --------------------------------------------------------------------------------------
# CLI / rendering.
# --------------------------------------------------------------------------------------
def human_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.0f}{unit}" if unit == "B" else f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}TiB"  # pragma: no cover


def render_table(report: HealthReport) -> str:
    rows = [("VENUE", "CHANNEL", "HEALTHY", "UPTIME%", "MSGS/S", "SEQ-GAPS", "GAP-TIME%", "DISK")]
    for f in report.feeds:
        if not f.sequence_gaps_supported:
            seq = "n/a"
        else:
            # Trailing '~' flags an upper-bound count (Kraken in-band CRC resync, F4).
            seq = f"{f.n_sequence_gaps}" + ("" if f.sequence_gaps_exact else "~")
        rows.append((
            f.venue,
            f.channel,
            "yes" if f.healthy else "NO",
            f"{f.uptime_pct:.3f}",
            f"{f.msgs_per_s:.4g}",
            seq,
            f"{f.gap_time_pct:.3f}",
            human_bytes(f.disk_bytes),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for ri, r in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
        if ri == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    w0 = datetime.fromtimestamp(report.window_start_ns // NS_PER_S, UTC)
    w1 = datetime.fromtimestamp(report.window_end_ns // NS_PER_S, UTC)
    lines.append("")
    unhealthy = report.unhealthy_feeds
    verdict = "ALL HEALTHY" if report.all_healthy else f"UNHEALTHY: {', '.join(unhealthy)}"
    lines.append(
        f"OVERALL  feeds={len(report.feeds)}  "
        f"window=[{w0:%Y-%m-%d %H:%M:%SZ} .. {w1:%Y-%m-%d %H:%M:%SZ}]  "
        f"msgs={report.total_messages}  disk={human_bytes(report.total_disk_bytes)}  "
        f"seq_gaps={report.total_sequence_gaps}  "
        f"min_uptime={100 * report.min_uptime_fraction:.3f}%  "
        f"max_gap_time={100 * report.max_gap_time_fraction:.3f}%  "
        f"[{verdict}]"
    )
    return "\n".join(lines)


def _parse_ts(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)  # epoch nanoseconds
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * NS_PER_S)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Recorder health report (uptime, rates, gaps, disk).")
    p.add_argument("--root", default="data/raw", help="data root (default: data/raw)")
    p.add_argument("--start", default=None,
                   help="window start: epoch ns or ISO-8601 (default: earliest data)")
    p.add_argument("--end", default=None,
                   help="window end: epoch ns or ISO-8601 (default: latest data)")
    p.add_argument("--max-quiet-s", type=float, default=5.0,
                   help="no-DATA interval that counts as downtime (default: 5s)")
    p.add_argument("--gap-threshold", type=float, default=0.001,
                   help="max gap_time_fraction for a feed to be healthy (default: 0.001 = 0.1%%)")
    p.add_argument("--min-rate", type=float, default=1.0,
                   help="min data msgs/sec for a feed to be healthy (default: 1.0)")
    p.add_argument("--json", action="store_true", help="emit JSON (for alerting) instead of a table")
    args = p.parse_args(argv)

    report = compute_health(args.root, _parse_ts(args.start), _parse_ts(args.end),
                            max_quiet_s=args.max_quiet_s, gap_threshold=args.gap_threshold,
                            min_rate=args.min_rate)
    if args.json:
        print(json.dumps(report.to_dict(), separators=(",", ":")))
    else:
        print(render_table(report))
    # Non-zero exit when any feed is unhealthy, so a cron/systemd alarm can fire on it.
    return 0 if report.all_healthy else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
