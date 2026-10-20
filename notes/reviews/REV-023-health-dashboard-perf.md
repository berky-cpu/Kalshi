# REV-023 — Red-team review: health-report windowing + fast dashboard (monitoring perf)

- **Date:** 2026-10-20
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/health.py` (`earliest_data_ns`/`latest_data_ns`/`_ts_bounds_in_file`,
  `compute_health` bounds resolution), `src/kbtc/recorders/liveness.py` (`feed_status`/`scan_status`/
  `render_status`, `--print-status`), `deploy/status.sh`, `tests/test_health.py`, `tests/test_liveness.py`.
- **Verdict:** **PASS — ship.** `uv run pytest -q` → 330 passed (+7). Reads-only; no order path; no new dep
  (stdlib); `guard.py`/`.claude`/`recorders/writer.py`/`execution` untouched.

## Problem (production)
At 5.6 GB of accumulated raw data the health report took **minutes**, and the `kbtc-status` dashboard under
`watch -n 30` stacked **6 concurrent scans**, pinning the box so the health table hung/looked "unhealthy"
while all feeds were actually fine. Root cause: `data_time_bounds` decompressed EVERY record of EVERY file
to find max-ts when `--start` was given without `--end` (the health unit's invocation).

## FIX A — windowed bounds (SPEED-ONLY, metrics identical) — CONFIRMED
`compute_feed_health` and `feed_files` are NOT in the diff — per-feed metrics (uptime%, msgs/s, seq-gaps,
gap-time, healthy flag, CRC validation) are byte-identical for a given `[start,end)`. Only BOUNDS resolution
changed: `earliest_data_ns`/`latest_data_ns` read oldest/newest ONE file per feed (fall through a
heartbeat-only boundary file to the next). `--start`-no-`--end` sets `end = latest_data_ns+1` = the old
`data_time_bounds` hi+1 (same value). The reviewer built a multi-day mixed heartbeat/data tree and compared
the new bounds against a brute-force full scan — **exact match** (earliest=global min, latest=global max,
`data_time_bounds`=(min,max+1)); auto-window (no --start/--end) still spans all data; late-started feed still
judged from its own first data (compute_feed_health unchanged). The authoritative gap/CRC validator (the
284k/284k Kraken path) is unaffected — just faster over a window.

## FIX B — fast dashboard — CONFIRMED
`kbtc-status` now calls `liveness --print-status` (reads ONE newest file/feed) instead of the full replay →
seconds, size-independent, so `watch -n 30` can't stack. It reports **newest-DATA-frame age** (via
`health.data_classifier`, NOT file mtime) so a heartbeat-only stall shows STALE (the whole point); msgs/s from
the trailing-60s DATA window; settlement labeled `batch`, never mis-flagged. `--print-status` NEVER constructs
or calls the notifier and always exits 0 — the dashboard can't false-page, and the real liveness-alerter path
(scan_root + notify) is untouched.

## Timing (synthetic)
Old full-tree `data_time_bounds` 1.70s on 13 MB → ~12 min extrapolated to 5.6 GB; new windowed
`compute_health(--start -4min)` 0.082s; `kbtc-status` fast path 0.065s (7 feeds). Size-independent (≈1
file/feed), so a few seconds on the real box.

## Findings (all LOW/cosmetic, none block)
- **F1 (applied):** `earliest_data_ns`/`latest_data_ns` bound ANY record's recv_ts (heartbeats included),
  matching the old `data_time_bounds` window semantics — NOT DATA-classified. Added a docstring note so it
  isn't misread as DATA-frame bounds.
- F2: boundary read assumes recv_ts monotonic across a feed's files (true for an append-ordered recorder;
  only a backward NTP step could violate, sub-second — the old scan was equally affected). Negligible.
- F3: a corrupt/truncated boundary `.zst` would propagate from `read_records` — identical exposure to the
  old full-scan (no regression).
- F4: `render_status` reads the clock twice (µs header skew). Cosmetic.
