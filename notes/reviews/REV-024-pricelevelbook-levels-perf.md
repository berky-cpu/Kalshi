# REV-024 — Red-team review: `PriceLevelBook.levels()` incremental fix (Coinbase normalize stall)

- **Date:** 2026-10-20
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/base.py` (`PriceLevelBook`: `__init__`/`set`/`best_bid`/`best_ask`/
  `levels`), `tests/test_recorders_base.py` (new parity property test).
- **Verdict:** **PASS — ship.** `uv run pytest -q` → 361 passed (was 330; +31 parametrized parity cases).
  Only base.py + the test file changed. `guard.py`/`.claude`/`writer.py`/`execution` untouched; no order
  path; no new dep (sortedcontainers already present from REV-013).

## Why
A production stall: normalizing Coinbase level2 (which uses `PriceLevelBook`) pegged a core at 100% CPU
crawling at ~0.3 MB/min. Root cause: `PriceLevelBook.levels()` did `sorted(d.items())` over ALL ~40k price
levels on EVERY event (called per-event by `_emit_book`→`extract.l2_levels`) — the same O(N·M) pathology
REV-013 fixed for `OrderIdBook` but never applied to `PriceLevelBook`.

## Fix
`bids`/`asks` are now `sortedcontainers.SortedDict` (Decimal price keys) — and since they ARE the
authoritative price→qty aggregate (no separate order store), the fix is simpler than OrderIdBook: no cache
to reconcile. `levels(side,n)` reads top-n off the ends in O(n) (bids `keys()[-n:]` reversed, asks
`keys()[:n]`); `best_bid`/`best_ask` read one end; `set()` unchanged (qty 0 pops the level, else sets).

## Verified (empirically, not just the new tests)
- **Parity byte-identical:** reviewer reimplemented the OLD `sorted(d.items(), reverse=bid)[:n]` body and
  compared vs the new `levels()` after every mutation across 40 seeds × 300 mutations, both sides,
  n∈{None,0,1,2,3,10}, + best_bid/best_ask — all equal. Edge cases match: empty→[] (best_* None); bids desc;
  asks asc; n≤0→[]; n=None→all; n>len→all (slice clamps); Decimal keys. The committed 31-seed property test
  (`test_price_level_book_sorted_aggregate_matches_full_scan_property` vs `_price_book_full_scan_levels`) has
  teeth: injecting the reversed-slice bug (bids `[:n]` not `[-n:]`) makes it fail.
- **0-size/removal unchanged:** qty "0" pops the level (gone from `levels()` and `in`); else sets/replaces —
  identical to the old plain-dict path (normalizer l2_levels 0-size filtering unaffected).
- **Dict-semantics preserved:** `SortedDict` subclasses dict and `book.bids == {plain}` is True both ways;
  `[]`/`in`/`.get`/`isinstance(...,dict)` all work. The venue processors reading `.bids`/`.asks` directly
  (binance/bybit/coinbase_advanced) + their tests pass (86-test subset green).
- **No regression:** diff touches ONLY PriceLevelBook; OrderIdBook/kraken_crc32/REV-013 machinery untouched;
  Kraken CRC byte-exact; all 361 pass.

## Timing (micro-benchmark, 80k-level book)
NEW incremental **2.55 µs/event** vs OLD full-sort **17,577 µs/event** → **~6887×**; per 2M-event hour
~5.1 s vs ~586 min (matches the observed stall). This unblocks the full 3-constituent 2.2 study — Coinbase
re-normalizes in seconds/hour.

## Findings (LOW, non-blocking)
- **F1 (applied):** `.bids`/`.asks` iteration is now ascending-by-price (SortedDict), not insertion order.
  No current caller iterates the book attribute for order (the index layer's `for lv in bk.bids` /
  `self.bids[0]` operate on `index.Book`, a different class; venue processors iterate the RAW snapshot lists,
  not the book attribute). Added a docstring note so a future caller doesn't assume insertion order.
- **F2 (housekeeping):** the working tree has untracked study scratch (`notes/_tmp_*`, study-2.2 helpers) —
  committed ONLY base.py + the test file, not the scratch.
