# EXP-006: Microstructure-robust realized-vol estimator for the BTC index (ROADMAP 3.1)

- **Date / author (agent):** 2026-10-24, quant-modeler (build + test; **revised same day** per a Fable
  design review that found a σ LEVEL bias — see "## Revision" below). **NOT yet reviewed.** Does not
  count until red-team PASS. Real-data/replica QLIKE is PENDING an on-box run (see Protocol note).
- **Scope:** ROADMAP 3.1 only — the σ estimator that feeds `pricing.settlement_moments` /
  `pricing.digital_up`. NOT 3.2 (fair-value calibration vs Kalshi mid) — explicitly out of scope.

## Hypothesis

A short-horizon realized-vol estimate of the BTC index that is (a) **microstructure-robust** (does
not inherit the bias of naive summed squared high-frequency returns) and (b) **intraday-deseasonalized**
will forecast forward realized variance **better (lower out-of-sample QLIKE) than naive baselines** —
a trailing-window realized variance and a previous-day-same-time ("random-walk") realized variance —
walk-forward by calendar day. σ is the diffusion coefficient (log-return per √second) that the
settlement-averaging variance `σ²(τ−2w/3)` / `σ²τ³/(3w²)` and the digital fair value are built on
(`docs/derivations/settlement-averaging.md`), so a biased σ feeds straight into mispriced YES/NO.

**Why these choices (motivation / literature):**
- Naive realized variance `Σ(Δp)²` is biased by **additive microstructure noise** UP by ~`2 n σ_u²`
  (grows with sampling frequency; Zhou 1996, Zhang–Mykland–Aït-Sahalia 2005) and, because the BRTI
  is a **depth-weighted mid (a low-pass / MA filter)**, biased DOWN at the finest scale by the
  induced return autocorrelation. We handle both with a **signature plot + sparse sampling** and the
  **Two-Scale Realized Volatility (TSRV)** estimator, which is consistent under additive noise.
- **Intraday seasonality**: BTC vol has a strong UTC time-of-day pattern (US/EU/Asia sessions). We
  deseasonalize with a multiplicative per-UTC-bin factor estimated **on past days only**, so the
  stochastic-vol level is estimated on a stationary series and σ is not biased by the diurnal shape.
- **QLIKE** as the loss because it is robust to a noisy-but-unbiased volatility proxy (Patton 2011);
  the proxy for forward realized variance is **TSRV** (unbiased under noise), not naive RV.

## Data

- **Input price series (design):** generic `(recv_ts_ns, price)` — `recv_ts_ns` (availability time)
  only, never `exch_ts_ns`; fully causal. Two sources, both supported by the runner:
  - **Preferred going forward:** the published `cfbenchmarks_value_5hz` BRTI feed (5 Hz) recorded by
    `recorders/kalshi_cfbenchmarks.py` → `data/raw/kalshi/cfbenchmarks/`.
  - **Build/eval fallback:** the replica index reconstructed over clean constituent books
    (`/opt/kbtc/data/clean_22study_v2`) via `kbtc.index`.
- **Real data lives on the VPS**, not on the workstation where 3.1 was built, so the **real-data QLIKE
  is produced by `notes/_exp006_scripts/run_vol_qlike_onbox.py` on the box** (reads the cf feed raw,
  runs the harness). This note reports (1) the full test suite and (2) a **simulation first-pass** that
  exercises the end-to-end harness on a series with KNOWN ground-truth vol; the real-data number is
  **PENDING** and must be filled in + red-teamed before 3.1 is ticked.

## Method (fixed BEFORE looking at any result)

- **Estimator** (`src/kbtc/signals/vol.py`):
  - Causal gap-aware grid sampling (`sample_log_prices_on_grid`): last-observation-carried-forward at
    a sparse interval `step_s`, with a staleness cap that turns a feed hole into a dropped sample (no
    stale carry, no interpolation across gaps).
  - Realized measures: naive `realized_variance`, `subsampled_rv`, **`tsrv`** (noise-bias-corrected),
    `signature_plot` (per-second variance vs sampling step — justifies `step_s`).
  - `IntradaySeasonality`: multiplicative per-UTC-bin factor, count-weighted-mean-1 normalized,
    shrinkage toward 1 for sparse bins, fit on PAST returns only.
  - `estimate_vol_path`: causal time-decay EWMA (half-life `halflife_s`) on **deseasonalized**
    sparse per-second squared returns; re-seasonalized at read time. Gaps hold the level (no spurious
    variance). `expected_forward_variance` integrates the seasonal curve over `[t, t+H]` → the
    `σ²·τ` the pricing layer wants.
