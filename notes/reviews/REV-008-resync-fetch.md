# REV-008 — Red-team review: recorder resync-FETCH (ROADMAP 1.1 open risk "F8")

- **Date:** 2026-10-16
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/resync.py` (new), `tests/test_resync.py` (new),
  and changes to `base.py`, `bitstamp.py`, `bybit.py`, `kalshi.py`, `kraken.py`
- **Verdict:** **FAIL (Round 1) → PASS (Round 2)**, + a Round-3 fail-safe guard. `uv run pytest -q` → 101 passed.
  Safety clean: reads-only (GET snapshot fetches + WS (un)subscribe), no order path, no secret logging
  (Kraken `token` only in outbound frames, never written — writer persists inbound only); `writer.py`,
  `execution/guard.py`, `.claude/*` untouched.

## What it is
Implements the `run_recorder` `resnapshot` recovery that was stubbed (F8). Two modes: **REST-snapshot**
(Coinbase/Binance/Bitstamp — fetch + rebuild + discard stale) and **in-band resubscribe** (Kraken/Bybit —
trigger a fresh `snapshot` frame). Injectable fetch/send seams → fully unit-testable without a live socket.

## Round 1 — FAIL (silent book-corruption)
- **F1 [CRITICAL]** Bitstamp reseed-only silently **double-applied** stale events (no anchor to discard
  against, unlike Coinbase `seq` / Binance `lastUpdateId`) → a WRONG BRTI book on essentially every resync.
- **F2 [HIGH]** The "buffer-during-fetch then replay-discard" path was dead in prod (inline await suspends
  the read loop); the real protection is the post-snapshot live-path guard, which Bitstamp lacked.
- **F3 [MED]** Kalshi bare resubscribe could double-count (second concurrent sid) + fired on initial connect.
- F4 (Bybit/Kraken resubscribe thrash), F5 (Kraken token placeholder → perpetual gap), F6 (dead-path tests).

## Round 2 — PASS
- **F1 FIXED:** Bitstamp `apply_snapshot` records the REST snapshot's `microtimestamp` as a FLOOR; the
  live path drops events `microtimestamp <= floor` before touching the book/anchor (true analogue of
  Coinbase `seq <= S`). `<=` is the safe direction (errs to a negligible single-event under-apply, never
  systemic double-apply). Pinned by a stale-event-unchanged test.
- **F2 FIXED:** discard model documented as the post-snapshot live-path guard; every REST venue has one;
  tests exercise the real path. **F4/F5 FIXED** (anti-thrash `awaiting_snapshot`; Kraken token required,
  raises on placeholder). **F6:** tests rewritten to pin F1 + the real guard.
- **F3 PARTIAL:** initial-connect no longer double-subscribes (`resync_on_connect` flag). Reviewer corrected
  my brief — Kalshi's worst case is a real silent double-count after the fresh snapshot (old sid resumes),
  not a no-op; confined to the Kalshi book, no raw-data loss, UNVERIFIED-gated (1.1b).

## Round 3 — Kalshi fail-safe
Rather than implement sid-pruning on Kalshi's UNVERIFIED protocol, Kalshi in-band resync is now a
**fail-safe no-op** (`DisabledResyncer`): a Kalshi gap logs once and clears the flag, sends no frames —
raw is still persisted, the (untrusted, 1.1b) book simply isn't recovered. Removes the double-count path
entirely. The `unsubscribe/resubscribe/awaiting_snapshot` machinery is retained for 1.1b to wire up once
verified. Test asserts the no-op (no frames, flag cleared, stream still processable).

## Standing items
- **Kalshi resync (→ 1.1b):** before Kalshi in-band resync is enabled in the live run, implement
  sid-pruning (on a resync snapshot, restrict deltas/trades to the snapshot's sid) OR use reconnect-based
  resync, with a test that an old-sid delta after the fresh snapshot leaves the book unchanged.
- **Bitstamp `order_data` `since_id`** fine-grained backfill is still a follow-up (floor + reseed is correct now).
- **Bitstamp floor** is skipped if the REST response lacks `microtimestamp`/`timestamp` (VERIFIED always
  present) — consider emitting a gap if the floor can't be determined rather than silently disabling the guard.
- `run_recorder` remains `# pragma: no cover` (live-socket integration); all load-bearing logic is in the
  unit-tested processors/resyncers.
