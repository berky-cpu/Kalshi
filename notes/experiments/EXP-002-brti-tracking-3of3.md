# EXP-002: BRTI replica tracking error vs Kalshi settlement — full 3-constituent, walk-forward

- **Date / author (agent):** 2026-10-21, index-replicator (run). **Supersedes EXP-001** (2-of-3).
  **NOT yet reviewed — pending red-team REV-027. Does not count until REV-027 PASS.**
- **Hypothesis:** with the Bitstamp reseed-resurrection crossed-book bug fixed (commit `a40b452`,
  REV-026 PASS) and the data re-normalized, the replica should now run on **all 3 recorded spot
  constituents** (Bitstamp + Kraken + Coinbase Advanced) and track Kalshi's published settlement
  (`floor_strike` open / `expiration_value` close), 60×1 s averaged, toward the acceptance target
  **p95 |error| < $5**. Expectation going in: better than EXP-001's 2-of-3 ($3.15 / $8.997), but the
  real BRTI uses more venues than we record, so a p95 miss was still plausible.
- **Data:** `clean_22study_v2` (Bitstamp `live_orders_btcusd`, Kraken `level3`, Coinbase Advanced
  `level2`), re-normalized post-fix. Kalshi settlement = ground truth (re-normalized fresh for
  2026-10-18…21 into a private tmp path; open(N)≡close(N−1) chaining confirmed in the data). Window:
  markets settling **≥ 2026-10-18 18:00 UTC** (post-incident clean-data cutoff) through latest. In
  scope: **241** settled KXBTC15M markets (10-18: 24 post-cutoff, 10-19: 96, 10-20: 96, 10-21: 25).
  Data extent: all 3 venues cover 10-20 fully (to 23:59 UTC); 10-21 thins out (bitstamp→01:59,
  kraken→02:59, coinbase→03:45 UTC).
- **Walk-forward split (justified):** we now have 2 full clean days before the latest. **TRAIN =
  {2026-10-18 (post-18:00), 2026-10-19}** (n=112 full-cov close); **TEST = {2026-10-20}** — the latest
  **full** day, held out, the freeze never saw it (n=80 full-cov close). **TEST2 = {2026-10-21}** was
  intended as an extra held-out day but yields only **n=2** full-cov windows (bitstamp/kraken data run
  out early 10-21), so it is reported for completeness only and is **not** evidence either way.
  evaluate_oos is run **per window_type** (close/open) so the open(N)≡close(N−1) chain never straddles
  a split; `split_by_date` raises on any date overlap.
- **Method:** `tracking.evaluate_oos` — tune (cap_mean O-4 × median_base O-9 × endpoint O-5) on TRAIN
  windows only by lowest median |error|, FREEZE, report `error_stats` on the disjoint TEST split only.
  **Full-coverage gate** for the headline: `venues_used == {bitstamp, kraken, coinbase_advanced}` (all
  3 contributed ≥1 of the 60 ticks); coverage physically attached to every stat. Code: committed
  `src/kbtc/index/` (run from the post-fix sandbox `/tmp/kbtc_renorm`, verified to carry `a40b452` +
  `evaluate_oos`); study/driver kept in `notes/_exp002_scripts/` (uncommitted).
- **Costs modeled:** N/A — index-tracking error (replica vs published settlement), not a P&L backtest.
- **Frozen choice:** config `(cap_mean=trimmed, median_base=all)`, endpoint **T-60..T-1**.
  **IMPORTANT — the O-4/O-9 grid was still empirically INERT** (contrary to the going-in expectation
  for 3 venues): all 4 configs were **byte-identical** on every window. Reasons, both confirmed in the
  run: (1) the §5.3 median-deviation filter **never fired** — 0 `deviation` drops across all 14,460
  close-window ticks (the 3 USD spot venues track within a few $, never breaching the 5% PED), so
  `median_base` all≡active; (2) the §3.7 order-size cap never changed the **cent-rounded** index, so
  `cap_mean` trimmed≡winsorized. So the frozen config is a **tie-break artifact**; the only effective
  lever was the **endpoint O-5** (TRAIN median T-60..T-1 $1.950 vs T-59..T $2.150 → froze T-60..T-1,
  same as EXP-001). This is a genuine finding to flag, not a passed tune.

## Results

- **HELD-OUT TEST (2026-10-20), full 3-venue coverage, frozen (trimmed/all, T-60..T-1):**
  **median $2.690 · p95 $7.022 · max $9.320 · signed bias −$1.292 · n=80 · venues=3.**
  **p95 $7.022 vs $5 target → MISS.** (≈ 0.0033% median / 0.0086% p95 / 0.0114% max of an ~$82k index.)
- **In-sample TRAIN (10-18+10-19, labeled in-sample):** median $1.950 · p95 $5.678 · max $8.650 ·
  bias −$0.393 · n=112. (TEST ≥ TRAIN on every metric — modest, expected.)
- **Coverage-relaxed TEST (context, not headline; all 10-20 windows regardless of coverage, n=96):**
  median $3.120 · p95 $7.228 · max $9.320 · bias −$1.327.