- **Forward target:** TSRV-based realized variance over `[t, t+H]` (unbiased proxy).
- **Forecasts scored:** `model` (the EWMA forecaster) vs baselines `trailing` (trailing-window RV ×
  horizon) and `prevday` (same-clock-time RV one day earlier — a seasonality-aware random walk).
- **Protocol:** `walk_forward_eval` — for each held-out TEST calendar day, seasonality is fit on data
  **strictly before** that day; the EWMA runs causally from the start of data; forecasts are made at
  origins spaced `forecast_every_s` through the day; QLIKE is scored only on held-out origins.
  Headline = mean QLIKE on TEST days; auxiliaries = QLIKE ratio model/baseline, Mincer–Zarnowitz
  (slope→1, intercept→0), per-day block-bootstrap 95% CIs. **Config frozen before results:**
  `step_s=5 s`, `halflife=600 s`, `horizon=900 s` (one contract life), `forecast_every=300 s`,
  24 seasonal bins, shrinkage 0.25, TSRV `k=5`.

## Revision (2026-10-24, Fable level-fix) — pre-registered BEFORE re-running

**The disqualifying finding.** The first build's production σ did NOT actually apply a microstructure
correction: `estimate_vol_path` / `_trailing_forecast` ingested naive 5 s squared returns, and TSRV
lived only in the forward *target* — which also made the QLIKE proxy share the forecast's measure
(circular). The BTC index is **smoothing-biased** (per-second variance RISES with coarser sampling; on
the replica 5 s 2.39e-9 → 60 s 3.11e-9, not flattened by 60 s), so a 5 s σ is biased **~14% LOW** →
~2.5–3.4¢ systematic mispricing near |z|=1. TSRV is misspecified here (its linear-in-n bias model
fails inside the autocorrelation range, which extends past 60 s) and is itself biased low. The
**horse-race ranking** (EWMA+seasonality vs trailing) is a separate question and is expected to survive.

**The fix (implemented in `src/kbtc/signals/vol.py`):**
1. **Scale-calibrated LEVEL:** σ̂²_t = κ̂ · EWMA_t(deseasonalized naive `step_s` r²/step), with
   **κ̂ = sig(Δ\*)/sig(step_s)** (ratio of per-second variances / signature-plot values) pooled over
   the TRAILING TRAINING days only, re-estimated per test day (one scalar) — `calibrate_scale_kappa`.
   The SAME κ̂ is applied to the trailing- AND prevday-baselines so the QLIKE race still tests
   EWMA+seasonality, not the scale fix (`walk_forward_eval`, `EwmaVolConfig.scale_kappa`).
2. **Extended `signature_plot`** steps to include {120, 300, 600, 900} s; Δ\* chosen where it flattens
   (candidate **60 s**, pre-registered). On the replica it may not flatten by 60 s — the full table is
   reported; the real Δ\* is decided on the published feed later.
3. **Forward target changed** to `method="coarse_subsampled"` (param Δ\*): overlapping Δ\*-second
   returns on the 1 Hz grid, `Σ r_Δ*²/Δ* × H/N` — a DIFFERENT, settlement-scale, model-free measure
   from the 5 s forecast (not circular). `tsrv()` is KEPT (the 5 Hz feed may show a genuine
   additive-noise regime; the signature plot decides).
4. **Settlement-exact z² LEVEL test** (`settlement_z2_test`): a(t,τ)=ln A_{t+τ}−ln Y_t, A = the 60 s
   average, z = a/√Var_forecast via `pricing.settlement_moments`; mean z² (1.0 = correctly scaled;
   1.3 ⇒ σ 14% low) with per-day block-bootstrap CI + a PIT/KS normality check. **This is the
   pricing-relevant check QLIKE cannot give.**
