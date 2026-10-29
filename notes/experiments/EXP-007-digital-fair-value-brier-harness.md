# EXP-007: KXBTC15M digital fair value + Brier-vs-Kalshi-mid harness (ROADMAP 3.2)

- **Date / author (agent):** 2026-10-24 / quant-modeler (lead-delegated).
  Day-7 run + results appended 2026-10-28.
- **Status:** model + harness BUILT & TESTED; evaluation protocol FIXED below **before** any
  result. **Day-7 run executed on the published CF feed (not the replica) — red-team PASSED;
  verdict STOP-AND-RETHINK (see "Day-7 run" section).** ROADMAP 3.2 stays UNTICKED; per
  [ADR-003](../../docs/decisions/ADR-003-maker-economic-gate.md) the beats-the-mid gate is retired
  in favour of FV non-inferiority + a Phase-4.2 economic (markout) gate.

## Hypothesis

The settlement-averaging-corrected digital fair value
`P(up) = P(A_close >= strike | F_t) = Phi(z)` — with `z, E[A_close], Var[A_close]` from
`pricing.settlement_moments` (variance compression `sigma^2(tau - 2w/3)` outside the 60 s window,
`sigma^2 tau^3/(3w^2)` inside; `docs/derivations/settlement-averaging.md`), the realized open-window
average as a fixed strike (ADR-001, derivation addendum §2), and `sigma` from the 3.1 estimator —
produces a **better-calibrated probability than Kalshi's own mid**, measured by a lower
out-of-sample Brier / log-loss, *concentrated in the buckets where the averaging correction bites*
(short `tau`, small `|z|` — where naive pricing is too close to 50c, derivation §5).

**Why it might hold:** the averaging correction is a known, load-bearing edge the market mid may not
price precisely near expiry; our index + strike are observed/reconstructed. **Why it might not:**
the mid aggregates order flow we don't see; our index is a 2-3-constituent replica (not the official
BRTI) with bias (EXP-002: −$1.29) and the 3.1 `sigma` scale is not yet validated on the real feed
(EXP-006: one non-degenerate z² CI sits ~13-24% high → `sigma` ~6-10% low). A mis-scaled `sigma`
directly inflates model Brier, so a NULL / loss is a live outcome — and the gate reports it plainly.

## Data

- **Index Y_t:** BRTI **replica** (committed `src/kbtc/index`) at 1 Hz, reconstructed per market over
  `[open - warmup, close]` (ADR-002: the published `cfbenchmarks_value` feed is primary going
  forward but has < 1 day of history; replica is the backtest source). Bitstamp + Kraken +
  Coinbase-Advanced (the 3 recorded spot constituents).
- **Strike + outcome:** the `settlements` dataset — `open_value` (= `floor_strike` = open-window 60 s
  average = strike) and `result` (YES/NO; == `close_value >= open_value`, ROADMAP 1.2).
- **Kalshi mid:** recorded `kalshi_books` (yes/no), reconstructed as-of each origin.
- **Split:** walk-forward by **calendar day** (`close_time` date). First pass uses our few clean days
  (10-18..10-21) as held-out — labelled **PRELIMINARY** (not >=3-5 independent days; single-market
  `sigma` warmup, no multi-day intraday seasonality). Credible run: >=3-5 days + ideally the feed.

## Method (fixed before results)

- **Fair value** (`src/kbtc/signals/fairvalue.py`): `fair_value_up(Y_t, strike, tau_s, sigma, state)`
  → `pricing.digital_up`. Window-state regimes BOTH handled: pre-window (`tau >= 60 s`,
  `n_observed = 0`), inside-window (`close_window_state` supplies the observed partial log-sum +
  count; `settlement_moments` combines the realized part with the conditional law of the remaining
  prints), and the degenerate at-close (`var = 0` → verified `>=`/ties → YES). The `n_observed` fed to
  `settlement_moments` is derived from `tau` so its consistency invariant always holds.
- **Drift:** **zero (martingale), stated explicitly.** `dX = sigma dW`; the risk-neutral drift of a
  near-driftless no-carry index over 15 min is ~1e-7 in log terms (derivation §2) — negligible vs.
  diffusion and vs. replica/strike error. A predictive short-horizon drift (perp lead / OFI) is a
  **3.3 signal, OUT OF SCOPE here**; `fair_value_up` exposes `drift_per_s=0.0` as a documented, OFF
  hook (so the assumption is visible, not hidden) that 3.3 can justify by *incremental* Brier later.
