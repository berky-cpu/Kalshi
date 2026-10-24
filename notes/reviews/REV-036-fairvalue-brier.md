# REV-036 — Phase-3.2 digital fair value + Brier-vs-Kalshi-mid harness (+ REV-035 vol fixes)

- **Date:** 2026-10-24
- **Reviewer:** red-team-reviewer (read-only)
- **Scope:** NEW `src/kbtc/signals/fairvalue.py`, NEW `src/kbtc/signals/brier.py`,
  MOD `src/kbtc/signals/vol.py` (REV-035 caveat fixes) + `signals/__init__.py`,
  NEW tests (`tests/test_signals_fairvalue.py` 20, `tests/test_signals_brier.py` 18),
  NEW `notes/experiments/EXP-007-...md`. Reviews CODE/METHOD; the acceptance Brier number is
  deferred to the feed + >=3-5 held-out days.
- **Tests:** `uv run pytest -q` -> **494 passed** (matches expectation).

## Verdict: PASS-WITH-CAVEATS

The core pricing math is correct and MC-validated; the Kalshi-mid reconstruction is correct
(DOLLARS, not cents -- the schema comment is stale, the code is right); the gate paired-diff logic
is correct and cannot spuriously report BEATS on a straddling/positive CI; no leakage in the strict
sense; safety clean. **Sound to commit and to run for the PRELIMINARY/day-7 Brier.** Two items must
be handled before the headline number is interpreted (F1, F2). **ROADMAP 3.2 must NOT be ticked
until beats-mid OOS on >=3-5 independent held-out days** (EXP-007 already says so).

## Critical items — confirmed CORRECT

- **Settlement variance sum** (`pricing.settlement_moments`): `s = sum_k u_k*(2(m-k)-1)` (0-indexed,
  `future` ascending) is exactly `sum_{k,l} min(u_k,u_l)` -> `Var = sigma^2*s/n^2`. Verified by hand
  and by the 400k-path Monte-Carlo calibration test (`test_mc_calibration_matches_model`, <=0.01
  across tau=900/300/45/30 & n_obs=0/0/15/30, plus a non-default 10x1 s window). The MC test
  simulates the Brownian average independently, so it validates the variance sum, not just the
  Gaussian CDF -- real teeth.
- **Window-state invariant:** `expected_n_observed = n - n_future_samples`; `close_window_state`
  collects offsets `o >= tau` (sample time <= t) so `n_observed` always satisfies
  `settlement_moments`'s `n_observed == n - m`. The `o == tau` boundary is handled consistently
  (observed on both sides; future uses strict `<`). Pre-window / inside-window / at-close all
  correct.
- **Guards have teeth:** inside-window with `state=None` -> ValueError
  (`test_fair_value_requires_state_inside_window`); inconsistent `state.n_observed` -> ValueError
  (`test_fair_value_rejects_inconsistent_state`); index gap inside the forming window -> ValueError
  (`test_close_window_state_raises_on_gap`). The silent "priced as un-formed" bug is prevented.
- **Strike = open-window average, a known PAST constant.** Origins start at
  `open_time + window.length_s` (`build_market_predictions`), so the strike is realized before any
  origin is priced -> no look-ahead on the strike. Monotone in (Y-K), ATM-peaked delta, tau->0
  ties->YES all tested.
- **Drift genuinely OFF by default:** `drift_per_s=0.0` skips `_fair_value_with_drift` entirely
  (martingale). When exercised, `E[A] += drift*sum(u_k)/n` is the correct constant-drift shift,
  variance unchanged; documented as a 3.3 hook. Not silently nonzero.
- **Gate paired-diff bootstrap is PAIRED and sign-correct:** `_head_to_head` forms
  `db = Brier(model) - Brier(mid)` per origin, averages per day, resamples WHOLE days;
  `model_beats_mid = isfinite(upper) and upper < 0`. Cannot report BEATS on a straddling or positive
  CI (`test_gate_stop_and_rethink_when_model_worse`). `brier_diff<0` => model better.

## Kalshi-mid units (the "100x wrong" worry) — RESOLVED: code is correct

`brier.py` reads `kalshi_books.price` as DOLLARS 0..1 and uses it directly as a probability. I traced
the normalize path: `normalize.pipeline.stream_kalshi` -> `make_processor("kalshi")` ->
`KalshiProcessor` (the SAME class as the recorder), whose book stores `_dec(price_dollars)` from
dollar strings like `"0.0800"` (`recorders/kalshi.py:270-271`); `_kalshi_levels`
(`pipeline.py:356-366`) passes those Decimals through unchanged. So the normalized `price` column is
DOLLARS 0..1 and the harness mid is right. (No `kalshi_books` parquet exists on this box to
spot-check -- see F2.)

## Findings (ranked)

### F1 (MEDIUM) — in-window / tau=0 origins flatter the model vs the mid
`build_market_predictions` (`brier.py:451`) steps origins to `close_time_ns` **inclusive**,
including in-window (tau<60) and tau=0. Inside the window the model mechanically OBSERVES part/all of
the settlement average (`close_window_state` feeds realized prints), so near close its "fair value"
converges to the realized outcome (exactly 0/1 at tau=0). This is **not leakage** (all data <= t) but
it inflates the pooled Brier vs the mid, concentrated in the `[0,60)` tau bucket -- and those are
exactly the moments CLAUDE.md says to PULL quotes. A pooled "beats mid" could be driven by
non-tradeable end-of-window observation rather than fair-value skill.
**Fix:** headline gate on `tau >= window.length_s` (the genuine pre-window Asian-digital tradeable
regime); report in-window and tau=0 buckets as diagnostics only. Exclude tau=0 (market closed, not a
decision point). At minimum, disclose this in EXP-007 before interpreting the number.