5. **Kept the σ + settlement-averaging-formula architecture** (do NOT estimate Var[A] directly). Added
   a **BM-shape flatness diagnostic** (`bm_shape_diagnostic`): V_A(τ)/(τ−2w/3) and V(τ)/τ for
   τ∈[60,900]; if flat within CI the model holds; a fixed multiplicative c(τ) table (fit on training)
   is warranted ONLY if it fails.
6. No deconvolution to the efficient price (settlement IS the smoothed index); no pre-averaging
   (dominated in a no-noise regime).

**Pre-registered validation protocol (BEFORE re-running):** half-life stays 600 s; seasonality
unchanged (24 bins, shrink 0.25); Δ\* and κ̂ from TRAINING days only; report the **FULL** sensitivity
table over Δ\*∈{30,60,120} s × half-life∈{300,600,1200} s (not the best cell); report **κ̂ by vol
tercile** (if it wanders >~15% the single-scalar assumption breaks → note it). **Acceptance (deferred
to the published feed, ≥7 days / ≥5 held-out; the replica stays PRELIMINARY):** model/trailing QLIKE
< 1 on a majority of held-out days AND mean-z² CI covers 1 at τ=300 and 900 s AND κ̂ day-to-day range
< ~15%.

## Costs modeled

N/A — this is a vol-forecast accuracy study (QLIKE + z² level), not a P&L backtest. (Fees/latency/queue
enter at 3.2+/Phase 4.)

## Results

### 1. Tests — `uv run pytest -q`: **456 passed** (was 422; +34 in `tests/test_signals_vol.py`).
Covers: unit roundtrips; causal LOCF + staleness gap; RV recovers known vol; TSRV recovers true vol
under additive noise where naive RV is >5× biased; signature plot flattens as sampling sparsens;
sparse sampling recovers truth under MA smoothing; seasonality recovers a 2× pattern + level-preserving
+ shrinkage; EWMA recovers constant vol, tracks a >3× regime change, **causally truncation-invariant**,
no variance injection across a gap; QLIKE min at truth; MZ perfect→slope1/r²1.
**New (fix):** **κ̂ lifts a smoothed series' 5 s level to the coarse/true scale** (κ̂≈sig(Δ\*)/sig(5 s));
**coarse-subsampled target recovers var on a fine grid** and is a higher measure than the fine scale
under smoothing; **z² ≈ 1 when σ correct, ≈ 2 when σ set too low** (the level test detects the bias);
KS small for true-normal; **BM-shape flat under GBM**; end-to-end walk-forward (coarse target + κ̂ on
both) **model beats trailing under smoothing**.

### 2. Simulation validation of the FIX (NOT the real-data/replica number) — `sim_qlike.py`
Simulated index smoothing-biased like the replica (MA low-pass + additive noise). 6 UTC days, TEST =
last 3. **Extended signature plot** not flat (5 s dip 9.0e-9 → ~1.5e-8 by 60–300 s); **κ̂=1.68** on
TRAIN ⇒ 5 s σ ~22% LOW. **κ̂ by vol tercile {low 1.14, mid 1.37, high 1.94} — range >15%** → single
scalar imperfect (flagged). **QLIKE (coarse target, κ̂ on both, 861 origins):** model 0.0889 / trailing
0.0984 / prevday 0.1768 → **ratios 0.903 / 0.503** (both <1, horse-race survives the fix). **z² level
(3-day CI):** UNCORRECTED 1.42–1.52 (σ 16–19% low, CI>1); CORRECTED (κ̂=1.68) 0.85–0.91 (slightly
over-corrected — the 5 s base sits in the signature dip — CI approaches/covers 1 at τ≥120). **BM-shape
flat** (σ²_point, σ²_avg ~1.46–1.61e-8) → no c(τ) table needed. Confirms the fix works on known truth.

### 3. PRE-FIX FIRST PASS on the REPLICA index (NOT final acceptance; the source of Fable's finding).

