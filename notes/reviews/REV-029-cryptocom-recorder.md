# REV-029 — Crypto.com Exchange v1 `book` (L2) recorder + base.py heartbeat-response seam

- Date: 2026-10-21
- Reviewer: red-team-reviewer (read-only)
- Scope: NEW `src/kbtc/recorders/cryptocom.py` (CryptoComL2Processor, venue `cryptocom`,
  channel `book_btcusd`), `tests/test_recorders_cryptocom.py`, `tests/fixtures/cryptocom/book.jsonl`;
  MOD shared core `base.py` (outbound/take_outbound seam + run_recorder drain), `health.py`,
  `resync.py`, `run.py`, `normalize/pipeline.py`, `normalize/extract.py`, `docs/feeds.md`, `docs/facts.md`.
- Gates: DEPLOY of a new live BRTI-constituent feed on Ashburn, AND a change that rides in every recorder.
- Tests: `uv run pytest -q` → **385 passed** (matches expected).

## Verdict: PASS-WITH-CAVEATS

- **base.py seam is SAFE to ride in all recorders.** Proven no-op for existing venues (see below).
- **Crypto.com is SAFE to DEPLOY now as an additive, read-only capture feed** on Ashburn. It is public/
  no-auth, no order path, no secrets. Recording it cannot hurt anything.
- **Caveat gating CONSUMPTION (not recording):** one UNVERIFIED data-quality point (F1, depth-window
  eviction) must be closed before this constituent's depth/mid is trusted in the BRTI replica / fair value.

## Seam safety proof (highest-priority ask)

