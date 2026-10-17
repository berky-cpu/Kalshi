# REV-012 — Red-team review: ROADMAP 2.2 (BRTI replica + tracking-error harness)

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/index/` (params, book, consolidate, spacing, curves, filters, brti,
  loader, tracking) and `tests/{test_index_aggregation,test_index_tracking}.py`.
- **Verdict:** **FAIL (Round 1) → PASS (Round 2).** `uv run pytest -q` → 209 passed (42 index tests).
  Reads/compute only; no network/order/secret path. `guard.py` / `.claude/*` / `writer.py` untouched
  (do not appear in the diff).

## What it does
Replicates the CF Benchmarks BRTI (methodology v17.0) from constituent order books as recorded, 1 Hz:
consolidate constituent books → order-size cap `C_T` (trimmed mean Eq4e + 5·winsorized σ Eq4h) →
uncross → price–volume curves → utilized depth `D=0.5%` → exponentially depth-weighted mid with
**λ = 1/(0.3·v_T)** → deviation filter (§5.3, PED 5%→2.5% hysteresis). All §6 constants are
`BrtiParams`. No look-ahead: as-of book lookup is O(log n) via bisect on `(recv_ts_ns, snap_id)` with a
documented tie-break; a compute failure emits a GAP, never an interpolated value. The tracking harness
averages the replica over the 60×1 s settlement window at both endpoints and compares to Kalshi
`floor_strike` (open) / `expiration_value` (close).

## Round 1 — FAIL (narrow)
Core algorithm verified **correct and faithful** by independent hand-recomputation (cap `6.472136`,
flat-book index `100.50`, λ·v_T decay `≈3.57%`, hysteresis thresholds), **no look-ahead, no float at any
price boundary, non-circular tests, safety clean.** The single FAIL was methodological:
- **F1 [HIGH, determinative]** — the tracking harness could tune the free levers (window endpoint,
  `cap_mean`) and report p95 over the **same** windows (no train/test date split). An explicit FAIL
  trigger: picking "the best endpoint" then reporting its p95 overstates accuracy (in-sample overfit).
- **F2 [MED]** — §5.3 deviation-filter median base used active-only venues, not the literal "all venues"
  of the spec, and was not a switchable parameter.
- F3 [LOW, accepted] staleness proxy (recorder heartbeat is the eventual fix); F4 [LOW] coverage
  (`venues_used`) not threaded into the reported stats.

## Round 2 — PASS
- **F1 FIXED (verified independently, not just by their test).** New OOS path in `tracking.py`:
  `split_by_date` / `evaluate_oos` partition by UTC date and **raise `ValueError` on any
  `set(train) & set(test)` overlap** (hard leak-guard, independent of how the sets were produced);
  `chronological_split` splits distinct dates on whole-date granularity (disjoint by construction).
  `evaluate_oos` tunes (config, endpoint) on **TRAIN windows only**, freezes, and computes the headline
  `error_stats`/p95 over the **disjoint TEST split only** → `OOSReport` records frozen choice + both date
  sets + n_train/n_test + train_stats (transparency) + test_stats (headline). `summarize`/`best_endpoint`
  docstrings now state IN-SAMPLE/TUNING-ONLY and point to `evaluate_oos` as THE acceptance function. The
  reviewer reproduced the freeze from scratch and confirmed a day-2 error of 60 is **provably excluded**
  from the day-1-tuned headline because it belongs to the non-frozen endpoint — a genuine freeze+filter,
  not hardcoding.
- **F2 FIXED.** `BrtiParams.median_base: str = "all"` (literal §5.3 default; `"active"` the alternative),
  validated in `__post_init__`, threaded into `DeviationFilter.apply`, logged as open item **O-9**
  (a tune-on-TRAIN / report-OOS lever alongside O-4 `cap_mean`, O-5 endpoint). Hysteresis test
  legitimately rewritten for the "all" default (four anchor venues pin the all-venue median at 100 to
  isolate the 5%/2.5% band) — not weakened.
- **F4 FIXED.** `ErrorStats.venues_used` appended (default `()`, positional construction unaffected),
  filled with the sorted union of contributing constituents and flowed into `OOSReport.test_stats` — a
  headline p95 now physically carries e.g. `('bitstamp','kraken')`, so the 2/7-constituent caveat can't
  be detached from the number.
- **Boundary CLEARED.** `as_price_decimal` (Decimal passthrough, else `Decimal(str(x))`, never
  `Decimal(a_float)`) wraps both published values in `evaluate_market`; settlements parquet already stores
  Decimal. Confirmed `100.1`-as-float → `Decimal("100.1")`, not the binary-expanded tail. Error is
  `Decimal − Decimal` throughout.

## Standing items (non-blocking for the build; gating the *result*)
- **The tracking-error RESULT is not yet produced.** Only 2/7 constituents are live (Bitstamp + Kraken;
  Coinbase pending institutional API, others queued), and recorded data is still accumulating. The
  machinery + harness are built and reviewed; the p95<$5 acceptance **against real settlement values on a
  walk-forward split remains open** until there is enough recorded constituent + Kalshi-settlement data.
  No in-sample or tuned number can now silently be reported as the headline (F1 fix guarantees this).
- O-4 (`cap_mean`), O-5 (endpoint), O-9 (`median_base`) are the levers to tune-on-TRAIN / report-OOS, and
  the 2/7 → full-constituent coverage study is part of that open work.
- F3 staleness proxy unchanged (accepted/flagged).
- Minor (info): `split_by_date` silently holds out dates in neither set — intentional, not a leak.
