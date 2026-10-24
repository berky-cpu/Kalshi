# REV-035 — Phase-3.1 realized-vol estimator (code + method)

- **Reviewer:** red-team (skeptical). **Date:** 2026-10-24.
- **Scope:** the MODULE — `src/kbtc/signals/vol.py` (+ `signals/__init__.py`), `tests/test_signals_vol.py`
  (34 tests), and the honesty of `notes/experiments/EXP-006`. This reviews the CODE/METHOD so 3.2 can
  build on it. It is **NOT** an acceptance review of the σ number — acceptance is explicitly deferred
  to the published `cf5hz` feed over ≥7 days / ≥5 held-out. **ROADMAP 3.1 must stay UNTICKED.**
- **Verdict: PASS-WITH-CAVEATS.** The code is sound to commit and to build 3.2 on. No leakage, no
  circularity, no safety issue; `uv run pytest -q` = **456 passed** (was 422; +34). Caveats below are
  minor (2 latent-look-ahead/API warts in the standalone diagnostics, 1 negligible Jensen
  approximation, EXP doc staleness). None block the commit; none change the deferred-acceptance status.

## Default skeptical position
"The edge is fake / the σ is mis-scaled." After reading the code (not just docstrings) and running the
suite, the module's own machinery (κ̂ level fix + z² level test + non-circular coarse target) is
honestly built to catch exactly that, and the EXP note does not overclaim. The remaining risk is
entirely the small-N / replica-not-feed problem, which the note itself foregrounds.

## Checklist findings

### 1. Look-ahead / leakage — PASS (verified in code)
- `walk_forward_eval` (vol.py:861-917): for each test day `d`, `past = ts_ns < day0`; seasonality is
  fit on `make_grid_ns(t_start, day0, …)` (ends at the test-day boundary) and κ̂ via
  `calibrate_scale_kappa(ts_ns[past], prices[past], …)` — both **strictly before** the scored day.
- EWMA path is built causally from `t_start` and is proven truncation-invariant by
  `test_ewma_is_causal_truncation_invariant` (appending later data cannot change an earlier level).
- `expected_forward_variance` (vol.py:569-592) multiplies the **as-of-t** deseasonalized level by the
  **past-fit** seasonal shape integrated over the forward window — the only "future" object is the
  seasonal SHAPE, which is known a priori; the LEVEL and κ̂ are as-of-t / training. No forward value
  (target `A_{t+τ}`) enters the forecast. The realized target `_forward_realized_var` legitimately uses
  `[t0, t0+H]` future prices and is **not** scaled by κ̂, so a κ̂ error shows up in QLIKE and z² (good).
- Walk-forward supplementary cut (test = 10-19 AND 10-20) is correctly handled: each test day refits on
  its own strict past, so 10-20 uses 10-18+10-19 and 10-19 uses 10-18. (Note: this means the two test
  days get DIFFERENT κ̂; EXP-006 §3b reports a single "κ̂=1.196" for the supplementary cut, which is a
  reporting simplification, not a code bug.)