Run on the Ashburn box (`root@5.161.236.203`, read-only on `/opt/kbtc/data`), 2026-10-24, over the
**3 Bitstamp-healthy clean days in `clean_22study_v2`** (2026-10-18/19/20). The published `cf5hz`
feed only began ~15:58 UTC 2026-10-24 (<1 day — too little for a walk-forward) and
`clean_prelim6` Bitstamp is corrupted on 10-22/23, so the **replica** over these 3 clean days is
the available real-data eval. The index is the committed `kbtc.index` BRTI replica reconstructed at
**1 Hz** (258 812 prints over 3 days; per-day coverage 100% / 100% / 99.6%; 388 gapped seconds,
max gap 117 s) from **Bitstamp + Kraken L3 only** — a **2-of-7-constituent** replica, NOT the
published BRTI. Driver: `notes/_exp006_scripts/replica_qlike.py` (sandboxed: the uncommitted
`src/kbtc` was rsynced to `/tmp/exp006_sb`, run under `/opt/kbtc/.venv/bin/python -I` with the
sandbox prepended to `sys.path`; `/opt/kbtc/src` untouched). Frozen config: `step_s=5 s`,
`halflife=600 s`, `horizon=900 s`, `forecast_every=300 s`, `target=TSRV(k=5)`.

**Signature plot** (per-second variance vs sampling step, whole 3-day series):

| step | var/sec   | ann. vol |
|------|-----------|----------|
| 1 s  | 2.179e-09 | 0.262    |
| 2 s  | 2.241e-09 | 0.266    |
| 5 s  | 2.387e-09 | 0.274    |
| 10 s | 2.543e-09 | 0.283    |
| 30 s | 2.779e-09 | 0.296    |
| 60 s | 3.110e-09 | 0.313    |

Variance **rises monotonically with coarser sampling** — i.e. the replica index sits in the
**smoothing-bias** regime (depth-weighted-mid / MA low-pass depresses the finest-scale RV), with
**no additive-noise blow-up at the fine end** (a noisy mid would instead spike at 1 s and fall).
This is the DOWN-bias half of the EXP-006 motivation, not the UP-bias half; see caveat below.

**Headline — train 10-18/19, held-out TEST = 10-20, 285 origins:**

| model    | pooled QLIKE | 95% day-CI        | MZ slope | MZ r² |
|----------|--------------|-------------------|----------|-------|
| **model**| **0.2067**   | [0.2067, 0.2067]* | 0.869    | 0.394 |
| trailing | 0.2395       | [0.2395, 0.2395]* | 0.778    | 0.392 |
| prevday  | 0.3508       | [0.3508, 0.3508]* | 1.461    | 0.392 |

QLIKE ratio **model/trailing = 0.863**, **model/prevday = 0.589** (both **< 1 → model better OOS**).
The model also has the best MZ slope (0.869, closest to 1) and r² (0.394). *The per-day block
bootstrap resamples whole days, so with a **single** held-out day the CI collapses to a point — there
is no day-level uncertainty estimate from N=1 (honest small-N limit; see below).

**Supplementary — train 10-18 only, held-out TEST = 10-19 + 10-20, 573 origins** (gives a
non-degenerate, if crude, 2-day block CI; seasonality fit on only 1 prior day):

| model    | pooled QLIKE | 95% day-CI        | per-day (10-19 / 10-20) |
|----------|--------------|-------------------|-------------------------|
| **model**| **0.2479**   | [0.2067, 0.2888]  | 0.2888 / 0.2067         |
| trailing | 0.2808       | [0.2395, 0.3216]  | 0.3216 / 0.2395         |
| prevday  | 1.4207       | [0.3508, 2.4795]  | 2.4795 / 0.3508         |

model/trailing = 0.883, model/prevday = 0.175. **The model beats the trailing baseline on BOTH
held-out days** (0.2888<0.3216 and 0.2067<0.2395) and crushes prevday (the random-walk baseline is
unstable: its 10-19 forecast references the different-regime 10-18, giving QLIKE 2.48).

**Verdict (this real replica data): the estimator beats both baselines out of sample** — modestly
over the strong trailing-RV baseline (~12–14% lower QLIKE, consistent on both held-out days) and
decisively over prev-day RV. **This is a FIRST PASS, not acceptance:** 1–2 held-out days is a point
estimate with no real day-level CI, it is a 2-of-7-constituent replica (not published BRTI), and the
signature plot shows the smoothing-bias (not additive-noise) regime — so the TSRV correction, which
is designed to remove an *upward* additive-noise bias, is not the operative correction here, and
because the forward target also uses TSRV the forecast and proxy share a measure (the proxy-robustness
point already flagged in the caveats). Final acceptance must come from the `cf5hz` published feed over
more days, then red-team.

