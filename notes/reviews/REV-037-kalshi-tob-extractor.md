# REV-037 — Kalshi top-of-book extractor (day-7 fast path)

Reviewer: red-team (skeptical). Date: 2026-10-24. Scope: uncommitted `git diff` on
`src/kbtc/normalize/pipeline.py` (+187) and `src/kbtc/normalize/__main__.py` (+16), plus
new `tests/test_normalize_kalshi_tob.py` (6 tests). Read-only review.

## Verdict: PASS-WITH-CAVEATS

Safe to commit as the day-7 Kalshi-mid source for the Brier harness. The incremental-best
logic is correct, the slim output is mid-equivalent to the full-depth path, the 24/7 recorder
is untouched, and there is no safety exposure. The CLI default flip is acceptable (the only
`kalshi_books` consumer reads best-only), but see F1 on how to keep it honest. All caveats are
MONITOR, none are must-fix. `uv run pytest -q` = 503 passed.

## Checklist findings

### 1. Incremental-best correctness (`_side_best`) — CORRECT
Verified the function returns exactly the brute-force `max` at every step, by case:
- touched present & `> cur_best` → touched becomes best (O(1)). Correct.
- touched present & `== cur_best` (reduce-in-place keeping it best) → returns cur_best; size is
  re-read fresh from the dict downstream. Correct.
- touched present & `< cur_best` (add below top) → returns cur_best, no recompute. Correct, and
  safe because a delta touches exactly ONE price, so `cur_best` (a different key) is untouched.
- touched absent & `< cur_best` (non-best level removed) → returns cur_best. Correct.
- touched absent & `>= cur_best` (the best itself removed), or `cur_best is None` → recompute
  `max(level_dict)` / `None`. Correct.
The only way `cur_best` can leave the dict is `touched == cur_best`, which is handled.
Key robustness point: `_side_best` reads `proc.books[...]` AFTER `proc.process`, so even a
seq-gap-DROPPED delta (processor skips `_apply_delta`) stays consistent — the touched price is
simply still/absent in the real dict and the branches resolve to the true max. Decimal price
keys come from the SAME `_dec(body["price_dollars"])` the processor used, so `touched in
level_dict` is an exact key match (no float/again-parse drift).
Test `test_tob_matches_brute_force_reference` has real teeth: SCENARIO hits add-below-best
(seq2, no-op), beat (seq3), delete-of-current-best-forcing-recompute (seq4), and non-best
removal (seq6), and compares to an independent fresh-`max` reference including recv order.

### 2. Harness read-back equivalence — CORRECT
`KalshiBookSeries.from_rows` takes `max` price per side within a `snap_id` group; the full-depth
path emits all levels (max over them) and the TOB path emits the single best — same result.
Both best-yes and best-no are emitted under ONE `snap_id` (verified in `stream_kalshi_tob`), so
the harness always gets both sides for a mid; `_tob_fingerprint`/`_kalshi_tob_rows` never split a
state across snap_ids. `_to_float_price` floats both paths, so Decimal-vs-`Decimal(str)` washes
out. `test_readback_mid_matches_full_depth_path` asserts equality at every timestamp. No case
where the slim path drops a mid the full-depth path would have produced.

### 3. Blast radius / CLI default flip — ACCEPTABLE
Grep confirms the ONLY consumer of `kalshi_books` is `signals/brier.py::KalshiBookSeries`, which
reads best yes/no only; nothing in `src` or `notes` reads level>0 of a Kalshi book. We have
never normalized `kalshi_books` at scale, so there is no on-disk full-depth artifact to break.
The flip is trivially invertible: `--kalshi-full-depth` restores `stream_kalshi`, and the guard
`not channel.lower().startswith("cfbenchmarks")` leaves the CF BRTI feed on its normal path.
`stream_kalshi_tob` is a standalone function independent of the CLI (the harness/study call it
directly). MONITOR (F1): default now silently produces level-0-only parquet; a future consumer
that assumes full depth from the default CLI would get a thin book with no error.

### 4. Live recorder untouched — CONFIRMED
`git diff --name-only` = the two normalize files only. `KalshiProcessor` / `.books` /
`_apply_delta` / `process` are imported and called READ-ONLY (plus the read-only helper `_dec`,
`_event_body`-equivalent `_kalshi_body`). No recorder or IO code changed; the 24/7 feed cannot
be affected by this offline normalize path.

### 5. synced / seq-gap — MATCHES full-depth
`baseline`/`pending` logic is copied verbatim from `stream_kalshi`: snapshot sets
baseline=True, pending=False; `proc.needs_resnapshot` sets pending=True until re-anchored;
`synced = baseline and not pending`. `test_synced_false_across_seq_gap_until_resnapshot` covers
it. Harness defaults `require_synced=True`, so un-synced TOB rows are dropped from the mid series
exactly as in the full-depth path.

### 6. Decimal / fingerprint — CORRECT
Price Decimal flows straight from the processor dict key into `_book_row` (no `Decimal(str)`
round-trip; the full-depth path's `Decimal(str(price))` is actually the weaker one, but both
float identically in the harness). `_tob_fingerprint` is O(1), keyed on
`(yes_price, yes_size, no_price, no_size)` as strings: a price OR size change on either side
changes it, so no real best-change is suppressed; a dropped/duplicate delta reproduces the same
dict and is correctly not re-emitted. MONITOR: `str(Decimal)` is exponent-sensitive
("100.0" vs "100.00"), which could at worst cause a spurious EXTRA emit — never a dropped one —
and the mid is unaffected. Not worth fixing.

### 7. Safety — CLEAN
No order path, no `execution/guard.py`, no `.claude/`, no `settings.json`, no secrets. Only new
import is `from kbtc.recorders.kalshi import _dec` (already a project module) — no new deps.

### 8. Tests have teeth — ADEQUATE, with gaps (MONITOR)
The brute-force comparison is a strong design (independent reference, recv-order checked). Gaps:
- No test drives a side FULLY EMPTY via a delta and then re-adds (the `cur_best is None` path of
  `_side_best` reached through a delta rather than a snapshot). Logic is correct by inspection,
  but it is untested directly.
- No randomized/property test over many delta sequences — the strongest possible teeth for an
  incremental-vs-brute-force invariant. Recommend (non-blocking) a property test with random
  add/remove/reduce deltas asserting TOB == fresh-max every frame.
- No test that a seq-gap-DROPPED delta leaves the tracked best correct (covered by reasoning +
  shared `proc`, but not asserted).

## Must-fix vs monitor
- MUST-FIX: none.
- MONITOR:
  - F1: default CLI now emits level-0-only `kalshi_books`; document this in the dataset/schema
    note so a future full-depth consumer fails loudly rather than reading a thin book.
  - F8a: add a delta-driven empty-then-refill test and ideally a randomized property test.
  - F6a: `str(Decimal)` fingerprint can spuriously (harmlessly) re-emit on exponent change.

## What would change my mind (to FAIL)
- A delta sequence where incremental `_side_best` != brute-force max (none found).
- Any real `kalshi_books` consumer that reads level>0 and is silently thinned by the default flip.
- Any change touching the recorder, guard, `.claude/`, or secrets (none present).
