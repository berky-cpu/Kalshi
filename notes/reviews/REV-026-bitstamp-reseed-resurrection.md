# REV-026 — Red-team review: Bitstamp reseed-resurrection guard (study-2.2 crossed-book fix)

- Date: 2026-10-20
- Scope: uncommitted working tree — `src/kbtc/recorders/bitstamp.py` (+89/-8),
  `tests/test_recorders_bitstamp.py` (+117). Nothing committed.
- Context: fix for the crossed-book data bug that invalidated Bitstamp as a BRTI
  constituent in EXP-001 (see REV-025 §"Bitstamp self-crossed book").
- Reviewer action: read-only. Ran `uv run pytest -q tests/test_recorders_bitstamp.py`
  (8 passed) and in-memory scenario probes (below). Did not edit code.

## Verdict: PASS-WITH-CAVEATS

The fix correctly targets the proven root cause (stale REST snapshot resurrecting
stream-deleted orders at the touch), the headline test has real teeth, the memory
bound is real, blast radius is clean, and UNVERIFIED venue cause is tagged honestly.
It is NOT yet a hard guarantee of uncrossed books, and the real-data payoff
(43.0% -> 2.1%) is not reproduced in any reviewable artifact. Those gate re-counting
Bitstamp into EXP-001; the code change itself is sound to commit with the caveats below.

## What I verified (positives)

- Mechanism match. The traced failure is "whole REST snapshot is stale": snapshot label
  S is behind live events already applied, so orders alive at S but deleted at micro > S
  get resurrected at the touch. Guard `delete_micro > S` catches exactly that class.
- Floor consistency (hunt item 4). `floor`/`S` is now computed ONCE at the top of
  `apply_snapshot` (bitstamp.py:115-118) and shared between the process-floor
  (`snapshot_floor_micro`) and the re-add suppression (`_seed_order`). Same value, same
  parse (`_int_or_none`, fallback `timestamp*1e6`) — consistent by construction.
- Memory bound is real (hunt item 3, the cap). `_note_delete` (bitstamp.py:143-159)
  prunes by age against the NEWEST delete micro, then a final `while len(rd) >
  MAX_RECENT_DELETES` hard-caps at 50k regardless of clock jumps / out-of-order micros.
  Hard cap evicts `popitem(last=False)` = oldest insertion (the right entry).
- Multiple consecutive reseeds (hunt item 5). `_recent_deletes` correctly persists across
  reseeds; no wrong suppression found on a later reseed — MODULO the id-reuse assumption
  (F1).
- Teeth. Reverting the guard (clearing `_recent_deletes` before the reseed) resurrects
  order 999 at bid 105 over ask 101 -> CROSSED. The bounded test is window-agnostic
  (parameterized by `RESEED_DELETE_WINDOW_US`).
- Blast radius (hunt item 7). `git status`: only `bitstamp.py` + its test modified.
  `execution/guard.py`, `.claude/`, `recorders/writer.py`, order paths untouched. No new
  deps (`OrderedDict` is stdlib). No network / order path.

## Findings (ranked)

### F1 (HIGH — must verify before counting recovered data): id-reuse is the load-bearing, UNVERIFIED assumption
Suppression is keyed purely by order id. Correctness against FALSE-suppression (dropping
REAL liquidity) depends entirely on Bitstamp order ids never being reused within the
retained window. If an id is ever reused, a stale `_recent_deletes[id]` silently drops a
genuinely-resting snapshot order. This failure is INVISIBLE — the book shows LESS than
reality and does NOT cross, so no crossing alarm catches it; it would quietly bias the
replica. The code does not flag id-uniqueness as the critical assumption.
Evidence: bitstamp.py:137-140 (`dmicro > snap_micro` keyed on `oid` only).
Fix: verify from the Bitstamp primary WS/REST doc (or empirically scan recorded
`order_created` streams for duplicate ids), record in `docs/facts.md`, and add a code
comment marking it the correctness-critical assumption.

### F2 (MED): boundary `delete_micro == S` is not suppressed — inconsistent with the process floor
`_seed_order` suppresses only `dmicro > snap_micro` (strict). The process-floor drops live
events with `micro <= S` i.e. treats micro == S as "already reflected in the snapshot".
The two conventions disagree at the tie. Confirmed empirically: a delete at micro == S,
re-listed by a stale snapshot, is re-added and the book CROSSES (105 over 101).
Vanishingly rare (microsecond tie), but a genuine logic asymmetry.
Evidence: bitstamp.py:138 vs process floor bitstamp.py:181-184.
Fix: use `>=` in `_seed_order` to match the floor's inclusive semantics; add a D==S test.

### F3 (MED): silent guard-disable paths leave residual crossing, unmonitored
Two paths quietly fall back to pre-fix behavior (confirmed crossing in both probes):
(a) `order_deleted` with missing/None `microtimestamp` -> `_note_delete` returns early
    (bitstamp.py:146-147) -> delete not recorded -> resurrection NOT suppressed.
(b) snapshot with neither `microtimestamp` nor `timestamp` -> S is None -> `_seed_order`
    skips the guard entirely (bitstamp.py:136) -> every order re-added.
Both depend on the venue always sending micro (doc-claimed, UNVERIFIED for the delete
frame specifically). These plausibly explain part of the residual 2.1% crossing.
Fix: add counters/logs (`deletes_missing_micro`; warn on apply_snapshot with S None and a
non-empty snapshot) and quantify their share of residual crossing before re-including
Bitstamp. Note: the coarse fallback `timestamp*1e6` UNDER-estimates true snapshot time, so
in that path the guard biases toward OVER-suppression (up to ~1s of deletes) — secondary.