Repro (box, sandbox): `python -I notes/_exp006_scripts/replica_qlike.py --clean
/opt/kbtc/data/clean_22study_v2 --start 2026-10-18 --end 2026-10-20 --test-days 1
--cache /tmp/exp006_work/s_3day.npz`.

> **This §3 run used the PRE-FIX estimator** (naive-5 s EWMA, TSRV forward target, no κ̂). **Its
> signature plot (5 s 2.39e-9 → 60 s 3.11e-9, monotone rising) IS the Fable finding** that the σ LEVEL
> is ~14% low and the TSRV target is circular. The **QLIKE ranking here (model/trailing 0.863) is the
> number Fable expects to SURVIVE the fix** — it is about EWMA+seasonality, not the level.

### 3b. REVISED estimator re-run — **PRELIMINARY (replica, 3 days, 1 held-out).**

Run on the Ashburn box (`root@5.161.236.203`, read-only on `/opt/kbtc/data`), 2026-10-24, by the
data-engineer (quant-modeler could not reach the VPS). The REVISED (uncommitted, Fable level-fix)
`src/kbtc` working tree was rsynced to a FRESH sandbox `/tmp/exp006_sb2/kbtc`, run under
`/opt/kbtc/.venv/bin/python -I` with the sandbox prepended to `sys.path`
(`kbtc.signals.vol` confirmed resolving to `/tmp/exp006_sb2/kbtc/signals/vol.py`; `/opt/kbtc/src`
untouched). The **same cached 1 Hz replica series** as §3 was reused
(`/tmp/exp006_work/s_3day.npz`, 258 812 prints, clean_22study_v2 2026-10-18/19/20, Bitstamp+Kraken L3,
2-of-7-constituent — NOT published BRTI); it was NOT re-reconstructed (a naive book load OOMs the 8 GB
box). Driver: `notes/_exp006_scripts/run_vol_qlike_cached.py` (loads the npz, runs the revised
items 1–6). Frozen config: `step_s=5 s`, `halflife=600 s`, `horizon=900 s`, `forecast_every=300 s`,
`Δ*=60 s`, `target=coarse_subsampled` (fine grid 1 Hz), `scale_calibrate=True`, `seed=0`.

Two cuts are reported: **PRIMARY** (train 10-18/19, held-out TEST = **10-20**; the pre-registered
split — but its day-level block bootstrap is degenerate at N=1 held-out day) and **SUPPLEMENTARY**
(train 10-18 only, held-out TEST = **10-19 + 10-20**; gives a crude but non-degenerate 2-day block CI
at the cost of a weaker κ̂ fit on 1 training day). Both are PRELIMINARY — acceptance is the cf5hz
published feed over ≥7 days / ≥5 held-out.

**1. Extended signature plot** (per-second variance vs sampling step, whole 3-day series):

| step | 1 s | 2 s | 5 s | 10 s | 30 s | 60 s | 120 s | 300 s | 600 s | 900 s |
|------|-----|-----|-----|------|------|------|-------|-------|-------|-------|
| var/s (e-9) | 2.179 | 2.241 | 2.387 | 2.543 | 2.779 | 3.110 | 3.229 | 3.110 | 2.915 | 3.369 |
| ann vol | 0.262 | 0.266 | 0.274 | 0.283 | 0.296 | 0.313 | 0.319 | 0.313 | 0.303 | 0.326 |

Per-second variance **rises from 5 s and plateaus around 60–120 s** (3.11–3.23e-9), then wobbles at
the coarse end (300–900 s) where few non-overlapping returns remain (noisy). This is the
**smoothing-bias** regime (no additive-noise blow-up at the fine end). **Δ\*=60 s sits just at the
start of the plateau**; Δ\*=120 s would give a marginally larger κ̂. It does NOT fully flatten — the
coarse tail is too noisy on 3 days to pin the plateau precisely.

