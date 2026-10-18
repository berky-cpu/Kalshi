# REV-020 — Red-team review: Kalshi recorder incident fixes (active-discovery + memory leak)

- **Date:** 2026-10-18
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/kalshi.py` (evict_market, deque bounds, settled-lifecycle evict,
  reset clears `_seq_by_sid`), `src/kbtc/recorders/kalshi_dynamic.py` (`_is_live`, active-aware
  `discover_open_tickers`, poller evict loop), `tests/test_kalshi_dynamic.py`, `tests/test_run.py`.
- **Verdict:** **PASS — clear to deploy.** `uv run pytest -q` → 300 passed (was 294; +6). Reads-only;
  public REST discovery; authed WS read session; no order path; `collections` stdlib (no new dep);
  `guard.py`/`.claude/`/`recorders/writer.py`/`execution/` untouched.

## The incident
`unattended-upgrades` restarted all recorders at 06:34 UTC. Two Kalshi bugs then caused an ~11h
degradation found via the live dashboard:
1. Discovery relied on Kalshi's `status=open` SERVER filter, but the tradeable KXBTC15M market is
   `status=active` (mid-window `?status=open` → 0 markets). So any re-discovery after a restart subscribed
   to nothing → Kalshi recorded nothing. (The REV-019 flagged residual, realized.)
2. The process leaked to **2.3 GB** over 11h (SIGTERM-timeout → SIGKILL) — would eventually OOM the 8 GB
   box and take down ALL feeds.

## FIX 1 — client-side live discovery — CONFIRMED
`discover_open_tickers(..., now_s)` no longer sends any `status=` param; it enumerates a close-time window
`[now−120, now+WINDOW_S+120]` and classifies client-side via `_is_live`: live iff status∈{active,open};
excluded iff status∈{finalized,settled,closed,determined,initialized}; unknown status → `open_time ≤ now <
close_time` fallback. `discover_target_tickers` sources the live set from this (active-aware), so the
tradeable market is included again. Live-confirmed against the public REST: the `active` market
`KXBTC15M-26OCT181445-45` is now in the target set (was dropped by the old `status=open` path mid-window).
Pagination/empty/dedup intact; upcoming pre-subscribe (REV-019) and grace-removal untouched.

## FIX 2 — memory bounded to O(1) — CONFIRMED (incl. the risky parts)
- `self.trades`/`self.lifecycle` were unbounded lists → `collections.deque(maxlen=4096/1024)`. **Nothing
  downstream reads them** — grep shows only the two `.append` sites; the normalizer's trade dataset is
  `NormalizeResult.trades` built from `extract.trade_rows(RAW)` (pipeline.py:306), NOT `proc.trades`. So
  capping them cannot drop dataset data (authoritative copy = untouched raw via writer.py). deque supports
  the `[0]`/`len`/iteration used.
- `evict_market(ticker)` drops `self.books[ticker]`; called from the poller after `apply_update` for each
  removed (post-grace) ticker AND on a terminal lifecycle frame. Verified it can NEVER evict a live
  `active`/subscribed market's book; idempotent; double/unknown evict is a no-op; does NOT touch
  `_seq_by_sid`/`sid_by_channel` (no subscription desync).
- `reset_subscriptions()` now also clears `_seq_by_sid` (re-anchored by the next snapshot per fresh sid).
- **O(1) bound verified:** a 500-roll simulation drove the real manager+processor — `books` peaked at 3,
  `_absent_since`/`_seq_by_sid` at 1, deques fixed-maxlen. No per-market unbounded growth.
- No F1/F2/F3/F4 regression (65/65 affected-suite tests); the per-tick try/except now also wraps the evict loop.

## Findings (all LOW, none block deploy)
- **F-a:** book eviction depends on a market being in `manager.subscribed`/`to_remove` or emitting a
  terminal lifecycle frame; a stray book for a never-subscribed ticker would only free on a terminal frame.
  Can't happen in practice (all book tickers come from subscribed markets); a periodic "evict any `books`
  key not in `manager.subscribed`" sweep would be belt-and-suspenders.
- **F-b:** the terminal-status taxonomy is a best-guess superset (only active/initialized/finalized are
  live-observed). An unlisted terminal status just defers eviction to the grace path (still bounded). The
  deploy run should log-sample observed `status` values to confirm.
- **F-c (resolved):** `iso_to_ns` is now genuinely used by `_is_live` (the REV-019 dead-import note closed).

## Deploy signals to watch (24/7 operational confirmation)
Steady `books tracked` count in the poller log (now emitted) + flat RSS.
