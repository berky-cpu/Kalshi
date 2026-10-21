# REV-027 — Red-team review: EXP-002 full 3-constituent BRTI tracking error + F4 crossing-recovery

- **Date:** 2026-10-20
- **Reviewer:** red-team-reviewer (adversarial; read-only)
- **Scope:** `notes/experiments/EXP-002-brti-tracking-3of3.md`; `notes/_exp002_scripts/{study_exp002.py,
  study_exp002_run.log, norm_settle.py, probe_*.py}`; committed `src/kbtc/index/{tracking,brti,book,filters,
  params,loader,consolidate,curves}.py`; on-box `/tmp/exp002/{study.log,fig,settle_clean}`, `/tmp/crossing_f4.py`,
  `/opt/kbtc/data/clean_22study_v2`, sandbox `/tmp/kbtc_renorm/kbtc`. Builds on REV-025 (methodology) + REV-026 (Bitstamp fix).
- **Verdict:** **VALID-with-caveats — it COUNTS** as the honest 3-of-3-spot-constituent tracking-error result,
  legitimately **MISSING p95<$5** (TEST p95 $7.022). The -$1.292 bias is **NOT a leakage/look-ahead/construction
  defect** — it is a benign, *conservative-direction* artifact (missing BRTI venues + forward-fill / receive-latency
  lag on fast moves). No defect inflates the number; the bias makes the error *larger*, and the headline is honestly
  reported as a MISS. Acceptance target p95<$5 is NOT met and must not be ticked.

## Provenance verified (not trusted)
- **Sandbox == committed.** All 8 index files (`tracking/brti/book/filters/params/loader/consolidate/curves.py`)
  are **md5-identical** between the run sandbox `/tmp/kbtc_renorm/kbtc/index/` and local committed `src/kbtc/index/`.
  Reviewing the committed code *is* reviewing what ran. `PYTHONPATH=/tmp/kbtc_renorm` resolves `kbtc.*` to the sandbox
  (layout `/tmp/kbtc_renorm/kbtc/index/...`), which carries the Bitstamp reseed fix (`recorders/bitstamp.py`,
  `resync.py`) and `evaluate_oos`. `/tmp/exp002/study.log` matches the committed run log.

## Checklist findings
1. **Look-ahead / leakage — CLEAN (verified).**
   - `recv_ts_ns` used everywhere (study COLS; `book.as_of` bisects `(recv_ts_ns, snap_id) <= T`, strictly backward —
     book.py:131-143). **No `exch_ts_ns` anywhere** in the index/tracking path. If anything this is *conservative*
     (receive time lags exchange time), not leaky.
   - `split_by_date` raises on any date overlap (tracking.py:340-342); `evaluate_oos` tunes on `train_by_cfg` only and
     reports `error_stats` on `test_sel` only (tracking.py:439-452). TRAIN={10-18,10-19}, TEST={10-20} disjoint -> the
     freeze never saw 10-20.
   - `evaluate_oos` run **per window_type**, so open(N)==close(N-1) never straddle a split. Confirmed: open headline is
     byte-identical to close (correctly flagged as NOT independent corroboration).
   - **Ground truth independent + uncontaminated.** Settlements come from Kalshi's own feed (`close_value`=
     expiration_value, `open_value`=floor_strike; raw_json), re-normalized fresh into `/tmp/exp002/settle_clean`.
     Direct check of that parquet: **313 KXBTC15M markets, all exactly 900 s close-open, 312/312 consecutive pairs have
     open(N)==close(N-1) EXACTLY (0 mismatches).** No book data feeds the ground truth; chaining invariant holds.
2. **Costs:** N/A — index tracking error vs published settlement, not a P&L backtest. Correctly stated.
3. **Fills / 4. Adverse selection:** N/A (no quoting/fills in this study).
5. **Statistics — CAVEAT (the main limitation).** TEST is **one UTC day** (10-20). n=80 "windows" are consecutive
   15-min slices of a **single BTC path** -> highly autocorrelated; the effective independent sample is ~1 day, not 80.
   No CI; "drop the best day" is impossible with one test day. TRAIN is 2 days; TEST2 (10-21, n=2) is correctly
   excluded from evidence. Multiple-testing is **minimal** — the (cap_mean x median_base) grid is empirically inert
   (below), so the tune is effectively a 2-endpoint search. Because the result is a **MISS**, this thin-sample
   limitation cuts the safe way, but the $7.022 p95 / -$1.292 bias are single-day point estimates, not a trustworthy
   population p95. (Same limitation REV-025 flagged.)
6. **Facts:** No reliance on anything unverified-and-unflagged. The settlement window endpoint is PARTIAL in
   `docs/facts.md` and the study *resolves it empirically* (the facts.md-sanctioned closer). Missing-constituent
   attribution is backed by facts.md:49/65 (7 BRTI venues; only 3 spot recorded).
7. **Benchmark:** N/A — the study tracks Kalshi's published settlement (the ground truth itself), not a fair-value-vs-mid bet.
8. **Safety — PASS.** Scripts read parquet only: grep for order-path / network / credential-string / demo-api / wss
   tokens -> no matches; git status shows no edit to the order-endpoint guard, the agent hooks directory, or the agent
   settings file. All box access read-only.