**2. κ̂ = sig(Δ\*=60)/sig(5 s), fit on TRAINING days only** (walk-forward):
- PRIMARY (train 10-18/19): **κ̂ = 1.292** ⇒ the 5 s σ is **~12.0 % LOW**.
- SUPPLEMENTARY (train 10-18): **κ̂ = 1.196** ⇒ ~8.6 % low (weaker — fit on 1 day).
- **κ̂ by vol tercile {low 1.344, mid 1.221, high 1.326} — range 9.5 % of the mean, < 15 %** → the
  single-scalar assumption holds on the replica (unlike the sim, which wandered > 15 %). ✓

**3. QLIKE horse-race** (coarse_subsampled target at Δ\*, κ̂ applied to model AND both baselines):

PRIMARY (TEST 10-20, 285 origins):

| model | pooled QLIKE | 95% day-CI | MZ slope | MZ r² |
|-------|--------------|------------|----------|-------|
| **model** | **0.2277** | [0.2277, 0.2277]\* | 0.775 | 0.340 |
| trailing | 0.2455 | [0.2455, 0.2455]\* | 0.702 | 0.347 |
| prevday | 0.3166 | [0.3166, 0.3166]\* | 1.302 | 0.338 |

ratio **model/trailing = 0.9275**, model/prevday = 0.7191. \*Degenerate CI (N=1 held-out day).

SUPPLEMENTARY (TEST 10-19 + 10-20, 573 origins):

| model | pooled QLIKE | 95% day-CI | per-day (10-19 / 10-20) |
|-------|--------------|------------|--------------------------|
| **model** | **0.2602** | [0.2277, 0.2925] | 0.2925 / 0.2277 |
| trailing | 0.2681 | [0.2455, 0.2904] | 0.2904 / 0.2277→0.2455 |
| prevday | 1.3135 | [0.3166, 2.3001] | 2.3001 / 0.3166 |

ratio **model/trailing = 0.9708**, model/prevday = 0.1981. **The horse-race SURVIVES the fix but
only narrowly**: the model beats trailing pooled and on 10-20 (0.2277 < 0.2455), but on **10-19 it
LOSES to trailing by a hair** (0.2925 vs 0.2904). The non-circular coarse target + κ̂-on-both shrank
the model's QLIKE edge from the pre-fix §3 (0.863 with the circular TSRV target) to **0.93–0.97** —
real but much smaller, and not consistent across both held-out days. prevday is crushed (as in §3).

**4. Settlement-exact z² LEVEL test (the pricing arbiter), τ∈{60,120,300,600,900} s:**

PRIMARY (TEST 10-20; CI degenerate at N=1 day):

| τ (s) | UNCORRECTED z² (σ bias) | CORRECTED z² (κ̂=1.292) (σ bias) | KS (corr.) |
|-------|-------------------------|----------------------------------|------------|
| 60 | 1.446 (−16.9 %) | **1.120** (−5.5 %) | 0.033 |
| 120 | 1.380 (−14.9 %) | **1.068** (−3.2 %) | 0.040 |
| 300 | 1.390 (−15.2 %) | **1.076** (−3.6 %) | 0.047 |
| 600 | 1.325 (−13.1 %) | **1.026** (−1.3 %) | 0.050 |
| 900 | 1.312 (−12.7 %) | **1.016** (−0.8 %) | 0.056 |

SUPPLEMENTARY (TEST 10-19+10-20; κ̂=1.196; **non-degenerate 2-day block CI**):

| τ (s) | UNCORRECTED z² [CI] | CORRECTED z² [CI] (κ̂=1.196) | σ bias (corr.) | KS |
|-------|---------------------|------------------------------|----------------|-----|
| 60 | 1.483 [1.467, 1.499] | 1.239 [1.226, 1.253] | −10.2 % | 0.044 |
| 120 | 1.356 [1.316, 1.395] | 1.134 [1.100, 1.166] | −6.1 % | 0.031 |
| 300 | 1.395 [1.395, 1.396] | 1.166 [1.166, 1.167] | −7.4 % | 0.033 |
| 600 | 1.379 [1.355, 1.404] | 1.153 [1.132, 1.173] | −6.9 % | 0.023 |
| 900 | 1.432 [1.386, 1.479] | 1.197 [1.158, 1.236] | −8.6 % | 0.029 |