- Only `recv_ts_ns`-style availability time is consumed; the module never reads `exch_ts_ns` (it takes
  a generic `ts_ns` and relies on the caller to pass recv-time — documented, caller's responsibility).

### 2. κ̂ correctness — PASS
- `calibrate_scale_kappa` (vol.py:287-313) = `sig(Δ*)/sig(base_step)` where `sig(s)` is the
  **per-second variance** (each Σr² normalized by its own count·s). This is the statistically correct
  reading: it rescales the fine-scale level to the coarse (settlement) scale. Fable's literal
  `[Σr_Δ*²/Δ*]/[Σr_5²/5]` with RAW sums would carry a spurious `base/Δ*` count factor; the implemented
  per-second-variance ratio removes it. `test_kappa_lifts_smoothed_level_to_coarse_scale` pins
  `kappa == sig[60]/sig[5]` and shows the κ̂-corrected 5 s EWMA tail recovers the coarse truth.
- Applied to ALL race models: `ewma_cfg = replace(cfg.ewma, scale_kappa=kappa)` for the model, and
  `_trailing_forecast`/`_prevday_forecast` multiply by the same κ̂ (vol.py:885, 907-908, 922-937). So
  the QLIKE race tests EWMA+seasonality, not the scale — confirmed by
  `test_walk_forward_coarse_target_model_beats_trailing_under_smoothing`. κ̂ is calibrated, not a knob.

### 3. z² level test — PASS (has teeth)
- `settlement_z2_test` (vol.py:1002-1059): `z = (ln A_{t+τ} − ln Y_t)/√Var`, `Var =
  settlement_moments(ln Y_t, τ, σ̂_t, n_observed=0).var`. `n_observed=0` requires all 60 samples
  future, i.e. τ>59 s — satisfied for τ∈{60…900}. σ̂_t = √(scale-calibrated var_per_s) as-of t0; A is
  realized after. Correct causal pairing; the denominator is the discrete averaging-compressed variance
  that matches the numerator's point-to-average dispersion, so z²→1 when σ correct.
- **Teeth:** `test_settlement_z2_detects_sigma_too_low` sets `scale_kappa=0.5` (σ = 0.707× truth, Var =
  0.5× truth) and asserts `mean_z2 ≈ 2.0` and `implied_sigma_bias < −0.1`. The test genuinely fails if
  the level logic breaks. `implied_sigma_bias = √(1/z²) − 1` has the right sign.
- Per-day block bootstrap (resample whole days) is the right tool for the within-day origin overlap;
  with N=1 held-out day it correctly collapses to a point (degenerate, honestly flagged).

### 4. coarse_subsampled / signature_plot / BM-shape — PASS (non-circular)
- `coarse_subsampled_var_per_second` (vol.py:261-284): overlapping Δ*-second returns on the fine grid,
  `mean(r²)/Δ*`. It is a genuinely different estimator/measure from the 5 s EWMA forecast (future data,
  Δ*=60 s scale) — the earlier TSRV-on-both circularity is resolved. The forecast and target target the
  same SCALE (correct — you want to predict the settlement-scale variance) but a κ̂ scale error is NOT
  shared by the proxy, so it shows up in QLIKE. `test_coarse_subsampled_is_different_measure_from_fine_
  under_smoothing` confirms coarse > fine under smoothing.
- `bm_shape_diagnostic` validates V(τ)/τ and V_A(τ)/(τ−2w/3) flatness independently; flat under GBM in
  test. Across overlapping origins the per-origin variances are still unbiased for the marginals.

### 5. EWMA / seasonality — PASS
- Causal time-decay EWMA (`_decay_per_step` = 1−2^(−step/halflife)); gap returns (NaN) HOLD the level
  (no update, no spurious variance) — `test_ewma_gap_does_not_inject_variance`. Deseasonalize-then-
  reseasonalize at read-out is level-preserving (`test_seasonality_is_level_preserving`); shrinkage
  toward 1 stabilizes sparse bins; fit is count-weighted-mean-1 normalized and past-only. Numerically
  sound (var_floor, positive-factor guard).

### 6. EXP-006 honesty — PASS (with doc-staleness nit)
- §3b is labeled PRELIMINARY and discloses the unflattering findings: the only non-degenerate z² CI
  does NOT cover 1 (sits ~13–24% high / residual σ ~6–10% low); the QLIKE edge shrank from 0.863
  (pre-fix, circular) to 0.93–0.97 and the model LOSES to trailing on 10-19 (0.2925 vs 0.2904); κ̂
  moves ~8% between the 1-day and 2-day fits; 2-of-7-constituent replica, not published BRTI;
  Δ*×half-life sensitivity is not robust (reverses to >1 at Δ*=120/short half-life). Acceptance is
  correctly deferred to cf5hz ≥7 days/≥5 held-out. This is honest.
- **Nit (not a correctness issue):** the "Honest caveats" bullet ("κ̂-corrected replica numbers are not
  yet produced … box re-run PENDING"), the "Red-team review PENDING" line, and the "Conclusion / Next:
  run §3b" line are STALE now that §3b is filled in. Clean up so the note doesn't read as if §3b is
  still outstanding. Purely editorial.

### 7. Blast radius / safety — PASS
- `signals/` is a brand-new package; no other `src/kbtc` module imports it (the only `signals` grep
  hits — recorders/bitstamp.py, recorders/kalshi_cfbenchmarks.py — are unrelated English comments). Full
  suite green at 456 (422 + 34). No order path, no `execution/guard.py`, no `.claude/`, no
  `settings.json`, no secrets touched. Floats only, no money crosses this module. No new dependencies
  (math, dataclasses, numpy, kbtc.pricing). Relies only on the VERIFIED `SettlementWindow` (60×1 s,
  pricing.py:38-39) — nothing UNVERIFIED is load-bearing in the committed code.

### 8. Tests have teeth — PASS
- Key logic-guarding tests fail if the logic breaks: z²-detects-low-σ, TSRV-vs-5×-biased-naive,
  κ̂-lifts-to-coarse, causal-truncation-invariance, coarse≠fine-under-smoothing, gap-no-variance.
  A few (e.g. `test_walk_forward_model_beats_baselines`) are "works-when-it-should" on favorable
  synthetic data rather than anti-bug tests — acceptable as sanity checks.

## Minor caveats to fix (non-blocking; fold into 3.2 or a follow-up)
1. **Latent ≤step_s look-ahead in the standalone diagnostics.** In `settlement_z2_test` (vol.py:1029)
   and `bm_shape_diagnostic`, σ̂/Y are read at `gi = searchsorted(grid, t0, side="left")`, which for a
   t0 NOT on the grid returns the first grid point > t0 — i.e. σ̂ uses data up to ~step_s AFTER t0.
   Inside `walk_forward_eval` origins are snapped onto the grid so this is exact/causal, but a caller
   passing arbitrary origins to the diagnostics gets a bounded (≤5 s out of τ≥60 s) look-ahead. Fix:
   use the last grid point ≤ t0 (`side="right"−1`), or document that diagnostic origins must be
   grid-aligned.
2. **Dead/misleading parameter.** `settlement_z2_test(…, seasonality, …)` never uses `seasonality` in
   its body (correct — `var_path.var_per_s` is already re-seasonalized). Drop the param or `assert` it
   matches, so it doesn't imply a second re-seasonalization.
3. **Jensen approximation (negligible).** z uses `ln(arithmetic mean of prices)` while
   `settlement_moments` models the mean of log-prices. The within-60 s-window gap is ~3e-7 in log units
   vs an a-value ~3e-3 at τ=900 — ~0.01%, immaterial. Worth a one-line comment so it isn't mistaken for
   a bug later; revisit only if the real feed's settlement is a true arithmetic $ average and τ is tiny.

## What would change my mind (toward FAIL)
- A feature/normalization/κ̂/seasonality fit that touched `ts_ns >= day0` for the scored day (I checked:
  it does not).
- κ̂ applied to the model but NOT the baselines (it is applied to both).
- The forward target sharing κ̂ or the forecast's exact estimator (it does not — different data, scale
  only).
- The z² test failing to flag a deliberately mis-scaled σ (it flags it, `scale_kappa=0.5 ⇒ z²≈2`).
- A new import reaching an order endpoint / guard / secrets (none).

## Gate statement
**CODE: PASS-WITH-CAVEATS — sound to commit and to build 3.2 on.** **ACCEPTANCE: still open —**
do NOT tick ROADMAP 3.1 until the cf5hz published feed over ≥7 days / ≥5 held-out shows the κ̂-corrected
z² CI covering 1 (at least at τ=300 and 900 s) AND model/trailing QLIKE < 1 on a majority of held-out
days AND κ̂ day-to-day range < ~15%. On the current 3-day 2-of-7 replica those are point estimates and
the one non-degenerate z² CI does NOT cover 1 — consistent with the EXP's own PRELIMINARY labeling.
