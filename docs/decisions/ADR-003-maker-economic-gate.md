# ADR-003: FV acceptance = non-inferiority; the real gate is maker economics (4.2)

- **Status:** accepted (2026-10-29; Berk approved "adopt Fable's plan in full" + "start the 4.1
  simulator now"). Grounded in the red-team-passed Phase-3.2 day-7 result and the P0a/P0b
  measurement (both 2026-10-28, preliminary — 5 CF-feed days, 3 full).
- **Updates:** the acceptance metric of [ADR-002](ADR-002-replica-role-and-metric.md). ADR-002's
  *direction* (price off the observed published feed; express replica accuracy in FV-cents per
  (τ,|z|); real gate = incremental skill vs the Kalshi mid) stands. This ADR corrects **what counts
  as passing** and **where the economic verdict is rendered**.

## Context (what we now know, that we did not when ADR-002 was written)

1. **3.2 headline, red-team PASS (REV of 2026-10-28):** priced off the live published CF BRTI feed,
   our settlement-averaging digital fair value is **well-calibrated and ties-to-slightly-worse than
   the live Kalshi mid** out-of-sample. Paired Brier-diff (model − mid) = **+0.00288**, per-day
   block-bootstrap 95% CI `[~0, +0.0058]`; log-loss diff +0.0083 `[+0.00002, +0.0178]`. The entire
   shortfall is **ATM in the final 1–3 min**; long-τ and off-money buckets are flat. Verified causal,
   no look-ahead; the miss is real (worse 4/5 days). A `synced` normalizer bug that had produced a
   spurious "beats" was found and discarded (the earlier −0.0824 is an artifact; raw data intact).
2. **P0a — the "safe-to-quote region" is essentially empty.** Kalshi's KXBTC15M book quotes at the
   **1¢ tick, often 0.1¢ sub-cent**, two-sided, across the whole tradeable window (verified on 2.94M
   book states). Our FV's implied RMS error bound is **3–16¢** for every |z|<2 bucket — an order of
   magnitude wider than the spread. There is no spread cushion to absorb FV error.
3. **P0b — our disagreement with the mid is anti-informative.** Where |FV − mid| > 3¢ the model is
   **clearly worse** (dBrier +0.0098, CI firmly > 0). The wide-spread "we *are* the mid" regime that
   would reward a stand-alone FV **does not occur** (n ≤ 1) in the tradeable window.
4. **Two framing facts.** (a) A strict superiority gate (CI upper < 0) over the mid on a 3–5-day
   block bootstrap is **near-unpassable by construction** — wrong bar for *FV acceptance*; it is the
   right bar for a *new signal's incremental* value over the current FV. (b) KXBTC15M is **maker-free
   with no rebate** (facts.md); the taker pays ≈ 0.07·p(1−p) (~1.75¢ ATM). So maker P&L per fill =
   **half-spread captured − adverse selection, conditional on being filled** — a fill-conditional
   markout, not an unconditional Brier average.

## Decision

1. **FV (Phase-3) acceptance is NON-INFERIORITY, not superiority.** The fair value is accepted when,
   in the **quote region** (exclude the endgame no-quote cells), it is **non-inferior to the mid**
   (paired Brier-diff CI includes/below 0) **and** its per-(τ,|z|) cents-error bound is reported
   against the observed half-spread. Superiority over the mid is **retired as a Phase-3 gate**.
2. **Superiority is reserved for *incremental* tests.** Any new signal (e.g. an index lead-nowcast,
   or crypto OFI/TFI) must beat the **current FV** incrementally, nested (`FV_new = FV + adj`), with
   pre-registered endpoint + placebos (shuffle / sign-flip / lag) + market-block bootstrap — not beat
   the mid.
3. **The pass/fail economic gate moves to Phase 4.2: fill-conditional markouts.** Edge is proven iff
   net markout per fill (**half-spread captured − adverse selection**) ≥ 0 with a market-block
   bootstrap CI over **≥7 OOS days**, under back-of-queue fills, explicit latency, and exact fees.
   This is produced by the Phase-4.1 queue-aware simulator (build approved 2026-10-29) and the
   recorded-trade markout study (P0d). No economic edge may be claimed before it runs.
4. **Shelve crypto-flow drift (3.3) to day ≥14**, contingent on the P0c lead-attribution regression
   leaving unexplained residual. The near-term "drift" object is a **≤2-parameter index lead-nowcast**
   (priors fixed from EXP-005's ~2 s lead), whose role is **adverse-selection defense** (pull/widen
   faster) — not a minute-horizon forecast. OFI predictive power decays in seconds.
5. **Widen the no-quote region to the data.** P0a shows |z|<1 stays unsafe out to **≥180 s** (and
   |z|<0.5 further), wider than the 60–90 s heuristic in the research brief. Treat ≥180 s at |z|<1 as
   the working pull-zone, pending re-confirmation on ≥7 days.

## Consequences

- **ROADMAP 3.2** is **not** ticked on "beats the mid." The fair value is recorded as *built, tested,
  well-calibrated, non-inferior to the mid in the quote region* — the economic verdict is deferred to
  4.2, not asserted at 3.2.
- **The project's central risk is now explicit and measurable:** on a 1¢-tick efficient market, a
  better fair value is *not* the edge. If 4.2 markouts show the captured spread does not cover
  adverse selection across the quotable region, **KXBTC15M has no maker edge for this approach** —
  and that is a valid, publishable negative outcome, reached honestly rather than curve-fit away.
- Phase-3.1 (vol) acceptance and a 3.2 re-run under this non-inferiority bar both wait for the ≥7-day
  CF feed (~Oct 30–31).
- The `synced` normalizer defect is filed for fix (consumers meanwhile use the live-book semantics).

## Open items / UNVERIFIED

- **P0c / P0d** (lead-attribution regression; recorded-trade adverse-selection markouts) — running;
  pending red-team. P0c decides lead-vs-flow (confirms or reopens shelving 3.3).
- **4.1 design:** whether faithful queue-position simulation is reconstructable from the recorded
  Kalshi full-depth deltas (TOB keeps only level-0). Load-bearing; being resolved in the 4.1 build.
- All P0/3.2 numbers are **preliminary** (5 CF-feed days). The non-inferiority and markout gates are
  only credible on ≥7 independent days with market-block CIs.
- Licensing on the relayed CF data (internal use; never redistribute) — carried from ADR-002.