**Headline answer — does the κ̂-corrected z² CI cover 1 at τ=300 and 900 s?** Mixed / NO with a
caveat. κ̂ removes **most** of the σ-level bias everywhere (z² drops from ~1.31–1.48 → ~1.0–1.24; the
uncorrected ~14–17 % σ-underscale that is Fable's finding is confirmed). **With the stronger
2-training-day κ̂=1.292 the PRIMARY point estimates essentially hit 1** (z²=1.076 at τ=300, 1.016 at
τ=900 — a residual σ error of −3.6 % and −0.8 %), **but that CI is degenerate (N=1 held-out day)**, so
no honest coverage statement. The **only non-degenerate CI (SUPPLEMENTARY, weaker κ̂=1.196) does NOT
cover 1** at any τ — it sits ~13–24 % above 1 (residual σ ~6–10 % low). So: **direction and bulk of
the correction are confirmed and large; full calibration to z²=1 is κ̂-estimate-sensitive and is NOT
demonstrated with a CI on this 3-day replica.** KS stats (0.02–0.06) are at/below the 5 % critical
values (≈0.052 at n=680, ≈0.036 at n=1400) → z is approximately normal; the miss is a variance-SCALE
issue (what κ̂ targets), not a shape issue.

**5. BM-shape flatness** (σ²_point = V(τ)/τ, σ²_avg = V_A(τ)/(τ−2w/3); w=60 s):

| τ (s) | 60 | 120 | 300 | 600 | 900 |
|-------|----|----|-----|-----|-----|
| σ²_point (PRIMARY, e-9) | 4.245 | 4.623 | 4.336 | 4.760 | 4.540 |
| σ²_avg (PRIMARY, e-9) | 4.622 | 4.451 | 4.512 | 4.626 | 4.691 |

Both **flat in τ and ≈ each other** (≈4.2–4.8e-9 primary; ≈3.6–3.9e-9 supplementary) → the BM +
settlement-averaging `σ²(τ−2w/3)` / `σ²τ³/(3w²)` model **holds; NO fixed c(τ) table is needed**. (The
~4.5e-9 test-day point variance sits above the 60 s signature value 3.11e-9 — it reflects the held-out
day's realized activity level, and is itself consistent with the κ̂-lifted scale ≈1.29·3.11 ≈ 4.0e-9.)

**6. Full Δ\*×half-life sensitivity** (QLIKE ratio **model/trailing**, coarse target, κ̂ on both):

PRIMARY (TEST 10-20):

| Δ\* \\ half-life | 300 s | 600 s | 1200 s |
|------------------|-------|-------|--------|
| 30 s | 0.9481 | 0.8795 | 0.8622 |
| **60 s** | 0.9851 | **0.9275** | 0.9098 |
| 120 s | 1.0323 | 0.9724 | 0.9424 |

SUPPLEMENTARY (TEST 10-19+10-20):

| Δ\* \\ half-life | 300 s | 600 s | 1200 s |
|------------------|-------|-------|--------|
| 30 s | 0.9694 | 0.9026 | 0.8957 |
| **60 s** | 1.0092 | **0.9708** | 0.9858 |
| 120 s | 1.0522 | 1.0210 | 1.0280 |

**The model's QLIKE edge is NOT robust across the grid.** It improves with a LONGER half-life and a
SMALLER Δ\* (best cell Δ\*=30/hl=1200: 0.862/0.896) and REVERSES (ratio > 1, model loses) at
Δ\*=120 / short half-life in both cuts. At the frozen cell (Δ\*=60, hl=600) the ratio is 0.928
(primary) / 0.971 (supp). Caveat: changing Δ\* changes the target measure, so cross-row comparisons
are not apples-to-apples; the clean read is per-row (fixed Δ\*, vary half-life), where longer
half-life consistently helps.

**Verdict (PRELIMINARY).** The Fable level-fix behaves as designed: the signature plot confirms the
smoothing regime, κ̂≈1.2–1.3 is stable across vol terciles (<15 %), and κ̂ removes the bulk of the
σ-underscale (z² ~1.3–1.48 → ~1.0–1.24). **But (a) z²=1 with CI coverage is NOT demonstrated** — the
one non-degenerate CI still sits ~6–10 % high, and the near-perfect PRIMARY point estimate rides on an
N=1-day degenerate CI and a κ̂ that differs from the 1-train-day κ̂ by ~8 %; and **(b) the QLIKE
horse-race survives but much more weakly than pre-fix** (0.93–0.97 vs 0.863, and trailing edges the
model on 10-19). BM-shape holds (no c(τ) table). This is a 2-of-7-constituent replica over 3 days /
1–2 held-out — a point estimate, not acceptance. **Real acceptance = the cf5hz published feed over
≥7 days / ≥5 held-out**, then red-team.

Repro (box, sandbox): `python -I notes/_exp006_scripts/run_vol_qlike_cached.py --sandbox
/tmp/exp006_sb2 --cache /tmp/exp006_work/s_3day.npz --test 2026-10-20` (and
`--test 2026-10-19,2026-10-20` for the supplementary 2-day-CI cut).

## Honest caveats

- The κ̂-corrected replica numbers are not yet produced (box re-run PENDING); the simulation can be
  made to look as good as its assumptions.
- We have only a handful of clean days; a walk-forward with 1–3 held-out days is a point estimate with
  wide/degenerate day-level CIs. Not a final number — acceptance is the published feed, ≥7 days.
- κ̂ is one scalar per test day; the sim shows it can wander >15% across vol terciles, so the level
  correction is approximate. The z² level test (not QLIKE) is the arbiter of whether it is good enough.
- **Circularity resolved:** the forward target is now `coarse_subsampled` at Δ\* (a different measure
  from the 5 s forecast), not TSRV. `tsrv()` is kept only for a possible additive-noise regime on the
  5 Hz feed (the signature plot decides). The replica regime is smoothing-bias, not additive noise.
- Δ\*=60 s is pre-registered as the flattening scale; the replica signature may not fully flatten by
  60 s — the extended table (to 900 s) and the Δ\*×half-life sensitivity are reported to show it.

## For quant-tutor (derivation note requested)

Formalize in `docs/derivations/`: (1) the **smoothing (down) bias of a depth-weighted mid** vs the
**additive-noise (up) bias**, why the signature plot's SIGN diagnoses which regime we are in, and why
the κ̂ = sig(Δ\*)/sig(step) LEVEL correction is the right fix for the smoothing regime (vs TSRV, whose
linear-in-n additive-noise model is misspecified when the autocorrelation range exceeds 60 s); keep
the TSRV bias-cancellation derivation for the additive-noise regime. (2) Why a **multiplicative**
intraday factor is the right variance deseasonalization and its seasonal integral in
`expected_forward_variance`. (3) The exact EWMA-per-second-variance → pricing-σ → settlement-averaging
`τ`-correction mapping, and why the **z² LEVEL test** (not QLIKE, which is scale-invariant across a
shared-measure proxy) is the pricing-relevant calibration — the `1/sd` near-ATM/near-expiry blow-up
means a 14%-low σ is ~2.5–3.4¢ of systematic mispricing near |z|=1.

## Red-team review
**PENDING.** Does not count; ROADMAP 3.1 not ticked until (a) the κ̂-corrected re-run (§3b) is run on
the box and its z² CIs + QLIKE filled in, and (b) red-team PASS.

## Conclusion & next step
Estimator + QLIKE harness + the Fable level-fix built, pure/causal, **456 tests green**. The pre-fix
box run (§3) confirmed the QLIKE **ranking** (model/trailing 0.863 OOS) AND surfaced the σ **LEVEL**
bias (smoothing-regime signature, ~14% low); the revised estimator adds the κ̂ level correction, a
non-circular coarse-subsampled target, and the settlement z² LEVEL test. The simulation (§2) confirms
κ̂ moves z² from ~1.5 toward ~1.0 while the ranking survives. **Next:** run `run_vol_qlike_onbox.py`
(§3b) on the VPS — replica now, cf5hz once ≥7 days — fill in the z²/QLIKE/sensitivity tables, hand to
red-team; then quant-tutor derivation note; then 3.2 wires this (level-calibrated) σ into
`pricing.digital_up` for the Brier-vs-Kalshi-mid test.
