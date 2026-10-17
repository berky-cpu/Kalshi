"""Disk-usage alarm for the data partition (ROADMAP 1.3 deploy).

The recorders write 24/7 to ``data/raw`` on an 80+ GB VPS disk; a full disk silently drops data.
This module reports the usage of the partition holding a given path and exits non-zero on a
warn/critical breach so a systemd timer (or cron) can alarm. The threshold decision is a PURE
function (:func:`evaluate`) so it is unit-tested without touching a real filesystem.

Breach is on EITHER a percent-used ceiling OR a minimum-free-space floor (whichever is worse),
because a large disk can still have a dangerous absolute shortfall before the percentage trips.

Exit codes: 0 = ok, 1 = warn, 2 = critical. Usage/IO errors exit 2 (treat "can't tell" as bad).
Nothing here reads or logs credentials.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass

GiB = 1024 ** 3


@dataclass(frozen=True)
class DiskStatus:
    level: str  # "ok" | "warn" | "critical"
    pct_used: float  # 0..1
    free_bytes: int
    total_bytes: int
    reason: str

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "warn": 1, "critical": 2}[self.level]


def evaluate(
    total_bytes: int,
    used_bytes: int,
    free_bytes: int,
    *,
    warn_pct: float = 0.80,
    crit_pct: float = 0.90,
    min_free_gib: float = 5.0,
) -> DiskStatus:
    """Pure threshold logic. Critical if pct_used >= crit_pct OR free < min_free_gib; else warn if
    pct_used >= warn_pct; else ok. Returns the worst matching level with a human reason."""
    pct = (used_bytes / total_bytes) if total_bytes > 0 else 1.0
    free_gib = free_bytes / GiB
    min_free_bytes = int(min_free_gib * GiB)

    if pct >= crit_pct or free_bytes < min_free_bytes:
        reason = (
            f"CRITICAL: {pct:.1%} used ({free_gib:.1f} GiB free); "
            f"limits crit>={crit_pct:.0%} or free<{min_free_gib:g} GiB"
        )
        return DiskStatus("critical", pct, free_bytes, total_bytes, reason)
    if pct >= warn_pct:
        reason = f"WARN: {pct:.1%} used ({free_gib:.1f} GiB free); limit warn>={warn_pct:.0%}"
        return DiskStatus("warn", pct, free_bytes, total_bytes, reason)
    return DiskStatus("ok", pct, free_bytes, total_bytes,
                      f"OK: {pct:.1%} used ({free_gib:.1f} GiB free)")


def check_path(path: str, **kw: float) -> DiskStatus:
    """Evaluate the partition holding ``path`` via ``shutil.disk_usage``."""
    usage = shutil.disk_usage(path)
    return evaluate(usage.total, usage.used, usage.free, **kw)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m kbtc.recorders.disk_alarm",
        description="Alarm on data-partition disk usage (exit 1=warn, 2=critical).",
    )
    p.add_argument("--path", default="data", help="path on the partition to check (default: data)")
    p.add_argument("--warn-pct", type=float, default=0.80, help="warn at this fraction used")
    p.add_argument("--crit-pct", type=float, default=0.90, help="critical at this fraction used")
    p.add_argument("--min-free-gib", type=float, default=5.0,
                   help="critical if free space drops below this many GiB")
    args = p.parse_args(argv)

    try:
        status = check_path(args.path, warn_pct=args.warn_pct, crit_pct=args.crit_pct,
                            min_free_gib=args.min_free_gib)
    except OSError as exc:
        print(f"disk-alarm: cannot stat {args.path!r}: {exc}", file=sys.stderr)
        return 2
    stream = sys.stderr if status.level != "ok" else sys.stdout
    print(f"disk-alarm[{args.path}]: {status.reason}", file=stream)
    return status.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