- **sigma:** committed 3.1 estimator (`signals.vol.estimate_vol_path`), causal, with the level
  correction `kappa` calibrated on the pre-open warmup only. (Scale caveat per EXP-006 carried.)
- **Scoring** (`src/kbtc/signals/brier.py`): Brier `(p-y)^2` + log-loss (clipped) for **model vs
  Kalshi mid** against the realized YES/NO, **per (tau-to-close, |z|) bucket**, with **per-day
  block-bootstrap 95% CIs** and a **per-day-paired head-to-head** Brier-difference CI. Calibration
  table + reliability plot (predicted vs realized frequency).
- **GATE (pre-registered, ROADMAP 3.2):** computed on the **single pooled τ≥60 s head-to-head**
  (REV-036 F1). The model beats the mid OOS **iff** the upper bound of the per-day-paired
  `Brier(model) - Brier(mid)` 95% CI is `< 0` **AND** there are ≥3 held-out days (F3 — a single
  degenerate day cannot green-light). If not — straddling 0, lower bound `> 0` (worse), or <3 days —
  the harness prints `STOP-AND-RETHINK` / `NOT GREEN-LIT`; that is the honest outcome, not a failure
  to hide. In-window (0<τ<60 s) origins are a **DIAGNOSTIC only**, and τ=0 is dropped (F6). The 25
  per-(τ,|z|) buckets are **DIAGNOSTIC only** (no multiplicity control, F5). Bucket edges
  pre-registered: `tau` {0,60,180,300,600,900} s, `|z|` {0,.25,.5,1,2,inf}.

## Costs modeled

N/A at this layer — fair value + probabilistic scoring only (no fills/P&L). Fees, back-of-queue
fills, latency enter at Phase 4 (backtester). The mid is the *raw* Kalshi mid (no fee adjustment);
"beat the mid" is a fair-value question, not a tradeable-edge claim (taker edge ~ nil, CLAUDE.md).

## Results

**PENDING the box run** (`notes/_exp007_scripts/study_exp007_brier.py`; clean parquet + settlements
are box-only). Synthetic validation of the machinery is committed and green:
- model: MC-calibration recovers the true P within 0.01 across all four window-state regimes
  (`tau` 900/300/45/30 s, `n_observed` 0/0/15/30) and for a non-default 10×1 s window → the
  `min(u_k,u_l)` variance sum and the regime logic are correct, not just the Gaussian CDF;
  monotone in `Y_t - strike`; ATM-peaked delta; `tau→0` tie → YES.
- harness: head-to-head gate fires `BEATS` when the model is better and `STOP-AND-RETHINK` when it is
  not; Kalshi yes/no mid `= (best_yes_bid + (1 - best_no_bid))/2` reconstructed as-of t with
  synced-only + as-of + missing-side handling; calibration recovers a known frequency; bucketing by
  (tau,|z|) populates. Tests: `tests/test_signals_fairvalue.py` (20) + `tests/test_signals_brier.py`
  (18).

**To produce the PRELIMINARY number on the box:**
```
uv run python notes/_exp007_scripts/study_exp007_brier.py \
    --clean-root <CLEAN> --settle-root <CLEAN> --test <days> --figdir /tmp/exp007
```
Report the gate line, per-bucket Brier diff, and the reliability plot back here, then → red-team.

## Honest caveats (interpret the Brier number only with these)

1. **σ-scale (EXP-006):** the 3.1 σ is likely ~6-10% low on the 2-of-7 replica; a mis-scaled σ
   directly inflates model Brier. First stop-and-rethink candidate if the gate fails.
2. **Replica strike bias (EXP-002, −$1.29):** feeds `ln(strike)` first-order (~2.3¢/bp ATM near
   expiry, derivation §3). The credible run wants the published feed.
3. **In-window flattering (REV-036 F1):** for 0<τ<60 s the model mechanically OBSERVES the forming
   settlement average and converges to the realized 0/1 — that would FLATTER a pooled "beats mid"
   without being skill, exactly in the [0,60) bucket where we pull quotes anyway. The **headline gate
   is τ≥60 s only**; the in-window regime is reported as a diagnostic (and τ=0 dropped, F6).
4. **Crossed Kalshi books (REV-036 F4):** a crossed book's "mid" is not a meaningful benchmark;
   `yes_mid_at(skip_crossed=True)` (harness default) drops those origins (disclosed as a missing
   quote). A locked book (zero spread) is kept. `skip_crossed=False` keeps the raw mid and discloses
   it instead.