- **Robustness TEST2 (10-21, frozen choice reused — frozen config/endpoint matched primary):**
  median $3.310 · p95 $5.182 · n=2. **n=2 is not meaningful**; recorded only for transparency.
- **open ≡ close:** the open-window headline is byte-identical to close (same n/med/p95/max/bias) —
  open(N) windows are the same underlying 60 s averages as close(N−1), relabeled. NOT independent
  corroboration (same as EXP-001).

**vs EXP-001 (2-of-3):** TEST median **$3.15 → $2.69**, p95 **$8.997 → $7.022**, max $10.34 → $9.32,
bias −$1.81 → −$1.29, n **30 → 80**. The 3rd constituent + the Bitstamp fix improve every metric and
nearly triple the held-out n — but **still MISS p95 < $5.**

## Coverage

- **Full-coverage (all 3 used) close windows per day:** 10-18 **24**, 10-19 **88**, 10-20 **80**,
  10-21 **2** (of 24/96/96/25 in scope). Late-10-20 and 10-21 shortfalls are venue resync/gap
  stretches, not missing recordings (data exists to 23:59 on 10-20).
- **Data-presence (synced non-empty book, close, of 241):** all3 = 226, 2of3 = 4, 1of3 = 3, none = 8.
- **Per-venue usable % (close T-59..T, 14,460 ticks):** Bitstamp **80.2%**, Kraken **93.4%**,
  Coinbase **96.3%**.
- **Per-venue drop reasons (tick counts):**
  - Bitstamp: `self_crossed` **1902** (13.2% of all ticks; 14.1% of ticks where it had a book),
    `no_book` 900, `stale` 63. **The residual §5.2.1(c) self-crossed drops are the ~10% the fix left
    behind** — down from EXP-001's ~63% of windows being Bitstamp-crossed. Those snapshots are still
    (correctly) dropped.
  - Kraken: `no_book` 660, `stale` 291.  Coinbase: `no_book` 480, `stale` 61.
  - **No `deviation` drops for any venue** — confirms the §5.3 filter never triggered.

## Error structure (why it still misses)

The error is **not** zero-mean noise. It has a **structural negative bias** (replica below published)
that concentrates in **high realized vol** (vol-quintile 4 bias −$3.83 vs ≈−$0.5 in q0–q3) and in
specific UTC hours (h=15 bias −$7.16, h=05 −$5.75, h=13 −$4.27; US pre-/post-open and a thin-liquidity
hour), visible in `notes/figures/EXP-002/exp002_tracking_close.png`. With the Bitstamp bug fixed and
all machinery invariants holding, the residual is now dominated by (a) **missing constituents** — the
current CF BRTI constituent list has more spot venues than the 3 we record (e.g. Gemini, LMAX,
Crypto.com, Bullish are not recorded), and (b) a **structural price-construction / high-vol lag** in
the replica. It is no longer attributable to the crossed-book data defect.

## Sanity

- Replica value range **$80,746.28–$83,224.67** vs published **$80,746.73–$83,229.95** — tracks
  published; the market genuinely dipped just below $81k on 10-20, so "below $81k" is real, not a bug.
- **60×1 s averaging invariant:** 0 windows with `n_used + n_gap ≠ 60` (all windows are exactly 60
  top-of-second prints; gapped seconds counted in n_gap).
- **Kraken CRC:** v2 re-normalize = **11,609,954 checkpoints, 0 mismatch, 0 deviation** (100% clean).
- Residual Bitstamp crossing (~13% of ticks) still dropped per §5.2.1(c), as noted above.

## Plots
`notes/figures/EXP-002/exp002_tracking_close.png`, `…_open.png` (4-panel: |error| hist, error vs UTC
hour, error vs realized vol, error vs published level; over all full-cov windows, train+test, frozen
endpoint — diagnostic, not the held-out headline alone).

## Red-team review
**PENDING — REV-027.** This result does **not** count and the ROADMAP is **not** ticked until it passes.

## Conclusion & next step
3-of-3 + the Bitstamp fix is a clear improvement over EXP-001 (median $3.15→$2.69, p95 $8.997→$7.02,
n 30→80) and the machinery is sound (CRC-clean Kraken, 60×1 s invariant exact, filters behaving, replica
in the published range). **But it still MISSES p95 < $5**, and the miss is now driven by **missing
constituents + a structural high-vol/time-of-day negative bias**, not by a data defect. The O-4/O-9
grid remains empirically inert (deviation filter never fires at 3 tightly-coupled USD venues; cap_mean
never moves the cent-rounded index), so **re-tuning will not close the gap**. Next: (a) add the missing
BRTI constituent recorders (Gemini / LMAX / Crypto.com / Bullish per the current list) and re-run;
(b) root-cause the structural negative bias at high vol / US hours (price construction: utilized-depth
index vs mid, staleness-proxy behaviour); (c) accumulate a clean full post-10-21 day for a larger
held-out test.
