# EXP-008: Maker economic diagnostics (P0a–P0d) on day-7 data

- **Date / author (agent):** 2026-10-28 / quant-modeler (lead-delegated), red-team reviewed.
- **Status:** DIAGNOSTICS, PRELIMINARY (5 CF-feed days, 3 full). Red-team verdicts folded in below.
  No ROADMAP tick. Zero fitted parameters. Motivated by [ADR-003](../../docs/decisions/ADR-003-maker-economic-gate.md):
  since the fair value cannot out-quote the mid on this tight market, any maker edge must come from
  spread capture + the no-fee regime + low adverse selection — these measure whether it exists.

## P0a / P0b (safe-to-quote + conditional Brier) — see EXP-007 day-7 section
- Kalshi KXBTC15M quotes at the 1¢ tick (often 0.1¢ sub-cent) across the tradeable window; FV
  cents-error bound 3–16¢ ⇒ **no FV-accuracy edge**, safe-to-quote region empty. Disagreement with
  the mid is anti-informative. The wide-spread "we are the mid" regime is absent (n ≤ 1).

## P0d — adverse-selection markouts (10.1M hypothetical fills, 387 markets, 5 days)
Every passive side of every recorded Kalshi trade is treated as a hypothetical maker fill at the
trade price; markout sign > 0 = maker profit; net `mkS` = half-spread captured − adverse mid-move to
settlement. Causal (book as-of recv_ts before trade; markout mids after; settlement = authoritative
`result`); model-free (FV/σ only enter z-bucketing + E1b).

**Red-team: PASS-WITH-CAVEATS — valid ONLY as a frictionless, 100%-fill UPPER BOUND. Not evidence of
a tradeable edge.**

Two decisive facts settled by review:
1. **Maker fee = $0 on KXBTC15M** (re-verified 3 ways: facts.md primary fetch 2026-10-16; `fees.py`
   maker_fee→$0.00; `backtest/feeapply.py` MAKER_FEES_ENABLED=False, confirmed on real fills by the
   4.1 review). The P0d agent's comparison to a **1.75¢ hurdle was WRONG** — 1.75¢ is the *taker* fee
   at P=0.5, not the maker's cost. Corrected reading: the maker pays $0, so the dataset shows
   **net-positive frictionless spread capture (~+0.2 to +0.6¢/fill)**, not a loss.
2. **It is a frictionless upper bound.** It assumes 100% fill, zero queue, zero latency. A real maker
   is back-of-queue and fills only the subset of trades that sweep the queue ahead — the larger,
   more-informed sweeps. So the "adverse selection is tiny (~0.2¢)" claim does **not** survive
   conditioning on the fills a back-of-queue maker actually gets, and that conditioning **cannot be
   done in this dataset**. P0d's `hs` (0.31–0.50¢) is a ceiling on capture, not realized P&L.

**Day-level CIs (red-team recompute; the within-day-market bootstrap understated them):**
| τ bucket | per-day mean mkS (¢) | day-boot CI | robust? |
|---|---|---|---|
| [0,60) | +0.57 | [0.00, 0.84] | fragile — 1 of 5 days negative; **headline CI collapses to ~0** |
| [60,180) | +0.19 | [−0.02, 0.28] | no |
| [180,300) | +0.34 | [−0.04, 0.98] | no |
| **[300,600)** | **+0.58** | **[0.28, 0.99]** | **yes — all 5 days positive, sign-test p≈0.03** |
| [600,900) | −0.19 | [−0.76, 0.72] | no (negative) |

→ The signal is **+0.2–0.5¢ with lower bounds hugging zero; fragile on 5 days**; only τ[300,600) is
robustly positive at day level. Not nothing, not a verdict.

**E1b (can FV filter bad fills?) — red-team: FAIL / WITHDRAWN.** The "FV sides-with-aggressor →
maker wins; θ=3¢ avoid-rule is harmful (−3.54¢/fill)" pattern is an artifact of unmodeled 100% fills
+ conditioning on trade moments (itself the AS channel) + binary-settlement lottery noise with
grossly understated CIs. The sign algebra is internally consistent (no bug); the *interpretation* is
unsupported. **Retracted.** Re-pose only on 4.2 sim fills. (Consistent with the separate finding that
FV < mid, but E1b adds no usable fill filter.)

## P0c — lead-attribution regression (ATM short-τ, pre-registered, one pass)
Residual (mid − FV) regressed on a 6-venue equal-weight SPOT composite − published CF (proxy for the
lead; **no perp in this tree**) + a σ-proxy. **Red-team: PASS-WITH-CAVEATS.** Index-lead term weakly
dominates flow, sign-stable positive across days, robust to day-demean and core-4 venue choice — but
**weak** (joint R² ≈ 2.7%, one near-flat day 10-25, no CI, and the `disc` regressor is a proxy bundle
that does not separately identify lead vs the replica-construction gap without a perp).

→ **Confirms the diagnosis** that the ATM FV miss is a ~2 s index-lead error (not crypto flow),
supporting shelving 3.3 — but the lead is a **small lever** (~2% of residual); the bulk of the ~3.6¢
ATM miss is construction/microstructure (matches EXP-005's "construction floor dominates").

## Conclusion / next step
The maker fee is genuinely $0 and the frictionless ceiling is net-positive — the first non-negative
economic signal — **but it is a ceiling, fragile at day level, and the "low adverse selection" claim
is exactly what back-of-queue fills will stress.** The decisive test is the **Phase-4.2 queue-aware
markout** (sim built + red-team-passed in 4.1; driver building): fill only when cumulative aggressor
volume clears the assumed queue-ahead, recompute adverse selection on *that* subset, day-level CIs,
re-run at ≥7 days. Until then nothing here counts as an edge. E1b withdrawn; P0c closes 3.3 for now.

Artifacts (box root@5.161.236.203): `/var/tmp/kbtc_scratch/{run_p0d_day7.py,report_p0d.py,p0c_*.py,
p0d_rows.npz,p0c_*.npz,*.log}`.