5. **Few, autocorrelated days + single-market σ warmup** — PRELIMINARY, not a verdict.

## Day-7 run (2026-10-28) — RESULTS (red-team PASSED; PRELIMINARY, 5 CF-feed days)

Run on the **published CF BRTI feed** (the ADR-002 primary source; now 5 days recorded) rather than
the replica — a better level source than this note originally anticipated. Live Kalshi mid
reconstructed as-of each origin. Headline gate: τ≥60 s, in-window excluded, drift OFF, 5 held-out
CF-feed days (3 full: 10-25/26/27; 10-24 and 10-28 partial), n≈23,922 origins.

**VERDICT: STOP-AND-RETHINK — the fair value does NOT beat the live mid OOS.**
- Paired Brier-diff (model − mid) = **+0.00288**, per-day block-bootstrap 95% CI `[~0, +0.0058]`
  (model marginally worse). Log-loss diff +0.0083 `[+0.00002, +0.0178]`. Model well-calibrated
  (reliability tracks the mid's). Worse 4/5 days; robust to dropping the worst day.
- Shortfall concentrates **ATM in the last 1–3 min** ([60,180)×|z|<1 ⇒ Brier +0.020..0.026); long-τ
  and off-money buckets flat (±0.002). Mechanism: a ~2 s index-lead error amplified by binary delta
  (matches EXP-005), **not** a drift deficiency and **not** a regime a maker quotes in.

**Critical benchmark bug (CONFIRMED by red-team).** The first pass printed a spurious "Model BEATS
the mid" (−0.0824). Root cause: the day-7 Kalshi TOB normalizer (commit 80e5d3a) computes `synced`
from a **per-market** sequence-gap check, but Kalshi shares **one seq across all markets** on a
subscription, so `synced` false-trips ~32 s after each market opens and the default
`require_synced=True` **freezes the reconstructed mid ~14 min stale**. The model was "beating" a
frozen mid. With `require_synced=False` the mid is live/correct → the STOP verdict above. The book
*data* is correct regardless; only the `synced` annotation is wrong. No raw re-recording needed; the
fix (per-sid seq / rename the column / standardize consumers on live semantics) is filed separately.

**P0a/P0b measurement (2026-10-28, pre-red-team), decisive for the gate reframe:**
- **P0a — safe-to-quote region is essentially EMPTY.** Kalshi quotes at the 1¢ tick (often 0.1¢
  sub-cent, verified on 2.94M book states) across the live window; FV cents-error bound (√dBrier×100)
  is 3–16¢ for all |z|<2 — no spread cushion. |z|<1 stays unsafe to ≥180 s; |z|<0.5 further.
- **P0b — disagreement is anti-informative.** |FV−mid|>3¢ ⇒ model clearly worse (dBrier +0.0098, CI
  > 0). Wide-spread "we are the mid" regime absent (n ≤ 1).
- **Implication → [ADR-003](../../docs/decisions/ADR-003-maker-economic-gate.md):** on this tight
  market the edge cannot come from FV accuracy; FV acceptance becomes non-inferiority, and the
  pass/fail economic gate moves to Phase-4.2 fill-conditional markouts (spread − adverse selection).

Artifacts (box root@5.161.236.203): `/var/tmp/kbtc_scratch/{run_brier_day7.py,results_day7_live.log,
run_p0_day7.py,results_p0_day7.log}`; clean parquet `/var/tmp/kbtc_clean_day7/`.

## Red-team review

REV-036 PASS-WITH-CAVEATS on the model + harness; the F1/F2/F3/F4/F5/F6/F7 fixes above are folded in.
A `quant-tutor` derivation note (mechanics → derivation → implication) for the window-state
combination and the martingale-drift choice is still to follow.

## Conclusion & next step

Model + harness are the deliverable and are built/tested. Next: run EXP-007 on the box for the
PRELIMINARY Brier-vs-mid (replica index), label it, and gate it; a credible verdict needs >=3-5
independent held-out days and ideally the published `cfbenchmarks_value` feed. **Do not tick
ROADMAP 3.2 until the model beats the mid OOS on enough days.** If it does not, that is a
stop-and-rethink (candidates: `sigma` scale per EXP-006, replica strike bias per EXP-002, or the
drift assumption → 3.3).
