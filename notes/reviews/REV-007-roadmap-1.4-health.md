# REV-007 — Red-team review: ROADMAP 1.4 (recorder health report)

- **Date:** 2026-10-16
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/health.py`, `tests/test_health.py`,
  `tests/fixtures/health/data/raw/*` (committed zst), `.gitignore`
- **Verdict:** **PASS (Round 2)** (Round 1 was PASS-with-required-follow-ups). `uv run pytest -q` → 92 passed.
  Safety clean: reads-only (stdlib + local processors + `writer.read_records`), no network/order path, no
  secret logging; `writer.py`, `execution/guard.py`, `.claude/*` untouched. `.gitignore` change correctly
  scoped (real `data/` + `*.parquet`/`*.jsonl.zst` still ignored; only `tests/fixtures/**` re-included —
  verified via `git check-ignore`). Reviewer hand-recomputed the fixture metrics from raw timestamps.

## What it does
Per feed {venue, channel} from `data/raw/.../HH.jsonl.zst` (via `writer.py`): disk usage, message rate,
coverage/uptime (`gap_time_fraction = downtime/window`), and sequence gaps by **replaying recorded raw
through the real per-venue `MessageProcessor`** (no reimplementation). CLI prints a table + `--json`;
non-zero exit on any unhealthy feed.

## Round 1 — PASS with required follow-ups
- **F1 [HIGH, gating]** Coverage/`uptime` counted *any* frame (incl. heartbeats/acks) as "up", so a
  stalled-but-heartbeating feed could report ~100% uptime and falsely pass 1.3's `<0.1%` gap gate.
- **F2 [MED]** The REST-snapshot prime + re-seed-after-gap (cascade-avoidance) paths had no fixture.
- F3/F4/F5 (LOW): rate fencepost/denominator doc; surface Kraken upper-bound; committed zst fixtures.

## Round 2 — PASS (all addressed)
- **F1 FIXED:** per-venue **data-frame classifier** strips control frames (subscription acks, heartbeats,
  pong, schema objects); BOTH coverage and `msgs_per_s` now use **data frames only** (the processor still
  sees every frame, so sequence detection is unaffected). Added a **`healthy` gate =
  `gap_time_fraction < gap_threshold AND msgs_per_s >= min_rate`** (defaults 0.001/1.0, configurable),
  surfaced in table/JSON with non-zero CLI exit. Regression test proves a heartbeat-only stall
  (`max_quiet_s=30`, heartbeats every 10s) is NOT filled → `gap_time_fraction≈599/600`, `healthy=False`.
  Excluding control frames can only ADD downtime (strictly conservative — cannot under-count silence).
- **F2 FIXED:** `test_sequence_jump_is_one_gap_not_a_cascade` (Coinbase 101,102,105,106) structurally
  requires the re-seed branch — without it 106 would emit a second gap; the test asserts exactly 1.
- **F4 FIXED:** per-feed `sequence_gaps_exact` (False for Kraken's in-band-CRC upper bound). **F3 FIXED:** rate
  convention documented, paired with gap-time in the gate. **F5:** committed zst fixtures kept (hermetic).

## The `healthy` gate is the correct 1.3 acceptance
`gap_time_fraction < 0.1% AND msgs_per_s >= floor` — neither a heartbeat-only stall nor a connected-but-slow
feed can pass. 1.3 must gate on this conjunction, **not** uptime/presence alone.

## Standing items (backlog, non-blocking)
1. **Binance-prime fixture** — `_prime_binance`'s inclusive `pu` boundary seed has no fixture (no Binance
   data in the tree); the structurally-critical reseed path is covered via Coinbase. **Add a Binance
   `depthUpdate` jump fixture before any Binance depth feed is used to satisfy 1.3.**
2. **Single global `min_rate`** — a legitimately sparse feed (e.g. Kalshi trade in a quiet window) would be
   flagged unhealthy (conservative false-ALARM, not a false-pass); per-feed thresholds would improve it.
3. **Coinbase classifier is a blacklist** (others are whitelists) — a novel Coinbase control type would be
   mis-counted as data; low risk, consider normalizing to a whitelist.
4. Control-frame vocab is derived from processors/feeds.md; a mis-classified frame fails safe toward
   "unhealthy", not toward a false pass.
