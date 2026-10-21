# EXP-001: BRTI replica tracking error vs Kalshi settlement — first pass (2-of-3 constituents)

- **Date / author (agent):** 2026-10-20, index-replicator (run) + red-team-reviewer (vet).
- **Hypothesis:** the BRTI replica (CF Benchmarks v17.0 reconstruction, src/kbtc/index/) averaged over the
  60×1 s settlement window should track Kalshi's published settlement values (`floor_strike` open-window,
  `expiration_value` close-window) within the acceptance target **p95 |error| < $5**. (Derivation:
  docs/derivations/brti-replica.md + settlement-averaging.md.) Expectation: with only 2 of the spot
  constituents it will likely MISS the target — this pass is to validate the machinery end-to-end on real
  data and locate the error sources, not to hit the target.
- **Data:** Bitstamp `live_orders_btcusd` + Kraken `level3` (the 2 CRC-relevant spot constituents; **Coinbase
  excluded** — its book-path perf bug, now fixed REV-024, stalled its normalize). Kalshi settlement = ground
  truth. Window: markets settling ≥ 2026-10-18 18:00 UTC (clean-data cutoff after the 10-18 incident).
  **Walk-forward DATE split: TRAIN = 2026-10-19 (n=22 full-coverage), TEST = 2026-10-20 (held out, n=30
  full-coverage; day partial).** 191 settled KXBTC15M markets in-scope (96 on 10-19, 95 on 10-20).
- **Method:** `tracking.evaluate_oos` — tune (cap_mean O-4 × median_base O-9 × endpoint O-5) on TRAIN
  windows only by lowest median |error|, FREEZE, report `error_stats` on TEST windows only. Frozen:
  cap_mean=trimmed, median_base=all, **endpoint = T-60..T-1**. Full-coverage gate (venues_used=={bitstamp,
  kraken}); coverage attached to every stat.
- **Costs modeled:** N/A — this is index-tracking error (replica vs published settlement), not a P&L
  backtest. No fees/latency/queue (those enter Phase 4).
- **Results:**
  - **Held-out TEST (10-20), frozen endpoint T-60..T-1:** median **$3.15**, p95 **$8.997**, max **$10.34**,
    signed bias −$1.81, **n=30**, venues_used = 2 (bitstamp, kraken). **MISSES p95<$5.**
    (≈ 0.004% median / 0.011% p95 of an ~$82k index.)
  - In-sample TRAIN (10-19, labeled in-sample): median $2.315 / p95 $6.812 at T-60..T-1 (vs $2.695/$7.463 at
    T-59..T — O-5 weakly favors T-60..T-1 by ~$0.38 median, n=22, suggestive not conclusive).
  - open ≡ close (chained markets: open(N)≡close(N-1)) → same measurements relabeled; NOT independent
    corroboration. O-4/O-9 grid INERT with 2 venues (all configs byte-identical) → effectively a 2-endpoint
    search. Plots: notes/figures/study22b/tracking_{close,open}_TnT.png.
- **Red-team review:** [REV-025](../reviews/REV-025-exp001-tracking-2of3.md) — **VALID (counts), caveated,
  target-missed.** Leak-free split verified (UTC settlement-time date split; chaining handled per
  window_type); p95 honest; coverage labeling sound.
- **Conclusion & next step:** The **machinery is sound** — 60×1 s averaging matches the derivation (0/191
  violations of n_used+n_gap==60), §5 filters behave as specified, Kraken reconstruction CRC-clean (<0.1%
  mismatch = genuine resyncs), replica in believable BTC range. The p95 miss is driven by (1) **missing
  constituents** (only 2 of the spot set; real BRTI uses ~5–6) and (2) a **Bitstamp self-crossed-book bug**
  (Bitstamp present+synced in ~159/191 windows but DROPPED by §5.2.1(c) in ~107 — persistent best_bid>best_ask
  — so the "2-venue" replica is Kraken-only ~62% of windows; effective Bitstamp coverage ~31%). NOT a replica
  or averaging defect. **Next:** (a) root-cause + fix the Bitstamp L3→L2 crossed-book (Coinbase-severity data
  bug); (b) deploy the PriceLevelBook fix (REV-024) → re-normalize Coinbase (fast now) → re-run as a full
  3-constituent study; (c) accumulate more days for larger n / full test day. Closing to p95<$5 should come
  from constituent coverage + the Bitstamp fix, not re-tuning (levers inert at 2 venues).
