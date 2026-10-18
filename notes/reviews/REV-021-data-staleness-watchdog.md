# REV-021 — Red-team review: data-staleness self-heal watchdog (run_recorder)

- **Date:** 2026-10-18
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/base.py` (`DataStalenessMonitor`, `_watchdog`,
  `IoConfig.stale_timeout_s`, observe-on-data wiring), `src/kbtc/recorders/run.py`
  (`KALSHI_STALE_TIMEOUT_S=300`), `tests/test_recorders_watchdog.py`, `tests/test_run.py`.
- **Verdict:** **PASS — ship.** `uv run pytest -q` → 310 passed (was 300; +10). Reads-only (the watchdog
  only calls `ws.close()`); no order path; stdlib only (no new dep); `guard.py`/`.claude/`/`writer.py`/
  `execution/` untouched.

## Why
The 2026-10-18 incident: after an unattended-upgrades restart, Kraken/Kalshi stayed WS-connected but
delivered only heartbeats/acks — no order-book data — for ~11h. The socket never dropped, so the existing
disconnect-based reconnect never fired and nothing self-healed. The watchdog forces a reconnect when no
DATA frame has arrived for `stale_timeout_s` while connected; a reconnect re-runs header_factory/
subscribe_factory/on_connect (Kraken re-token, Kalshi re-discover + re-subscribe the live market). Recovery
in minutes, not 11h.

## Design
Pure `DataStalenessMonitor`: `observe(raw, recv)` advances the data-clock ONLY on DATA frames (via
`health.data_classifier(venue)` — the SAME data-vs-control definition the health report uses);
heartbeats/acks/control do NOT. `is_stale(now)` true when `now − baseline > timeout_s`, baseline = last
data frame or connect-time if none yet. `run_recorder` starts `_watchdog` alongside `_heartbeat` (same
inner try, cancelled in the same finally); on stale → `WARNING event=data_stale` + `ws.close()` → outer
loop reconnects. `IoConfig.stale_timeout_s` default 120s; Kalshi override 300s (local import of
`data_classifier` avoids the base↔health cycle).

## Verified (behaviorally re-derived)
1. **No startup false-trip:** fresh connection gets the FULL timeout of grace (baseline=connect-time until
   first data; @299s not stale, @300s stale; data@250 → trips at 550). Any data frame resets the clock.
2. **Classifier strict + non-None for all 7 venues** (no silent-disable): heartbeat/ok/subscribed/error
   frames do NOT reset the clock (the exact property whose absence caused the incident); kraken data only
   on channel=level3 type∈{snapshot,update}, kalshi only on orderbook_snapshot/delta/trade.
3. **Task lifecycle:** `wd` cancelled in finally on every disconnect (no leak); `ws.close()` unblocks the
   read loop; both clean-exit and ConnectionClosed paths reconnect without crash/loop; `on_connect` resets
   baseline each connect.
4. **No import cycle:** function-local `from .health import data_classifier` inside the pragma-no-cover
   `run_recorder`; base has no module-level health import.
5. **No regression:** other feeds get 120s default, no behavior change beyond the (never-tripping for them)
   watchdog; F1–F4, roll-gap pre-sub, active-discovery + memory-leak fixes intact; 310 green.

## Threshold judgement (the flagged risk)
- **120s crypto:** zero false-trip risk (books update many times/second; 120s silence = broken).
- **300s Kalshi:** low risk, benign consequence. The monitor resets on ANY delta/trade across ALL
  subscribed markets, so the live market keeps it fresh. The only realistic false-trip is a pathologically
  quiet market (deep ITM/OTM, dead hours, MMs pulled) with zero book change for 5 min — and even then the
  consequence is a clean reconnect + fresh snapshot (~1–2s gap, NO data loss, baseline resets → no loop).
  The asymmetry (rare benign reconnect vs 11h blackout) favors shipping.

## Follow-up (operational, non-blocking)
Watch the `event=data_stale` WARNING rate after deploy; if it correlates with verifiably-quiet (no delta in
the raw for 5 min) rather than broken sessions, raise the Kalshi timeout (e.g. 600s). The correct lever is
the timeout value, NEVER the classifier (resetting on heartbeats would reintroduce the bug).

## Minor (non-blocking)
- Double JSON-parse per frame (classifier `as_dict` + each processor's `as_dict`); ~2× parse, negligible
  for the I/O-bound recorder; thread the parsed dict through if CPU ever matters.
- `proc.last_data_ns` is observability-only (the watchdog uses its own clock); nothing downstream depends on it.
