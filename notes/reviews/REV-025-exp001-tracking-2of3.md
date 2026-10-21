# REV-025 — Red-team review: EXP-001 first-pass BRTI tracking error (2-of-3 constituents)

- **Date:** 2026-10-20
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `notes/_study22b_scripts/{study22b.py, diag22b.py, norm22b.py, study22b_run.log}`;
  `src/kbtc/index/tracking.py` (evaluate_oos, split_by_date, tune_endpoint_and_config, result_date,
  error_stats, window_sample_times_ns). Data (`/opt/kbtc/data/clean_22study`) is VPS-only — reviewer vetted
  the committed leakage-critical code + script logic + run-log internal consistency; could not re-run.
- **Verdict:** **VALID first result — it COUNTS** as the 2-of-3-constituent (Bitstamp+Kraken) tracking error,
  honestly caveated, legitimately **MISSING p95<$5** (TEST p95 $8.997). Nothing (leakage, miscomputation, or
  the Bitstamp-crossed issue) invalidates it as a reported first pass. The p95 miss is the EXPECTED
  consequence of thin constituent coverage + the Bitstamp data bug, NOT a replica/averaging defect.

## Verified
1. **Leakage — CLEAN (verified, not trusted).** evaluate_oos splits by UTC settlement date
   (`split_by_date` raises on any overlap), tunes (config,endpoint) on TRAIN only, freezes, reports on TEST
   only — no TEST window influences the freeze. `result_date` = UTC date of the window's reference second
   (close_time_ns), so the split unit is the authoritative settlement time, not the ticker string. Chaining
   handled: evaluate_oos runs PER window_type, so close(N) and open(N+1) (same boundary reference) are never
   in one split; within a window_type consecutive windows are 15 min apart → distinct reference points.
   The reviewer chased the apparent red flag (three `26OCT19…`-named tickers inside a result_date∈{10-20}
   filter) and confirmed it is BENIGN + confirms correctness: the Kalshi ticker datetime is ET, the split is
   by UTC close_time_ns (23:00 ET = 03:00 UTC 10-20), and the error values reconcile exactly with the
   by-UTC-hour buckets. No contamination.
2. **p95 honest.** $8.997 = frozen-on-train (endpoint chosen because train median 2.315 < 2.695), reported
   over the frozen test selection (n=30; n_full=29 + one n_gap>0 window), np.percentile(…,95) of |error| in
   $; breakdown reconciles (both-venue count 52 = train 22 + test 30). open≡close is correct (chained
   markets → same measurements relabeled; NOT independent corroboration). Config grid INERT with 2 venues
   (all 4 cap_mean×median_base identical → effectively a 2-endpoint search) — stated, not over-claimed.
3. **Coverage labeling SOUND.** Correctly 2-of-3 spot constituents (Coinbase excluded), venues_used attached,
   headline gated on venues_used=={bitstamp,kraken} (not a 1-venue result in disguise); kraken-only 119/191
   (~62%) surfaced. Missing-constituent attribution (caveat E) sound; bias sign-flip across days
   (train +2.13 / test −1.81) consistent with missing-constituent composition drift, not a systematic offset.
4. **Sanity PASSES.** Replica 81,284–83,224 vs published 81,130–83,230 (believable, same scale); 60×1 s
   averaging matches the derivation (n_used+n_gap==60, 0/191 violations); worst window (max 10.34, n_gap=0) is
   a clean genuine miss, the gappy 10.10 window shown with n_gap=35; Kraken CRC clean (<0.1% = resyncs, from
   REV-013).

## The key finding — Bitstamp self-crossed book (escalate to Coinbase-severity)
Bitstamp is present+synced in ~159/191 close windows but USED in only 52 (kraken-only 119, bitstamp-only 7,
none 13) — so in ~107 windows Bitstamp was present-and-synced yet DROPPED by §5.2.1(c) because its normalized
top-of-book is persistently CROSSED (best_bid>best_ask, e.g. ~$16, synced=True). A synced non-empty crossed
L2 book is not real market data — it's an L3→L2 normalization bug (bid/ask mislabel, or resting orders not
removed on cancel) or a recording gap, directly analogous to the Coinbase exclusion, and Bitstamp has NO CRC
oracle (checkpts=0) so nothing else caught it. It does NOT invalidate the headline number (the 30 headline
windows used Bitstamp books that PASSED the crossed-book filter), but it means effective Bitstamp coverage is
only ~31% and the Bitstamp book must be treated as SUSPECT pending a root-cause fix.

## Required follow-ups (before it graduates beyond "first pass")
1. Attach `diag22b` crossed-book evidence (per-tick drop reason_counts + bb/ba samples) — the diag output was
   not in the run log; the present-but-dropped arithmetic (159 present vs 52 used) is strong circumstantial
   support but should be shown. **→ to attach.**
2. Open a **Bitstamp L3→L2 crossed-book investigation** at Coinbase-level severity. **→ delegated.**
3. State in the writeup: the inert-config-grid and open≡close-not-independent facts (done in EXP-001).
4. Optional on-box assert of train/test ticker disjointness (reviewer expects it holds).

## Read on the miss
$3.15 median / $9 p95 on an ~$82k index ≈ 0.004% / 0.011% — small, dominated by (a) missing BRTI
constituents and (b) Bitstamp usable in only ~31% of windows. The averaging, snapshotting and split
machinery all check out; closing to p95<$5 should come from ADDING constituents (Coinbase perf fixed; fix
Bitstamp crossed book) rather than re-tuning the replica (levers inert at 2 venues).
