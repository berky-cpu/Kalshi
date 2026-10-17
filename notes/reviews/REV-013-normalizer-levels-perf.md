# REV-013 — Red-team review: normalizer `OrderIdBook.levels()` performance fix

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/base.py` (OrderIdBook add/modify/delete/clear/_agg_*/levels/best_*),
  `src/kbtc/normalize/extract.py` (`l2_levels` bounded-N), `tests/test_recorders_base.py`,
  `pyproject.toml`, `uv.lock`.
- **Verdict:** **PASS.** `uv run pytest -q` → 210 passed; targeted kraken+normalize_books → 22 passed.
  Safety clean: `execution/guard.py`, `.claude/*`, `recorders/writer.py` untouched; no network/order
  path; prices stay Decimal (SortedDict keys are `_dec(price)`, no float); new dep
  `sortedcontainers>=2.4.0` is a mature pure-Python lib, hash-pinned in `uv.lock`.

## The change (and why)
Found by profiling a real VPS normalize run (py-spy): `OrderIdBook.levels()` rescanned **every** resting
order and re-sorted the whole book on **every** event (O(N) per event × millions of events), called by
`_emit_book`→`extract.l2_levels` purely to detect top-N changes — a ~1 hr-per-hour pathology that hung the
normalizer. The fix adds a DERIVED per-side `price→summed-qty` aggregate (`SortedDict`) plus a
`price→order-count` map, maintained incrementally in add/modify/delete/clear; `self.orders` (OrderedDict)
stays the authoritative store (so `grouped`/`side_orders`/`kraken_crc32` time-priority is unchanged), and
`levels(side,n)` now reads top-n off the SortedDict in O(n). Reported benchmark ~396× (≈9 s/hr vs ≈60 min/hr).

## Cache-coherence hunt (the dangerous failure mode) — PASSED
The risk was the derived aggregate silently drifting from `self.orders` → a wrong-but-valid-looking book.
- **No bypass writes:** repo-wide, the only external `self.book.orders` touches are READS (coinbase.py:122
  `.orders.get`, kraken.py:132 `.orders.items`); all mutations route through add/modify/delete/clear.
- **Logic audit:** re-add/overwrite drops the old contribution first (no double-count, incl. opposite-side
  re-add); modify saves old side/price/qty before mutating (price-change = remove+add, qty-only = in-place
  delta, ts-refresh leaves agg alone, side immutable); delete-of-absent is a no-op; clear resets both maps.
- **Zero-size retention:** `_count` keeps a price key alive while ≥1 order rests even at summed qty 0, so a
  modify-to-0-without-delete still yields `(price, 0)` in `levels()` — verified identical to the OLD
  full-scan body (git HEAD), so `extract.l2_levels`'s 0-size filtering sees the same input (no behavior change).
- **Empirical:** reviewer replayed the property test's seed (confirmed it hits 233 re-adds / 74 price-changing
  modifies / 43 modify-to-0 / 840 delete-of-absent / 227 clears / 1624 steps with a retained 0-size level);
  injected 3 desync bugs into subclasses and confirmed the test's check loop CAUGHT all three; ran a
  2000-seed × 300-step stress (wide price grid, forced 0-qty top-of-book stacks) comparing `levels()` and
  `extract.l2_levels(N)` for N∈{1,2,5,10} against the OLD implementation — **zero mismatches over ~600k steps.**

## Other checks — PASSED
- **extract.l2_levels bounded-N parity:** the doubling loop stops only on enough real levels OR whole side
  returned; cannot under-fill with real levels beyond the window (the adversarial "many consecutive 0-size
  top levels" case can't break it — reasoned + covered by the stress run).
- **levels() ordering/bounds:** bids descending, asks ascending, n≤0→[], n=None→all, Decimal keys;
  best_bid/best_ask == levels(...,1) top price (incl. retained 0-qty).
- **Kraken CRC byte-exact:** time-priority path untouched; worked-example `1063832831` + real-recording
  per-update checksums still pass.

## Findings (none blocking)
- **LOW (test robustness):** the coherence guard used a single fixed seed (20251017). Reviewer showed it has
  teeth and 2000 other seeds pass, but recommended parametrizing over many seeds for shared data-integrity
  code. **→ Applied post-review** (property test parametrized over multiple seeds).
- **INFO:** the ~396× perf win was not independently benchmarked on a real multi-hour L3 replay (correctness
  was the mandate). → Will be confirmed by the re-run on the Ashburn box as the 2.1 real-data validation.
- **Unenforced invariant (watch):** coherence depends on all book mutation going through add/modify/delete;
  a future venue adapter writing `self.orders` directly, or a `modify` that changes an order's SIDE, would
  break the aggregate. None do today; worth a guard/comment when the next adapter lands.
