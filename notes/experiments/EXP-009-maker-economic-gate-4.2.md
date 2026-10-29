# EXP-009: Phase-4.2 maker-economic gate — queue-realistic markout (day-7)

- **Date / author (agent):** 2026-10-29 / backtest-engineer (lead-delegated), red-team PASS-WITH-CAVEATS.
- **Status:** RESULT, PRELIMINARY (6 CF∩market days, 1-in-10 market sample = 42 markets). The gate
  statistic and verdict below are red-team-accepted; the **≥7-day literal-ADR-003 re-run is pending**
  (feed hits 7 full days ~Oct 30–31; low risk — this is a negative). No ROADMAP tick.
- **The question (ADR-003):** does a passive maker capture net-positive P&L on KXBTC15M after
  back-of-queue fills, latency, and exact fees? The frictionless upper bound (EXP-008 P0d) looked
  net-positive; this is the realistic test.

## Setup (pre-registered headline config, fixed before running)
`RestAtTouchPolicy` (rest 1 lot at the current best bid each side; pull when τ<180s & |z|<1 per
ADR-003's widened no-quote region), **back-of-queue**, **CONSERVATIVE** cancels (asserted in code on
the accepted path), **100 ms** submit+cancel latency, index = published CF BRTI feed (causal,
recv_ts-stamped), σ = committed 3.1 EWMA (causal; κ on each market's pre-open warmup only). Maker fee
$0 (maker-free, confirmed on real fills). Day-level block bootstrap over the 6 days (each day = one
block), pooled per-fill statistic. 42 markets, 70,730 queue fills.

## Result — NEGATIVE (red-team: SOUND, queue-robust, no look-ahead)
**Net markout per fill (day-level block-bootstrap 95% CI):**
| horizon | mean | median | 95% CI | % fills positive |
|---|---|---|---|---|
| 5 s | **−9.06¢** | −5.35¢ | **[−11.21, −6.55]** | 17.8% |
| 30 s | −9.25¢ | −7.00¢ | [−11.49, −6.58] | 22% |
| settlement | −18.4¢/contract | — | [−27.7, −7.2] | — |

Every horizon's CI is entirely below 0; every one of the 6 days is independently negative; 82% of
fills ≤ 0; no outlier tail (nothing beyond ±50¢) — the negativity is the **bulk**, not the tail.

**Capture fraction 7.6%** (realized back-of-queue 68,845 lots vs frictionless 908,603): a real maker
gets ~1/13 of the passive fills P0d counted, and those are the adversely-selected ones — you reach
the front of the queue mainly inside informative sweeps.

## Robustness — the negative is NOT a queue/markout artifact (red-team-verified)
- **Even the frictionless front-of-queue upper bound is net-negative:** mean5s **−1.34¢** (median
  −0.05¢, 49% positive). So there is **no positive edge even in the best case**; realistic
  back-of-queue amplifies it ~4–5×. **Headline should rest on "no edge even frictionless," not on the
  −9¢ magnitude** (which is queue-amplified).
- **Quiet vs volatile split (frictionless, per market):** the 5 QUIET markets are marginally
  **+0.05 to +0.33¢** (a front-of-queue maker captures ~half-spread when not run over); the 2
  VOLATILE windows are deeply negative even frictionless (−5.26¢, −4.49¢). The net loss concentrates
  in volatile windows and in back-of-queue selection.
- **Markout reference is fresh & correct:** YES-mid LOCF age median 0.00s, p99 <0.1s; the largest
  negatives occur where mids are *freshest* (genuinely fast windows), ticker-scoped, two-sided (no
  crossed/one-sided inflation). Sign and magnitude trustworthy.
- **Latency mechanism physically sound:** a resting quote the market moves through *should* fill and
  that fill *is* adverse; we fill only within the irreducible 100 ms cancel window, never after a
  logical pull completes. **No look-ahead** end-to-end (index/σ/κ causal; settlement = authoritative
  `result`). Per-fill 5s/30s markout is inventory-independent (the uncapped |inv|→2669 lives only in
  summary fields, not the gate statistic).

## Conclusion
**Naive rest-at-touch passive market-making has negative maker edge on KXBTC15M, outside the pulled
endgame — robust to the queue assumption (no edge even frictionless).** This is a valid negative and
is exactly ADR-003's stated central risk: on a 1¢-tick efficient market, a good fair value is not an
edge, and the no-fee/low-apparent-AS setup does not survive realistic queue position.

**This does NOT prove no maker strategy can work** — it tests the *naive* policy with no
adverse-selection defense or inventory control (deliberately, no curve-fitting). The quiet-market
marginal-positive + volatile-window-blowup split is the one thread an AS-defended policy (ADR-003
item 4: faster/earlier pulls, book-imbalance gating, inventory limits, quote only when good queue
position is attainable) would target. Whether that rescues it is the open strategic question.

## Red-team caveats folded / pending
- Headline framed as "no edge even frictionless" (done above). Frictionless markout added (−1.34¢).
- CF-warmup "usable" market exclusion is a benign recording-time selection (noted).
- **≥7-day re-run pending** (~Oct 30–31) to meet ADR-003 literally; low risk for a negative.
- Cosmetic stale docstring in `report.py` fixed.

## Artifacts
Driver `notes/_bt42_scripts/gate_run.py`; result JSON (box) `/var/tmp/kbtc_src_day7/gate_stride10.json`.
Code: `src/kbtc/backtest/{loader,policy,queue,engine,report,markout,events,feeapply}.py`; tests
`tests/test_backtest_*.py` (553 suite pass). Red-team: PASS-WITH-CAVEATS (this file's caveats).
