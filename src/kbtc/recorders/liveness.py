"""Feed-liveness backstop (failure-alerting #3b): the OUT-OF-PROCESS catch-all that fires if the
in-process data-staleness watchdog (``base.DataStalenessMonitor``) fails to recover a stalled feed.

WHY NOT FILE MTIME: the recorder writes EVERY raw frame — including heartbeats — to disk, so a
"connected but only heartbeats" stall (the 2026-10-18 incident) keeps the newest ``*.jsonl.zst``
file growing and its mtime fresh. File age would therefore MISS the exact failure we care about. So
this checks the age of the newest **DATA** frame per feed, classified with the SAME
``health.data_classifier`` the watchdog and health report use (heartbeats/acks/control do NOT
count). It reads only the newest file(s) per feed (data is append-ordered by ``recv_ts_ns``).

NO FALSE ALARMS: thresholds are set ABOVE the watchdog timeouts (crypto 180 s > 120 s; kalshi
420 s > 300 s), so this only fires if the self-heal did NOT recover. It gates STRICTLY on data-frame
age — never on the health report's "late-start" flag — so a routine feed restart cannot page. The
Kalshi ``settlement`` dataset is EXCLUDED (it is a 5-minute batch timer, not a stream). A feed with
no recorded files at all is skipped (not enabled on this box), never alarmed.

READS ONLY. The alert is POSTed by ``deploy/alert.sh`` (ntfy), whose ``KBTC_ALERT_URL`` comes from
the environment only and is never read or logged here.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .health import data_classifier
from .writer import read_records

logger = logging.getLogger("kbtc.recorders.liveness")
logger.addHandler(logging.NullHandler())

now_ns = time.time_ns

# Per-feed staleness thresholds (seconds). Set ABOVE the in-process watchdog timeouts so liveness
# fires only when the watchdog itself failed to restore data.
CRYPTO_STALE_S = 180.0   # watchdog 120 s
KALSHI_STALE_S = 420.0   # watchdog 300 s
# Datasets that are NOT continuous streams — excluded from liveness (batch timers, etc.).
EXCLUDE: set[tuple[str, str]] = {("kalshi", "settlement")}

Notifier = Callable[[str, str], None]  # (subject, body) -> None


def threshold_for(venue: str, channel: str) -> float | None:
    """Per-feed staleness threshold in seconds, or ``None`` if the dataset is excluded."""
    if (venue, channel) in EXCLUDE:
        return None
    if venue == "kalshi":
        return KALSHI_STALE_S
    return CRYPTO_STALE_S


@dataclass(frozen=True)
class FeedLiveness:
    venue: str
    channel: str
    last_data_ns: int | None   # recv_ts of the newest DATA frame (None if none found)
    age_s: float | None        # seconds since that data frame (or since silence began if none)
    threshold_s: float
    stale: bool
    detail: str

    @property
    def feed(self) -> str:
        return f"{self.venue}/{self.channel}"


def classify(
    venue: str, channel: str, last_data_ns: int | None, now_ns_val: int,
    *, silence_since_ns: int | None = None,
) -> FeedLiveness | None:
    """Pure staleness decision. Returns ``None`` for an excluded dataset.

    ``last_data_ns`` is the newest DATA-frame recv_ts (``None`` if the newest file(s) held no data).
    When there is no data, ``silence_since_ns`` (the earliest record seen, i.e. when the no-data
    span began) is used as the age baseline so a FRESH feed whose current file only has a few
    seconds of (non-data) frames is NOT flagged — only one silent for longer than the threshold is.
    """
    th = threshold_for(venue, channel)
    if th is None:
        return None
    if last_data_ns is not None:
        age_s = (now_ns_val - last_data_ns) / 1e9
        return FeedLiveness(
            venue, channel, last_data_ns, age_s, th, age_s > th,
            f"newest DATA frame {age_s:.0f}s old (threshold {th:.0f}s)",
        )
    # No DATA frame in the newest file(s): silent since ``silence_since_ns`` (or now if unknown).
    baseline = silence_since_ns if silence_since_ns is not None else now_ns_val
    age_s = (now_ns_val - baseline) / 1e9
    return FeedLiveness(
        venue, channel, None, age_s, th, age_s > th,
        f"no DATA frame; silent for {age_s:.0f}s (threshold {th:.0f}s)",
    )


def _data_files_newest_first(channel_dir: Path) -> list[Path]:
    """Recorded files newest-first. Names are ``date=YYYY-MM-DD/HH_<ns>.jsonl.zst`` — zero-padded
    and ns-suffixed, so a lexical sort of the relative path is chronological."""
    files = list(channel_dir.glob("date=*/*.jsonl.zst"))
    return sorted(files, key=lambda p: (p.parent.name, p.name), reverse=True)


def _scan_file(path: Path, classifier: Callable[[Any], bool] | None) -> tuple[int | None, int | None]:
    """Return ``(last_data_ns, first_record_ns)`` for one file. ``last_data_ns`` is the max
    ``recv_ts_ns`` over DATA frames (records are append-ordered, so the last match is the max).
    ``classifier is None`` ⇒ every frame counts (health's fallback for an unclassified venue)."""
    last_data: int | None = None
    first_rec: int | None = None
    for rec in read_records(path):
        ts = rec.get("recv_ts_ns")
        if ts is None:
            continue
        if first_rec is None:
            first_rec = ts
        raw = rec.get("raw")
        if classifier is None or classifier(raw):
            last_data = ts
    return last_data, first_rec


def scan_feed(
    venue: str, channel: str, channel_dir: Path, now_ns_val: int, *, max_files: int = 2,
) -> FeedLiveness | None:
    """Liveness for one feed. Reads the newest file (and up to ``max_files`` back only if the newest
    has no data — the hour-boundary edge). Returns ``None`` if excluded or if the feed has no files
    at all (not enabled on this box)."""
    if threshold_for(venue, channel) is None:
        return None
    files = _data_files_newest_first(channel_dir)
    if not files:
        return None  # nothing recorded here — do not alarm a feed that isn't running on this box
    classifier = data_classifier(venue)
    last_data: int | None = None
    earliest: int | None = None
    for f in files[:max_files]:
        ld, fr = _scan_file(f, classifier)
        if fr is not None and (earliest is None or fr < earliest):
            earliest = fr
        if ld is not None:
            last_data = ld
            break  # newest file with data holds the most recent data frame
    return classify(venue, channel, last_data, now_ns_val, silence_since_ns=earliest)


def scan_root(root: str | Path, now_ns_val: int) -> list[FeedLiveness]:
    """Liveness for every ``{root}/{venue}/{channel}`` feed present (excluded datasets skipped)."""
    root = Path(root)
    out: list[FeedLiveness] = []
    if not root.is_dir():
        return out
    for venue_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for channel_dir in sorted(p for p in venue_dir.iterdir() if p.is_dir()):
            res = scan_feed(venue_dir.name, channel_dir.name, channel_dir, now_ns_val)
            if res is not None:
                out.append(res)
    return out


# --------------------------------------------------------------------------------------
# Fast dashboard status (FIX B) — a few seconds regardless of total data size.
#
# ``kbtc-status`` runs under ``watch -n 30``. Calling the full health replay (gap/CRC validation
# over the window) was minutes on a 5.6 GB tree, so ticks stacked and hammered the box. The
# dashboard instead uses this: per feed read ONE newest file, report the newest-DATA-frame age (same
# health.data_classifier as the watchdog/liveness — so a heartbeat-only stall is NOT masked the way
# file mtime would be) plus a recent msgs/s estimate from that file's trailing window. No alerts.
# --------------------------------------------------------------------------------------
RATE_WINDOW_S = 60.0  # recent msgs/s is measured over the newest file's trailing window


@dataclass(frozen=True)
class FeedStatus:
    venue: str
    channel: str
    last_data_ns: int | None
    age_s: float | None
    data_per_s: float          # recent DATA msgs/s estimate (trailing RATE_WINDOW_S)
    threshold_s: float | None  # staleness threshold, or None for an excluded/batch dataset
    stale: bool

    @property
    def feed(self) -> str:
        return f"{self.venue}/{self.channel}"


def feed_status(
    venue: str, channel: str, channel_dir: Path, now_ns_val: int,
    *, rate_window_s: float = RATE_WINDOW_S, max_files: int = 2,
) -> FeedStatus | None:
    """Fast per-feed status from ONE newest file (two only at the hour boundary). Returns ``None``
    if the feed has no recorded files. ``threshold_s`` is ``None`` for excluded/batch datasets
    (e.g. ``kalshi/settlement``) — shown informationally but never marked stale."""
    files = _data_files_newest_first(channel_dir)
    if not files:
        return None
    classifier = data_classifier(venue)
    window_ns = int(rate_window_s * 1e9)
    recent: deque[int] = deque()  # DATA timestamps within the trailing window of what we've seen
    last_data: int | None = None
    earliest: int | None = None
    for f in files[:max_files]:
        had_data = False
        for rec in read_records(f):
            ts = rec.get("recv_ts_ns")
            if ts is None:
                continue
            if earliest is None:
                earliest = ts
            raw = rec.get("raw")
            if classifier is None or classifier(raw):
                last_data = ts
                had_data = True
                recent.append(ts)
                while recent and ts - recent[0] > window_ns:
                    recent.popleft()
        if had_data:
            break  # newest file with data holds the most recent data + its trailing window
    if len(recent) >= 2 and (recent[-1] - recent[0]) > 0:
        data_per_s = len(recent) / ((recent[-1] - recent[0]) / 1e9)
    else:
        data_per_s = 0.0
    th = threshold_for(venue, channel)
    if last_data is not None:
        age_s = (now_ns_val - last_data) / 1e9
    elif earliest is not None:
        age_s = (now_ns_val - earliest) / 1e9  # no data: silent since first (non-data) record
    else:
        age_s = None
    stale = th is not None and age_s is not None and age_s > th
    return FeedStatus(venue, channel, last_data, age_s, data_per_s, th, stale)


def scan_status(root: str | Path, now_ns_val: int) -> list[FeedStatus]:
    """Fast status for every feed present under ``root`` (including excluded/batch datasets, shown
    informationally)."""
    root = Path(root)
    out: list[FeedStatus] = []
    if not root.is_dir():
        return out
    for venue_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for channel_dir in sorted(p for p in venue_dir.iterdir() if p.is_dir()):
            st = feed_status(venue_dir.name, channel_dir.name, channel_dir, now_ns_val)
            if st is not None:
                out.append(st)
    return out


def render_status(statuses: list[FeedStatus], now_ns_val: int) -> str:
    """Compact dashboard table: feed, newest-DATA-frame age, recent msgs/s, live/STALE verdict."""
    rows = [("FEED", "DATA-AGE", "MSGS/S", "STATUS")]
    for s in sorted(statuses, key=lambda x: x.feed):
        if s.age_s is None:
            age = "no-data"
        else:
            age = f"{s.age_s:.0f}s"
        if s.threshold_s is None:
            status = "batch"        # excluded dataset (e.g. settlement) — informational only
        elif s.stale:
            status = "STALE"
        else:
            status = "live"
        rows.append((s.feed, age, f"{s.data_per_s:.4g}", status))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for ri, r in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
        if ri == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    ts = datetime.fromtimestamp(now_ns_val / 1e9, tz=UTC)
    stale_feeds = [s.feed for s in statuses if s.stale]
    verdict = "all live" if not stale_feeds else f"STALE: {', '.join(stale_feeds)}"
    lines.append(f"feed data-age @ {ts:%H:%M:%SZ}  [{verdict}]")
    return "\n".join(lines)


def _default_notifier(alert_script: str) -> Notifier:
    """Notifier that shells out to ``deploy/alert.sh`` (which reads ``KBTC_ALERT_URL`` from env).

    ``alert.sh`` is a no-op when the URL is unset, so this is safe on an unconfigured box. The URL
    is never passed here and never logged.
    """
    def notify(subject: str, body: str) -> None:
        try:
            subprocess.run([alert_script, subject, body], check=False, timeout=30)
        except Exception as exc:  # noqa: BLE001 - alerting must never crash the check
            logger.warning("liveness: alert invocation failed (%s)", exc,
                           extra={"event": "liveness_alert_error"})
    return notify


def main(
    argv: list[str] | None = None,
    *,
    clock_ns: Callable[[], int] = now_ns,
    notifier: Notifier | None = None,
) -> int:
    """Scan feeds and alert (via ``alert.sh``) on each genuinely-stale one. Exits 0 (the alert is
    the signal; the unit has no ``OnFailure`` so there is no double-page). ``clock_ns``/``notifier``
    are injectable for tests (no filesystem clock, no subprocess)."""
    p = argparse.ArgumentParser(
        prog="python -m kbtc.recorders.liveness",
        description="Feed-liveness backstop: alert if a feed's newest DATA frame is too old.",
    )
    p.add_argument("--root", default="data/raw", help="data root (default: data/raw)")
    p.add_argument("--alert-script", default="deploy/alert.sh",
                   help="path to the ntfy notifier (default: deploy/alert.sh)")
    p.add_argument("--print-status", action="store_true",
                   help="fast dashboard view (per-feed data-age + recent msgs/s); prints a table "
                        "and NEVER alerts — for kbtc-status under watch")
    args = p.parse_args(argv)

    if args.print_status:
        # Dashboard mode: fast, read-only, no ntfy POST. Always exits 0.
        print(render_status(scan_status(args.root, clock_ns()), clock_ns()))
        return 0

    notify = notifier or _default_notifier(args.alert_script)
    results = scan_root(args.root, clock_ns())
    stale = [r for r in results if r.stale]
    for r in stale:
        logger.warning("LIVENESS_STALE feed=%s %s", r.feed, r.detail,
                       extra={"event": "liveness_stale", "feed": r.feed,
                              "age_s": r.age_s, "threshold_s": r.threshold_s})
        notify(f"kbtc feed STALE: {r.feed}", f"{r.feed}: {r.detail}")
    if not stale:
        logger.info("liveness: all %d feeds fresh", len(results),
                    extra={"event": "liveness_ok", "n_feeds": len(results)})
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
