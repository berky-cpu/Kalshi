# EXP-003 (DIAGNOSTIC, not a result): where does the BRTI-replica negative bias come from?

- **Type:** DIAGNOSTIC. **No acceptance number, no p95 claim, no model/code change committed.**
  This note root-causes the structural bias REV-027 confirmed is benign (EXP-002), to shape a
  FUTURE experiment. All what-ifs are **TRAIN-only** (2026-10-18 post-18:00 + 2026-10-19).
  **TEST (2026-10-20) was deliberately NOT touched** — not loaded for any decomposition or sweep.
- **Date / author (agent):** 2026-10-20, index-replicator.
- **Question (from the lead):** REV-027 attributed EXP-002's −$1.29 TEST bias (concentrated in
  high realized vol — volq4 −$3.83 — and hours, e.g. h=15 −$7.16) to *benign forward-fill lag +
  missing venues*. Red-team's stated mechanism: the §5.1 forward-fill up to the 10 s staleness
  cutoff (`filters.py:55-59`) + receive latency make each venue's book stale on fast moves, so the
  replica lags published BRTI and undershoots. **Quantify how much of the bias is forward-fill-lag
  vs missing-constituents, and whether tightening staleness/latency handling would help.**
- **Setup:** committed `src/kbtc/index/` via the REV-027-verified sandbox `/tmp/kbtc_renorm/kbtc`
  (md5-identical to `src/kbtc/index/`, re-checked). Data `/opt/kbtc/data/clean_22study_v2`
  (Bitstamp/Kraken/Coinbase-Advanced); ground truth `/tmp/exp002/settle_clean` (REV-027's).
  Endpoint = frozen `T-59..T`. Scope = **112 full-coverage close windows** on TRAIN. Scripts
  (uncommitted): `notes/_exp003_scripts/diag_exp003.py` (box), `plot_exp003.py` (local);
  run log `notes/_exp003_scripts/exp003_run.log`; figure
  `notes/figures/EXP-003/exp003_lag_diagnostic.png`.
- **Harness validation:** TRAIN `T-59..T` signed bias = **−$0.343**, which matches REV-027's
  independently-stated TRAIN `T-59..T` bias (−0.343) **to the cent** → my pipeline reproduces
  EXP-002's machinery exactly before I start perturbing it.

## Headline answer

**On the TRAIN days the forward-fill/staleness-lag mechanism is essentially absent, and
tightening the staleness cutoff does nothing. The residual error is a construction-level
discrepancy (missing 4-of-7 constituents + averaging/endpoint alignment), not a lag.**

Four independent lines of evidence, all pointing the same way:

1. **Books are already fresh; the 10 s forward-fill is almost never engaged on moving markets.**
2. **Staleness is anti-correlated with volatility** (ρ ≈ −0.5): books are only old when the market
   is quiet, i.e. exactly when a stale book ≈ the current book.
3. **The direct lag test has the wrong sign.** Forward-fill lag must make error *negatively*
   correlated with the intra-window drift; the observed correlation is **positive**.
4. **Shortening the cutoff 10 s → 1 s moves bias/median/p95 by cents** (and slightly *worsens* the
   median), while costing coverage — a pure downside.

## Q1 — Book age at the sampled second (is the mid built from a stale book?)

Per-venue book age = `T − recv_ts_ns` at each of the 60 one-second ticks (7,200 ticks/venue):

| venue | p50 | p90 | p99 | max | % forward-filled >1 s | % >2 s | % >5 s |
|---|---|---|---|---|---|---|---|
| bitstamp | 0.022 s | 0.150 s | 0.846 s | 4.11 s | **0.8%** | 0.3% | 0.0% |
| kraken | 0.102 s | 0.682 s | 3.33 s | 64.4 s* | **5.3%** | 1.8% | 0.9% |
| coinbase_advanced | 0.060 s | 0.267 s | 0.764 s | 2.01 s | **0.3%** | 0.0% | 0.0% |

*Kraken's 64 s outliers are dropped by the §5.1 staleness gate; **when kraken is actually USED its
age p99 is 2.05 s**. Forward-fill beyond ~1 s is a tail event (≤5% of ticks), and essentially all
of it is Kraken quiet-book gaps, not fast-move staleness. The §5.1 10 s cutoff fires on **55**
kraken ticks total across all 7,200 — it is almost never the binding constraint.

31/112 windows are "all-fresh" (every used tick's max venue-age ≤1 s); 81/112 have ≥1 tick with a
>1 s forward-filled book — but see Q2: those stale ticks sit in **low-vol** windows.

## Q2 — Lag vs missing-venue split

**(a) Staleness happens when nothing is moving.** `max used-venue book age` vs window realized vol:
**pearson −0.41, spearman −0.575.** Higher vol → *more* book updates → *fresher* books. The
worst-|error| windows confirm it: the three largest-drift windows on TRAIN (drift +$45.5, −$45.4,
−$37.2) are **100% fresh, max age <1 s**. The "stale book on a fast move" case barely exists here.

**(b) The direct lag test has the wrong sign.** Define intra-window drift = (last − first used
print). Forward-fill/receive-latency lag reads an *older* price, so on an up-move the replica should
read *low* → **negative** error; the lag hypothesis predicts a **negative** err-vs-drift slope.
Observed:

- `pearson(err, drift) = +0.30`, OLS `err = +0.034·drift − 0.46` → **positive** slope.
- up-drift (>+$20) windows bias **+1.21**; down-drift (<−$20) windows bias **−1.82**; flat −0.47.

The replica sits *further in the direction of travel* than the published average — the **opposite**
of forward-fill lag. (This smells like a sub-second window/averaging-endpoint alignment offset, not
staleness — see recommendation.)

**(c) Error magnitude does not scale with book age or vol.** Over the 112 windows: `|err|` vs
`max_used_age` pearson −0.13 / spearman −0.08; `|err|` vs rvol ≈ 0; `|err|` vs `|drift|` ≈ 0. If
forward-fill lag drove the error, |err| would grow with age and with move size. It does neither.

**(d) The "floor" (missing-venue + construction) is small on TRAIN.** Overall TRAIN bias is only
−$0.34, and it is **not** monotone in vol (volq0..4 bias +0.25, −1.23, −0.90, −0.05, +0.16) — a
static missing-venue offset of a few tenths of a dollar plus two-signed window noise, not a vol-
scaling lag. The dramatic EXP-002 concentration (volq4 −3.83; h=15 −7.16) **does not reproduce on
TRAIN**: TRAIN per-hour biases are small-n (n=3–8/hour) and both-signed (h=13 −3.77 *and* h=21
+2.97, h=20 +2.30, h=04 +2.62). This is consistent with REV-027's caveat that EXP-002's TEST was a
**single autocorrelated day (10-20)** — much of the "structural high-vol/h=15 bias" is a one-day
sampling artifact, not a reproducible structure.

## Q3 — Would tighter staleness/latency handling help? (TRAIN-only what-if)

Re-ran the 60-tick average over the **same 112 full-cov@10s windows** with the §5.1 Retrieval Lag
Threshold shortened (spec default = **10 s**; tighter = a deliberate divergence from methodology):

| cutoff | bias | med \|e\| | p95 \|e\| | max \|e\| | full-cov windows | mean n_gap |
|---|---|---|---|---|---|---|
| 10 s (spec) | −0.343 | 2.150 | 5.863 | 8.710 | 112 | 0.00 |
| 5 s | −0.342 | 2.150 | 5.863 | 8.710 | 111 | 0.00 |
| 2 s | −0.357 | 2.175 | 5.868 | 8.710 | 111 | 0.00 |
| 1 s | −0.355 | 2.235 | 5.868 | 8.710 | 111 | 0.00 |

**Bias moves by ~1 cent; median gets slightly worse; p95/max don't move.** On the top-vol quintile
(where lag would bite most) it is equally inert (bias +0.164 → +0.178, p95 5.472 → 5.544). The
per-window shift histogram (figure, panel 4) is a spike at 0 (mean −$0.01, median |shift| $0.025).
**Tightening the staleness cutoff does not help** — it trades a sliver of coverage for a slightly
worse median and no bias/p95 gain, because the books are already fresh. (An "align to freshest
common book" variant is the same family — it can only drop already-fresh ticks here, so it would
behave like the 1–2 s cutoff: marginal at best.) **Flag: this is bias-for-coverage with no payoff.**

## Q4 — Is residual Bitstamp self-crossing skewing composition on fast moves?

On TRAIN, Bitstamp `self_crossed` drops total **532 ticks** (vs EXP-002's 1,902 over all 241
markets); per-window drop-fraction p50 = 0, p90 = 0, **max 0.50**, only **2 windows** ≥25%.
Correlations: `corr(drop_frac, rvol)` pearson +0.06 / spearman +0.02 (**no** clustering in high
vol); `corr(drop_frac, err)` ≈ 0; `corr(drop_frac, |err|)` −0.08. The 2 high-drop windows actually
have **+$1.9** bias. The low-drop (<5%) windows (n=110) carry the whole −0.38 bias. **Residual
Bitstamp self-crossing is not skewing venue composition in a bias-relevant way, and does not
cluster on fast moves** — REV-027's second-order worry does not bite on TRAIN. (Still correctly
dropped; benign data-quality handling.)

## What I could NOT separate / limitations

- **Missing-venue vs construction/alignment cannot be fully split here** — I don't record the other
  4 constituents, so I can bound the *lag* contribution (≈ zero) but not prove the residual is
  venues-vs-endpoint. The positive err-vs-drift slope points at an averaging/endpoint alignment
  component that is *not* a missing-venue level offset.
- **TRAIN = 2 autocorrelated days** (single BTC path); n-per-hour is 3–8. Treat the TRAIN
  decomposition as mechanism evidence, not population statistics.
- I did not re-derive the EXP-002 TEST structure (TEST kept untouched); the claim "much of the
  high-vol/h=15 concentration is single-day" is inferred from its non-reproduction on TRAIN.

## Recommendation — what closing p95 < $5 actually needs

**Answer to (a) constituents / (b) lag-staleness fix / (c) both: it is (a) + construction/
alignment, NOT (b).** A lag/staleness fix is empirically a dead end here (Q1–Q3). The next proper
experiment should test, in order:

1. **Add the 4 missing BRTI constituents** (Gemini, LMAX Digital, Crypto.com, Bullish — brti-spec
   §1) and re-run the tracking study. This is the only lever that can change the consolidated-book
   *composition/level*, which is where the residual floor lives.
2. **Investigate the averaging/endpoint alignment** (the +0.30 err-vs-drift slope). It suggests a
   sub-second offset between our `T-59..T` integer-second sampling and Kalshi's actual 60-print
   window (O-5/O-6: which prints, 1 Hz vs 200 ms). Test a fractional-second/200 ms-grid window and
   regress err-vs-drift toward zero — on TRAIN, report OOS.
3. **Do NOT spend effort shortening the staleness cutoff or adding a latency correction** — Q3 shows
   it buys nothing and costs coverage. Keep the spec's 10 s.
4. **Get ≥3–5 independent held-out days** before quoting any population p95 — EXP-002's TEST is one
   day and its vol/hour concentration largely does not reproduce on TRAIN.

**Bottom line:** the −$1.29 remains benign, but its benign cause is **missing constituents +
averaging alignment**, not forward-fill lag. The red-team's stated lag mechanism is not supported
on TRAIN; "just tighten staleness" would not move the number.