### F4 (MED — process gate): the 43.0% -> 2.1% counterfactual is not reproduced in a reviewable artifact
The payoff that justifies re-including Bitstamp is asserted in the module docstring only.
`notes/_study22b_scripts/study22b_run.log` is the tracking-error study, not the crossing
counterfactual; `diag22b.py` is unaudited. Unit tests prove the PROCESSOR logic on
synthetic frames, but EXP-001 consumes NORMALIZED clean parquet. Before any result counts:
re-normalize the study-2.2 window through the fixed processor and re-measure crossing% on
the normalized L2, reviewer-visible. 2.1% is NOT zero — characterize the remainder
(F2/F3 are candidate contributors).

### F5 (LOW): window 120s is UNVERIFIED but safe; add an alert and document the failure mode
No venue SLA; inferred lag ~0.15s. The guard only misses a resurrection if REST lag >
120s (a delete gets age-pruned before the lagging snapshot lands) — ~800x margin.
Acceptable. Fix: alert on `resurrections_suppressed` spikes and document that lag > 120s
-> missed suppression -> crossing returns.

### F6 (LOW): hard-cap eviction path untested
Only age-pruning has a test. The `MAX_RECENT_DELETES` backstop (defensive against micro
regression) is exercised by no test. Reading confirms it is correct (evicts oldest; final
while-loop guarantees the bound). Fix: add a micro-regression burst test to lock it.

### F7 (NIT): working tree carries untracked scratch
`notes/_tmp_*.py`, `notes/_study22b_scripts/`. Keep out of the fix commit.

## What would change my mind (to full PASS)
1. F1 resolved: id non-reuse verified and recorded in `docs/facts.md`.
2. F4 resolved: reviewer-visible re-normalize + crossing re-measurement on real study-2.2
   data, with the residual characterized.
3. F3 instrumented: residual-crossing paths counted/quantified.
`>=` boundary (F2) and the cap test (F6) are quick hardening.

---

## Round 2 (2026-10-20) — re-review of the applied caveats

Scope: F2/F3/F6/F1 code+facts delta (still uncommitted). Full suite `uv run pytest -q`
-> 368 passed; `tests/test_recorders_bitstamp.py` 11 passed. I re-read the diff and ran
in-memory probes. F4 (reviewer-visible crossing% on real NORMALIZED parquet) is explicitly
deferred to the coordinator's next re-normalize — a separate artifact, not part of this code
delta.

### Updated verdict: PASS (code delta). Re-counting Bitstamp into EXP-001 remains gated on F4 only.

- **F2 — RESOLVED.** `_seed_order` now suppresses `delete_micro >= S` (bitstamp.py), matching
  the process-floor `micro <= S` convention; the tie is now consistent. New
  `test_reseed_suppresses_stale_tie_delete_at_floor` has teeth (with `>` the D==S phantom
  re-adds and crosses). Verified it cannot over-suppress: the branch only fires when an id is
  BOTH in the snapshot rows AND carries a recorded delete `>= S`; a legitimately re-rested
  order (deleted then re-created before a later snapshot) keeps its old delete micro `< S`, so
  the guard does not fire — confirmed by an adversarial probe (re-create at 2600, reseed S=2700,
  order kept, suppressed=0).
- **F3 — RESOLVED.** Both silent paths are now counted and WARNING-logged: delete with no micro
  (`deletes_missing_micro`) and floorless reseed with rows (`snapshots_without_floor`). Warning
  suppressed on deliberate empty seeds (no spam). The `timestamp*1e6` fallback is documented as
  biasing toward OVER-suppression (the safe direction — never a resurrection). New
  `test_guard_disable_paths_are_counted_and_warned` exercises both via caplog. Behavior otherwise
  unchanged. Minor non-blocking nit: `snapshots_without_floor` also increments on benign empty
  seeds (warning correctly suppressed), so the counter slightly conflates benign vs real; it is
  observability-only.
- **F6 — RESOLVED.** `test_hard_cap_bounds_map_under_microtimestamp_regression` uses a
  non-advancing micro (age-prune can never fire) and MAX_RECENT_DELETES+100 inserts; map caps at
  exactly 50_000 and evicts the oldest (FIFO front), newest retained. Real teeth (without the cap
  the map grows to n and d0 survives).
- **F1 — CLOSED for the purpose of counting recovered data.** Empirical scan of recorded raw for
  2026-10-18/19/20: 21,129,299 `order_created` ids, ZERO duplicates within each day and across
  the full 3-day span (distinct == creates), i.e. no id reuse over 3 days >> the 120 s window.
  Recorded in `docs/facts.md`, honestly tagged **VERIFIED (empirical) / UNVERIFIED (doc)** — the
  primary WS v2 page is JS-rendered and yielded no explicit non-reuse statement. A
  CORRECTNESS-CRITICAL note in `bitstamp.py` cross-references it. This closes the invisible
  over-suppression risk for the current venue/data regime; revisit if Bitstamp changes id
  allocation or over much longer horizons.

### Remaining gate (not a code issue)
- **F4 — OUTSTANDING.** The 43.0% -> 2.1% crossing improvement must be reproduced on the real
  NORMALIZED clean parquet (re-normalize the study-2.2 window through the fixed processor),
  reviewer-visible, with the ~2.1% residual characterized (F2/F3-path contributions now countable
  via the new counters). Until then the code is sound to commit but EXP-001 must NOT re-count
  Bitstamp as a constituent.
