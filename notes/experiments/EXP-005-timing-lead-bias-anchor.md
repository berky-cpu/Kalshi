# EXP-005 — Timing-lead sweep (E2), bias-anchor feasibility (E1), decomposition/floor (E3)

- **Date:** 2026-10-24
- **Status:** PRELIMINARY / DIAGNOSTIC — no acceptance number, no model/code change, ROADMAP not ticked.
  Validates Fable's two "cheap, no-new-recording" corrections (timing shift + bias anchor) from the
  BRTI-accuracy analysis, on the EXP-002 clean data.
- **Owner:** index-replicator (run) → red-team advised before the k≈2 offset is adopted into fair value.

## Setup / provenance
- Data: `clean_22study_v2` (2026-10-18/19/20), 3 constituents Bitstamp+Kraken+Coinbase — all HEALTHY
  that window (the Bitstamp inversion onset was 10-21 23:00, after this window). Settlements = REV-027's
  `/tmp/exp002/settle_clean`. Box `src/kbtc/index/*` md5-identical to local HEAD (no index/ change since).
- Frozen endpoint `T-59..T` (EXP-002). Window set = full-coverage-at-all-k close windows so coverage never
  changes across k. 288 in-scope close windows; **223 full-cov** (TRAIN 10-18/19 = 143, held-out TEST 10-20 = 80).
- Harness validated before perturbing: k=0 err-vs-drift slope +0.0327 reproduces EXP-003's +0.034; k=0
  biases reproduce EXP-002/003.
- `k` = shift the 60-print average EARLIER by k s (sample `[T−59−k, T−k]`, compare to published(T)).
  Analytic slope of err-vs-drift = `(2−k)/59` → +0.034 at k=0, 0 at k=2.

## E1 — bias-anchor feasibility (ρ₁)
err series in settlement order (open(N)≡close(N−1)); moving-block bootstrap CI.

| split | n | ρ₁ | 95% CI | ρ₄ | Var kept 1−ρ₁² | mean bias |
|---|---|---|---|---|---|---|
| TRAIN 10-18/19 | 143 | +0.323 | [+0.096,+0.374] | +0.167 | 0.90 | +0.109 |
| TEST 10-20 | 80 | +0.494 | [+0.126,+0.558] | +0.210 | 0.76 | −1.360 |
| ALL | 223 | +0.421 | [+0.206,+0.473] | +0.212 | 0.82 | −0.418 |

**ρ₁ ≈ 0.3–0.5, BELOW the ~0.5 "clean-win" line** → naive anchoring cuts variance only 10–24% (soft win).
But the per-day bias WANDERS (10-18 +1.34, 10-19 −0.66, 10-20 −1.36) and ρ₄>0 → the bias is a slowly-drifting,
day-specific level offset, not a fixable constant. **Anchor is worth it as an ADAPTIVE level tracker, marginal
as a variance reducer.**

## E2 — timing-lead sweep (the key test). k̂ tuned on TRAIN, frozen, reported on held-out TEST.
TRAIN:
| k | bias | med\|e\| | p95\|e\| | max\|e\| | slope_b | (2−k)/59 |
|--|--|--|--|--|--|--|
| 0 | +0.11 | 2.180 | 6.291 | 8.710 | +0.0327 | +0.0339 |
| 1 | +0.08 | 2.030 | 6.395 | 8.650 | +0.0170 | +0.0169 |
| 2 | +0.04 | 2.040 | 6.285 | 8.500 | +0.0051 | 0 |
| 3 | −0.00 | **2.010** | 6.082 | 8.330 | −0.0138 | −0.0169 |
| 4 | −0.04 | 2.060 | 6.190 | 8.100 | −0.0327 | −0.0339 |
| 5 | −0.08 | 2.270 | 6.798 | 8.060 | −0.0468 | −0.0508 |

TEST (held-out 10-20), frozen k̂=3:
| k | bias | med\|e\| | p95\|e\| | max\|e\| | slope_b |
|--|--|--|--|--|--|
| 0 | −1.360 | 2.735 | 7.518 | 9.300 | +0.0285 |
| 1 | −1.292 | 2.690 | 7.022 | 9.320 | +0.0108 |
| 2 | −1.226 | 2.395 | 7.367 | 11.410 | −0.0028 |
| **3** | −1.154 | **2.240** | 7.666 | 12.970 | −0.0159 |
| 4 | −1.075 | 2.250 | 7.975 | 14.800 | −0.0315 |
| 5 | −0.988 | 2.205 | 8.022 | 16.420 | −0.0467 |

**Fable's prediction CONFIRMED:** slope_b lands on the `(2−k)/59` line and crosses zero at **k≈2 on BOTH
TRAIN (~2.2) and held-out TEST (~1.9)** — a real, theory-matching ~2 s replica lead (one-parameter shift).
Honest nuances:
- med\|err\| minimum is **shallow/flat** over k=1–3; the median-argmin picks k̂=3 but that's within noise.
  **Honest estimate = k̂ ≈ 2 s from the slope zero-crossing** (slope is the drift-systematic the shift nulls).
