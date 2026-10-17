# REV-011 — Red-team review: ROADMAP 1.1c (persist REST snapshots into recordings)

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/{resync,base}.py`, `src/kbtc/normalize/{pipeline,schema}.py`,
  `tests/test_resync.py`, `tests/test_normalize_books.py`, new fixtures
  `tests/fixtures/{coinbase/full_with_snapshot,bitstamp/live_orders_with_snapshot}.jsonl`.
- **Verdict:** **PASS** (Round 1), + N1/N2/N3 robustness follow-ups folded in. `uv run pytest -q` → 161 passed.
  Safety clean: no order path, persisted `body` is public order-book JSON (no secret), writer.py/guard/.claude
  untouched (the guard hook fired on a `.claude/`-referencing command, confirming it's live).

## What it does
A tagged envelope `{"__kbtc_rest_snapshot__":1,"venue","channel","body":<raw REST snapshot>}` is written through
the existing writer when a `RestSnapshotResyncer` fetches a snapshot (initial sync + resync), stamped recv_ts_ns
at fetch, persisted BEFORE apply. The normalizer routes a tagged record's `body` to `apply_snapshot` →
`synced=True` complete baseline. In-band resyncers (Kraken/Bybit/Kalshi) have no `persist` (their snapshot
already rides the WS stream). Snapshot-less (old) recordings stay `synced=False` — backward-compatible.

## Round 1 — PASS (hand-verified)
- **Tag collision — none, both directions.** `parse_rest_snapshot_record` only matches a dict with the dunder
  key; no venue payload (incl. Coinbase level3 positional arrays) can carry it, and the envelope is parsed at
  step-0 and `continue`d so no processor ever ingests it.
- **Ordering / rebuilt book correct.** On a gap: gap frame written (rejected by the per-venue guard), then the
  snapshot persisted (recv_ts > gap frame), then later deltas — file order = causal order; replay re-anchors
  correctly and discards stale deltas via the same live guard (Coinbase seq, Binance lastUpdateId, Bitstamp
  micro floor). The complete-book assertions are hand-computed (pre-existing resting orders present), not tautological.
- **`synced` logic:** once a persisted snapshot anchors, a later gap keeps `synced=False` until the next
  persisted snapshot (no silent empty-reseed); no path emits `synced=True` on an incomplete/post-gap book.
- **No regression:** in-band resyncers never wire `persist`; persist-before-apply records the snapshot even if
  apply raises; 101 prior resync/recorder tests still green; base.py import-clean (no circular dep).

## Folded-in follow-ups (LOW)
- **N1** — the normalizer now validates the envelope's `venue`/`channel` against the feed and skips+logs a
  mismatch (never seeds the wrong processor). Test: a cross-wired envelope is skipped, feed stays `synced=False`.
- **N2** — step-0 `apply_snapshot` wrapped in try/except: a malformed `body` is skipped+logged, baseline stays
  unset (`synced=False`), the feed keeps processing (no replay-crash loop). Test: garbage body skipped, then a
  valid snapshot recovers the baseline.
- **N3** — the bitstamp fixture gained post-reseed events; the test now asserts a stale event (micro ≤ reseed
  floor) is dropped in the replay path and a fresh event re-anchors.

## Standing (non-blocking)
- **N4 / info:** `run_recorder`'s live persist wiring stays `# pragma: no cover` (needs a live socket); the
  persist seam + helpers are unit-tested directly. Exercised for real by the 1.3 VPS run.
- Live per-venue REST snapshot JSON shape vs `apply_snapshot` expectations stays UNVERIFIED until the live run
  (the persist layer stores `body` untouched regardless). Kalshi excluded (fail-safe DisabledResyncer, 1.1b).
