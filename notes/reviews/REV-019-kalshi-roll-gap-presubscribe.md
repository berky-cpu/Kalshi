# REV-019 — Red-team review: Kalshi roll-gap fix (pre-subscribe upcoming windows)

- **Date:** 2026-10-18
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/kalshi_dynamic.py` (`discover_upcoming_tickers`,
  `discover_target_tickers`, `plan_update(desired, now_s)`, `apply_update`), `src/kbtc/recorders/run.py`
  (LOOKAHEAD/GRACE wiring + `discover_target` in subscribe_factory/poller), `tests/test_kalshi_dynamic.py`,
  `deploy/{env.example,README.md}`.
- **Verdict:** **PASS.** `uv run pytest -q` → 294 passed (was 287; +7). Reads-only; public REST discovery;
  no order path; `guard.py`/`.claude/`/`recorders/writer.py`/`execution/` untouched; no new dep.

## Why
The deployed dynamic recorder picked up each new 15-min market only on the next poll, so at every roll the
new market's FIRST ~45 s — its **open-window (the strike-forming 60 s)** — was missing. Live-measured a
**49.1 s hole at the 03:45 roll** (all other inter-message gaps <1 s).

## The fix
Pre-subscribe the upcoming market BEFORE it opens:
- `discover_upcoming_tickers` queries a bounded close-time window `[now, now+(max_upcoming+1)·900s]` and
  keeps `status=="initialized"` rows client-side (the verified status VALUE — it does NOT rely on a server
  `status=initialized` filter, unlike the open path which trusts the verified `status=open` server filter),
  sorted by close_time, capped at `max_upcoming` with a HARD cap `_MAX_UPCOMING_CAP=2`.
  `discover_target_tickers` = open ∪ upcoming (open first, deduped). Both the connect-time subscribe_factory
  and `run_poller` use it, so the next window is subscribed via the initial subscribe + the poller's
  `add_markets` path.
- `plan_update(desired, now_s)`: `to_add` = desired−subscribed (idempotent — a pre-added upcoming market is
  not re-added); `to_remove` = markets not-desired for ≥ `remove_grace_s` (default 120 s), via a per-ticker
  `_absent_since` timer cleared if the market re-enters desired. So a just-closed market's final frames are
  retained, not dropped the instant it closes.
- Config: `KBTC_KALSHI_LOOKAHEAD` (default 1, hard-cap 2), `KBTC_KALSHI_REMOVE_GRACE_S` (default 120).
  `KBTC_KALSHI_POLL_S` no longer bounds roll coverage.

## Verified (behaviorally re-derived)
- **Bounded set:** `max_upcoming = max(0, min(int(x), 2))` at the authoritative place — config=99 → ≤2; a
  mis-set env can't blow up the set. Steady-state target ≈ open(1) + upcoming(≤2) + closed-within-grace(1)
  ≤ 4 — far below any plausible error-26 market limit.
- **Grace-removal:** full timeline verified — kept within grace, timer cleared on re-entry (flapping never
  deletes), removed exactly once at absent+grace; `_absent_since` bounded three ways (plan clears non-absent,
  apply pops removed, reset clears on reconnect); a failed delete-send just retries next tick (no leak).
- **Idempotency:** a pre-added upcoming market stays in `subscribed` → excluded from `to_add` every later
  poll (no add_markets thrash while it sits subscribed-but-not-open).
- **No F1/F2 regression:** the `plan_update` signature change is absorbed; error-frame logging, reset_subs,
  reconnect-partial-reack tests all still pass (59/59 affected suite); F3/F4 intact.

## LIVE cross-roll validation (Ashburn, Kalshi PRODUCTION authed read, across the 2026-10-18T04:00Z roll) — PASS
New market `KXBTC15M-26OCT180015-15` first snapshot **−186.7 s before open** (PRE-SUBSCRIBED), first real
`orderbook_delta` at **+0.0 s vs open**; old market streamed to +1.0 s past close. **Max inter-message gap
in [roll−90 s, roll+90 s] = 0.120 s** (215,976 frames), vs the previous 49.1 s hole. The strike-forming
open-window 60 s is now fully captured. Consistent with the code (upcoming enters the target set up to
~900 s before open; pre-subscribe at connect + poller add_markets on the initialized market).

## Residuals (LOW, non-blocking)
- `iso_to_ns` was an unused import — **removed** before commit.
- `discover_open_tickers` has no client-side status defense (pre-existing, REV-018); it trusts the verified
  `status=open` server filter, proven live by the 0.120 s result. A belt-and-suspenders client check would
  match the upcoming path's posture; not required.
- Applied-state still advances on send not the server `ok` (documented fast-follow, now loud via F1).