### F2 (LOW-MEDIUM) — stale "INTEGER CENTS" schema comment is a landmine
`normalize/schema.py:79` and `normalize/pipeline.py:359` both say kalshi_books price is
"INTEGER CENTS (0..100)". The actual data is DOLLARS 0..1 (see above). The harness is correct, but
the contradictory comment is a live trap: anyone "fixing" `brier.py` to match it (x100 / /100) would
make the mid 100x wrong and silently dishonest. **Fix (doc-only, outside this PR's files):** correct
both comments to "DOLLARS 0..1". Belt-and-suspenders: before the PRELIMINARY run, spot-check on the
box that `kalshi_books.price` min/max is within [0,1].

### F3 (LOW) — `model_beats_mid` boolean can be True on 1-2 days
With `vals.size == 1` the block-bootstrap "CI" is a degenerate point `(v,v)`, so one negative-diff
day sets `upper < 0` -> `model_beats_mid=True`. `gate_statement` correctly appends `PRELIMINARY`, but
the boolean itself carries no credibility flag -- downstream automation keying off it (not the text /
`n_days`) could spuriously green-light.
**Fix:** AND `model_beats_mid` with `n_days >= 3`, or expose a separate `credible` flag. Keep 3.2
human-gated on >=3-5 days regardless.

### F4 (LOW) — crossed-book: clamp worry is UNFOUNDED; filtering is the real (minor) question
`mid = 0.5*(by + (1-bn))` with `by, bn` in (0,1] is **provably in [0,1]** -- it cannot exit the
range, so no clamp is needed and there is no clamp bug (contrary to the stated worry). The residual
issue is that locked/crossed books (`by+bn>1`, yes_bid > yes_ask) are NOT filtered; a crossed TOB is
often a transient stale one-sided quote and its mid is a noisy benchmark. Impact small and
~symmetric.
**Fix:** one-line disclosure; optionally skip crossed books for the headline.

### F5 (LOW) — no multiplicity control on 25 per-bucket gates
5x5 (tau,|z|) buckets each get their own `head_to_head`/`model_beats_mid`. The pooled h2h is the
pre-registered headline (good), but per-bucket "beats" flags invite cherry-picking.
**Fix:** label per-bucket flags diagnostic-only; correct for 25 looks if a bucket claim is made.

### F6 (LOW) — log-loss asymmetry at the degenerate close
The model can emit exactly 0/1 (var=0 branch); a knife-edge FP-rounding tie at tau=0 routed the wrong
way eats ~34.5 clipped log-loss, while Brier caps at 1.0. Only bites the tau=0 origin -- another
reason to drop tau=0 (ties into F1). Headline gate is Brier, so impact is contained; clipping vs
non-clipping is otherwise handled consistently.

### F7 (INFO) — EXP-007 disclosure gaps
EXP-007 honestly surfaces sigma-scale risk as the first stop-and-rethink (incl. EXP-006
"sigma ~6-10% low"), replica strike bias (EXP-002 -$1.29), and the PRELIMINARY single-market-warmup
caveats. It does NOT mention the crossed-book mid flag (F4) nor the in-window/tau->0 flattering (F1).
Add both.

## REV-035 vol.py regression check — PASS
The three changes are exactly the documented caveat fixes, confined to `settlement_z2_test` /
`bm_shape_diagnostic` (diagnostics): (1) causal grid read `searchsorted(..., side="right")-1` with a
`gi<0` guard -- removes the up-to-`step_s` future leak on off-grid origins; exact for grid-aligned
origins; (2) dead `seasonality` param documented + `_ = seasonality` no-op (callers unbroken, no
double-seasonalization); (3) Jensen comment only. 494 passed incl. the 34 vol tests. No regression.

## Safety — PASS
No order path, no production/demo order endpoint reference, no secrets. No edit to the demo-only
order guard module, the hooks directory, or the settings file (git status clean on all of those). No
new hard dependency: `plot_calibration` lazy-imports matplotlib inside a `# pragma: no cover` helper
with a clear ImportError; numpy already a dep; `pyproject.toml`/`uv.lock` unchanged. Floats used only
for probability math; Decimal preserved at the kalshi price boundary (`_to_float_price` converts only
into probability space, where no money/fees cross -- allowed by CLAUDE.md). Blast radius contained to
`signals/`.

## What would change my mind (toward FAIL)
- A box spot-check showing `kalshi_books.price` NOT within [0,1] (i.e. the "cents" comment is
  actually right and the live normalize path differs from what I traced) -- that makes the mid 100x
  wrong and the benchmark dishonest.
- The headline beats-mid being reported over the pooled set INCLUDING in-window/tau=0 origins and
  used to claim a win (F1).

## Recommendation
Sound to COMMIT and to run for the PRELIMINARY/day-7 Brier. Address F1 (restrict headline to
tau>=60 s or at least disclose) and F2 (schema doc + box price-unit spot-check) before interpreting
any number. Do NOT tick ROADMAP 3.2 until beats-mid OOS on >=3-5 independent held-out days.
