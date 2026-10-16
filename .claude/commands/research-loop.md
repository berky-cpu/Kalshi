---
description: Run a hypothesis-driven research loop (modeler proposes, backtester tests, red team attacks) for N rounds
argument-hint: "<hypothesis> [rounds=3]"
---

Hypothesis: $ARGUMENTS

For each round (default 3, stop early on a clear negative or on a PASS):
1. `quant-modeler`: write/extend an EXP note in notes/experiments/ — hypothesis, data split
   (walk-forward by day), metrics, and the benchmark (Kalshi mid). Implement the minimal code.
2. `backtest-engineer` (if the hypothesis concerns P&L) or `quant-modeler` (if it concerns
   forecast quality): run the evaluation exactly as pre-registered.
3. `red-team-reviewer`: attack the result. Record the review.
4. Lead: decide — refine (next round, stating what changed and why), abandon (record the
   negative result; negatives are valuable), or promote (open a ROADMAP item).

Never change the test split between rounds. Count every variant tried and report it.
End with a summary table: round, change, metric vs. benchmark, CI, verdict.