- (a) Strict no-op for existing venues: `grep -rn '\.outbound' src/` shows the only `.append` is
  `cryptocom.py:94`. `base.py:476` initialises `self.outbound=[]` in `MessageProcessor.__init__`; every
  processor calls `super().__init__` (else they'd lack `.gaps/.synced` and already be broken), so all have
  an empty `outbound`. `take_outbound()` returns `[]` for them → the new `for frame in p.take_outbound()`
  loop (`base.py:731`) iterates zero times. All venue tests stay green (385 pass). Zero behavior/hot-loop change.
- (b) Crash/wedge safety: `await _send(...)` on a dead socket raises → caught by the outer
  `except Exception` → normal reconnect (existing semantics). A queued frame is always a dict →
  `json.dumps` cannot fail. Book processing is not dropped: `process()` already ran before the drain.
- (c) 5 s echo deadline under load: the heartbeat is answered in the same sequential loop, but Crypto.com
  runs on its OWN dedicated connection/processor (separate feed), not sharing the loop with Coinbase's
  ~40k-level snapshots; frames here are depth-50 (tiny). Reply latency is sub-ms + one `ws.send`. Safe.
- (d) Processor stays pure/socket-free: it only appends to a list; the IO layer sends. Confirmed.
- (e) Drain path coverage: `run_recorder` is `# pragma: no cover` (needs a live socket), so the actual
  `await _send(take_outbound())` wiring is NOT unit-tested — only the processor-side contract is. See F2.

## Findings (by severity)

### F1 — MEDIUM (verify before relying on depth/mid) — depth-50 window eviction is UNVERIFIED, undetectable
`book.BTC_USD.50` is a depth-CAPPED feed. `PriceLevelBook` (base.py:183) has no `max_size`/trim, and
`_apply_levels` (cryptocom.py:155) does an absolute SET with qty-0 removal only. If Crypto.com does NOT
emit an explicit qty-0 remove when a level is pushed OUT of the top-50 window (e.g. price rises and the
old rank-50 bid becomes rank-51), that level LINGERS forever until a reseed. The pu-chain cannot catch
this (pu stays consistent), and the `cs` checksum is deliberately UNUSED (F-12). Failure mode: after a
large move the stale "ghost" level can resurface as best_bid/ask — exactly the Bitstamp-reseed class of
bug (REV-026). For a lead signal this is tolerable; for a BRTI CONSTITUENT feeding fair value it is not,
until verified. Note: docs/feeds.md §5C claims `PriceLevelBook` has a "default max_size" — it does not;
that doc line is wrong and masks this gap.
Fix (any one, cheapest first): (1) trim each side to the top-50 after every apply — this exactly matches
the venue's window semantics and evicts the ghost; (2) confirm from the doc/wire that the venue sends
qty-0 removes for evicted levels and record it; (3) add a periodic reseed; and regardless (4) run a
tracking-error study vs the other constituents (EXP-002 style) before this feed is admitted to the replica.

### F2 — LOW — the new send branch in run_recorder is unexercised by automated tests
The processor queues/drains are tested (`take_outbound()` asserted twice in
`test_heartbeat_queues_respond_and_is_not_data`), but `run_recorder`'s `for frame in p.take_outbound():
await _send(...)` line itself is pragma-no-cover. The end-to-end "reply actually leaves the socket within
5 s and the server accepts it" is covered only by a one-shot manual probe.
Fix: after deploy, watch the box for a ~30–35 s disconnect/reconnect cycle (the signature of a missed
heartbeat). Optionally add a fake-ws integration test asserting the drained frame is sent.

### F3 — LOW — heartbeat cadence and resubscribe nonce reuse are thinly verified (already flagged F-13)
30 s cadence / 5 s deadline observed once (honestly flagged in facts.md F-13). `resubscribe_messages`
reuses the SAME `nonce` and `id:1` for the unsubscribe+subscribe pair; for public market data the nonce
is not replay-protection so this is almost certainly fine, but "nonce must strictly increase" is not
confirmed. Monitor-after; not deploy-blocking.

### F4 — INFO — offline normalize never drains `outbound`
During batch re-normalize, heartbeat replies accumulate on `self.outbound` (never drained, no socket).
~1 entry / 30 s of capture → negligible, harmless. Noted for completeness.

## What is CORRECT (checked, not trusted)

- pu-chain: snapshot anchors `last_u` (cryptocom.py:117); delta requires `pu == last_u` else `_emit_gap`
  (→ needs_resnapshot via base `_emit_gap`, base.py:490-493) + `awaiting_snapshot`, delta DROPPED; gap
  logged ONCE (`_gap_flagged`); stale deltas dropped while awaiting reseed; reseed snapshot re-anchors
  silently. Reconnect reseed (no begin_resync) rebuilds via the `update`-absent discriminator.
- Tests have teeth: `test_pu_chain_gap_then_in_band_reseed` asserts gap reason/expected(1002)/got(9999),
  `awaiting_snapshot`, and that the book was NOT mutated — all fail if the chain check regresses.
  Snapshot/delta content assertions fail if the discriminator flips.
- SET semantics + qty "0" removal correct (PriceLevelBook.set, base.py:207-214); Decimal at boundary.
- Normalizer parity: pipeline `_is_recorded_complete_snapshot` mirrors the processor's `update`-absent
  discriminator; `extract.exch_ts_ns` reads `result.data[0].t` (ms→ns). `test_normalizer_completeness`
  reconstructs the same book offline and emits zero trades (book channel has no tape — correct by design).
- health `_is_data_cryptocom`: counts book frames (result.channel startswith `book` AND data truthy),
  excludes heartbeat (no `result`) and bare acks — watchdog neither false-alarms nor misses a stall.
- Safety/blast radius: public/no-auth, `required_env=()`, no order endpoint, no guard/.claude/.settings
  touch, no new deps. Fixture faithful to the verified wire schema (snapshot_fields / delta_fields).

## What would change the verdict to PASS

- F1 closed: either a trim-to-depth safeguard added, OR the venue's eviction semantics verified and
  recorded, PLUS a tracking-error study admitting the constituent — then PASS for consumption.

## Must-fix-before-RELYING-on-feed (consumption): F1.
## Monitor-after-deploy: F2 (heartbeat echo actually works on the box), F3.
## Deploy as additive capture: approved now.