## The central question: is the -$1.29 bias a defect? — NO, benign (evidence)
A systematic one-signed bias is where bugs hide, so I attacked it directly. It is **conservative-direction lag +
missing venues**, not look-ahead / timestamp / weighting / endpoint defect:
- **(a) Direction is the opposite of leakage.** Replica systematically UNDERSHOOTS published (9/10 worst TEST markets
  replica<pub; replica value range **80746.28-83224.67 sits strictly inside** published **80746.73-83229.95** — the
  replica *compresses the extremes*, the fingerprint of a lagging estimator). Look-ahead would make the replica LEAD
  and shrink the error; this does the reverse.
- **(b) Bias scales with realized vol:** q0 -0.62 -> q4 -3.83, ~0 in low vol. A static construction/mislabel/weighting
  offset would show in low vol too. Scaling with move size = a *dynamics/lag* effect (forward-fill + receive latency on
  fast moves), not a wiring bug.
- **(c) Endpoint is NOT the driver.** `as_of` forward-fills the last book up to the 10 s staleness cutoff (filters.py:
  55-59), so on up-moves the replica reads stale lower prices -> negative. The one-second endpoint placement contributes
  only ~$0.05 (in-sample T-59..T bias -0.343 vs T-60..T-1 -0.393); it was tuned by median|error| (T-60..T-1 won
  1.95<2.15) — empirically supported, not the bias source.
- **(d)** Concentration in US high-vol hours (h=15 -7.16, n=4) is consistent with fast directional moves + lag + thin
  venue set, not with any look-ahead/misalignment signature.
- **Caveat:** "missing constituents" vs "construction lag" cannot be fully separated here — both benign. The writeup's
  next-step (b) (root-cause the high-vol lag) is correct and should NOT be dropped in favour of "just add venues";
  adding venues alone may not close the high-vol lag. Second-order: the residual Bitstamp `self_crossed` drops
  (1902 ticks) could skew venue composition on fast moves — a worthwhile follow-up diagnostic, still a benign
  data-quality drop, correctly handled.

## F4 crossing recovery — sound, reproduced
- **Reproduced `/tmp/crossing_f4.py` exactly:** OLD 62.92% -> NEW 10.34% overall; per-day 55.17->6.02, 77.28->10.73,
  58.05->13.33. Measurement is sound: clean data is one `part.parquet` per day so `files[0]` is the whole day;
  level-0 best bid/ask per snap_id; crossed = bb>=ba.
- **Residual ~10-13% is benign** — those snapshots are (correctly) dropped by the index `prefilter_books` self_crossed
  gate, so they cannot contaminate the index. (Note: `crossing_f4.py` measures raw level-0 crossing without the
  `synced` filter; the study's drop accounting — self_crossed=1902=13.2% of ticks — uses the synced-gated prefilter
  path. Two different but mutually consistent lenses.)
- **Snapshot-count confound (12.8M->16.5M): none.** It is a per-snapshot *rate*; v2 legitimately reconstructs more
  snapshots post-fix. The massive crossing reduction holds regardless of denominator.

## Config-grid inert — correct behaviour, not a broken filter
0/14,460 `deviation` drops is **correct**: 3 USD spot venues never diverge >5% PED (~$4000 on $82k). cap_mean
trimmed==winsorized because the sec.3.7 order-size cap never moves the cent-rounded depth-weighted index. In-sample log
shows all 4 configs byte-identical. Correctly disclosed as a tie-break artifact; the only effective lever was the
endpoint. This *reduces* multiple-testing exposure.

## Coverage & sanity — honest, not cherry-picked
- n=80 headline honestly gated on `venues_used=={all 3}`; coverage-relaxed n=96 shown separately; 10-21 (n=2) excluded
  from evidence. Per-venue usable % (Bitstamp 80.2 / Kraken 93.4 / Coinbase 96.3) + drop-reason tick counts shown.
- Replica range inside published; 0 windows with `n_used+n_gap!=60` (60x1 s invariant exact); Kraken CRC 11.6M/0.
  TRAIN n=112 = 24+88, TEST n=80 reconcile.

## What would change my mind (-> FAIL)
- Bias present in LOW vol too (static offset), or replica *leading* published (positive bias on up-moves) -> construction/look-ahead bug.
- Ground truth derived from book data, or chaining broken -> contamination. (Verified clean: 312/312.)
- Sandbox code differing from committed on the leakage path. (Verified md5-identical.)
- TEST date touching the freeze. (Verified disjoint; guard raises on overlap.)

## Required follow-ups (do NOT block COUNTING this as a MISS)
1. Acceptance (p95<$5) needs BOTH missing BRTI constituents added AND the high-vol lag root-caused — plus **>=3-5
   independent held-out days** for a credible p95/CI (current TEST = 1 autocorrelated day).
2. Add a diagnostic separating missing-constituent vs forward-fill-lag contribution to the bias before assuming venues alone close it.
3. Keep the n=80 headline framed as a single-day point estimate, not a population p95.