- Shift helps the CENTER: TEST med\|e\| 2.735 → 2.240 (−18%), de-correlates err from drift.
- Shift does **NOT** lower p95/max — tails WORSEN (TEST max 9.30 → 12.97) on whipsaw/reversal windows
  (the linear-drift timing model overshoots). It tightens typical fair value, not the worst case.
- Bias barely moves with k → confirms "bias is the wrong statistic for a time shift"; the −$1.36 is a
  day-specific LEVEL effect, not timing.

## E3 — decomposition / empirical floor (err = β(level) + (k̂/60)·drift + ε, k̂=3 frozen)
| TEST series | n | bias | sd_ε | med\|ε\| | p95\|ε\| | max\|ε\| |
|---|---|---|---|---|---|---|
| RAW err_0 | 80 | −1.360 | 3.909 | 2.735 | 7.518 | 9.300 |
| floor ε (const anchor) | 80 | −1.303 | 3.845 | 2.274 | 8.272 | 13.057 |
| floor ε (adaptive anchor) | 80 | −0.792 | **3.633** | **1.954** | 8.335 | 11.164 |

**Empirical floor ≈ sd_ε $3.6–3.8 (1σ), p95 ≈ $8.3, med ≈ $2.0–2.3** on the held-out day — barely below
raw error. The two cheap corrections strip the drift-slope and ~0.1–0.3σ; the bulk (level + tails) survives.
Adaptive anchor beats the frozen constant on TEST (tracks the day-specific level). Error splits cleanly into
(i) large-|drift| windows = timing-driven, fixed by the shift, and (ii) small-drift large-error windows =
level/missing-venue, untouched by the shift.

### Floor in fair-value CENTS (settlement-averaging.md §3; K≈$81.9k, σ≈17% ann.; cents ∝ 1/sd_A)
From sd_ε=$3.845 (1σ): τ=900s ATM **2.1¢**, 300s **3.9¢**, 120s 6.9¢, 90s 8.8¢, 60s 13.9¢, 30s 39¢.
From p95=$8.27: τ=900s 4.6¢, 300s 8.3¢, 90s 18.9¢, 60s 29.9¢, 30s 84¢. At |z|≥2, <1¢ for τ≥120s.
→ Replica floor is ~2–4¢ ATM at 5–15 min, exploding to 14–84¢ ATM in the final 30–60 s, <1¢ off-the-money.

## Honest read (the three questions)
1. **~2 s lead: holds, robustly.** slope crosses zero at k≈2 on train + held-out, matches theory, reproduces
   EXP-003. Cuts median |err| ~18%, removes the drift-systematic, but does NOT reduce p95/max. Defensible
   point estimate **k̂ ≈ 2 s** (slope-based; k=3 median-argmin is marginal on a flat curve). Adopt as an
   index-timing offset for fair value — it tightens the center, not the tail.
2. **Bias anchorable only softly.** ρ₁≈0.3–0.5 → 10–18% variance cut. Real value = adaptive tracking of a
   day-specific, wandering level bias (not a stable −$1.29 constant). Use the anchor as an adaptive level
   estimate, not for variance.
3. **Floor in cents:** ~2–4¢ ATM at 5–15 min, 14–84¢ ATM in the last minute, <1¢ off-money. **The cheap
   corrections do NOT reach p95<$5** — the floor is dominated by the missing constituents + construction,
   not timing. Timing+anchor are genuine second-order polish on top of a missing-venue floor.

## Implications
- Reinforces the metric reframe: express replica accuracy as FV-cents per (τ,|z|); the replica is safe ATM
  at long τ and dangerous ATM near expiry → "pull quotes near the strike in the last 60–90 s" falls out
  mechanically.
- The biggest lever remains **recording the published `cfbenchmarks_value` feed** (sidesteps the replica
  floor for the LEVEL entirely) + constituents; timing/anchor are refinements for the replica's signal role.

## Caveats
3-day window; single autocorrelated held-out day (ρ₁≈0.4 → effective n ≪ 223); 3/7 constituents (floor is a
subset-replica floor); cents scale set by a ~17% ann. vol estimate (cents ∝ 1/σ). k̂=3 is the median-argmin
but k≈2 (slope zero-crossing) is the honest estimate.

## Artifacts
Scripts `notes/_exp005_scripts/{exp005.py,plot_exp005.py,exp005_run.log,exp005_windows.csv}` (uncommitted);
figure `notes/figures/EXP-005/exp005_timing.png`. Follow-up: small tested PR adding `slope_b` + `rho1` to
`tracking.py` ErrorStats (slope_b needs `drift` on WindowResult; rho1 needs settlement-ordered chaining).
