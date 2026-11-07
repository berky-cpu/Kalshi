# EXP-010: Candidate-2 — resting-NO on single-name/mention markets (KILL)

- **Date / author (agent):** 2026-10-29 / backtest-engineer (lead-delegated), red-team PASS.
- **Status:** RESULT, red-team-passed. Post-conclusion opportunity check (after ADR-004); a KILL.
  No ROADMAP tick. Runs local-only (VPS deleted); historical data pulled from the free public
  Kalshi REST API.

## Claim tested
Bartlett & O'Hara (2026, NBER, 41.6M Kalshi trades): on single-name / "mention" markets takers
overbuy YES, so a passive maker resting **NO** nets **+1.91¢/contract, 63.4% win** (paper ignores
fees and uses idealized fills). Adversarial question: does it survive back-of-queue fills + fees +
the recent regime + fat-tail concentration?

## Data / method
Free public REST (`api.elections.kalshi.com/trade-api/v2`): KXTRUMPMENTION (flagship: 1,513 settled
markets, 891,753 trades, all 2026) + a breadth basket (Vance/Mamdani/earnings-NVDA/TSLA, 801
markets). Passive resting-NO, held to settlement, maker-free (mention series `fee_type=quadratic` →
$0 maker). Reused the reviewed `src/kbtc/backtest/queue.py` + `fees.py`. REST gives only the trade
tape + settlement, so `queue_ahead` is ASSUMED, swept over {0,5,20}. Two fill models: generous
multi-level (rest at every traded level, no pick-offs — the pre-registered gate) and honest
single-entry causal (one order at the opening-clear NO price, fill only from strictly-later YES-taker
flow, INCLUDE trade-through pick-offs). Pre-registered gate Q=5/size=5, event-block bootstrap; KILL
if net <+1¢ OR >70% of net in top-5% markets OR recent sub-sample ≤0.

## Result — KILL
- **Frictionless reproduces the paper:** +1.95¢ (Trump) / +2.57¢ (basket) vs their +1.91¢ — validates
  the fill plumbing against an external benchmark.
- **Honest single-entry causal:** Trump **−3.2¢/contract** (−4.9¢ recent half, CI below 0); basket
  +3.05¢ but CI [−1.97,+8.00] spans zero. **Queue-robust:** negative/non-significant at Q=0 (front)
  through Q=20. ~half of markets net-negative; median market loses.
- **Concentration kill fired:** top-5% of markets = **155% of net P&L** (Trump) / 107% (basket) —
  strip them and it's negative. Net is a thin residual of large offsetting flows, not a broad edge.
- **Regime shift (real, from recorded settlements):** these markets now settle YES **~41–46%** vs the
  paper's 30.9%, while taker YES-volume share is still ~66% — the NO-side premium the paper harvested
  is largely gone, and the honest model is *more* negative in the recent half.
- **No static mispricing:** the NO price is monotone-informative across deciles (behaves like a
  calibrated probability); walk-forward calibration causal.

**Red-team PASS (guarding against a FALSE negative):** actively searched for a surviving config; the
only apparent survivor (basket entry-deciles [3,4,5], +8.9¢) is a **post-hoc multiple-testing
artifact** — 36 independent events, knife-edge (dies when widened one decile), economically
incoherent (its d3 settles NO only 6.2% yet shows +10.9¢), non-monotone across deciles. Not a
defensible edge. Code correct; no sign-changing bug; maker-free handling right.

## Conclusion
Same pattern as KXBTC15M: an idealized edge that evaporates under honest fills. **No real,
harvestable edge for a solo operator — a fat-tailed bet, negative under realistic conditions, and the
regime has moved against it.** Combined with the LIP check (marginal, crowded, doesn't cover our
contract — facts.md), **both post-conclusion candidates failed → the project closes on ADR-004.**

## Artifacts
`src/kbtc/backtest/single_name.py` (+ `tests/test_single_name.py`, 12 tests; full suite 565 pass);
drivers `scripts/experiments/exp010_{download,study}.py`; outputs `notes/_candidate2/result_*.txt`
(raw data gitignored).
