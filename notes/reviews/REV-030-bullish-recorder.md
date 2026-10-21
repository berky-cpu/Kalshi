# REV-030 — Bullish Exchange public `l2Orderbook` (L2) recorder

- Date: 2026-10-21
- Reviewer: red-team-reviewer (adversarial, read-only)
- Scope: NEW `src/kbtc/recorders/bullish.py` + tests/fixture + wiring (resync/run/health/normalize/docs).
  3rd in the gemini (REV-028) / cryptocom (REV-029) public-L2 series.
- Gate: DEPLOYING a new live recording feed on Ashburn, and (separately) CONSUMING it in the BRTI replica.
- Tests: `uv run pytest -q` → **395 passed** (confirmed).

## Verdict: PASS-WITH-CAVEATS

- **Safe to DEPLOY now as an additive RECORDING feed on Ashburn.** No must-fix blockers for recording.
  `run_recorder` persists every raw frame untouched before the processor runs, so recording fidelity
  does not depend on any of the processor-logic caveats below. Blast radius is clean (see F5).
- **CONSUMING in the replica is GATED** on the unverified `type:"update"` incremental path (F1 / docs
  F-16) and, to a lesser degree, on characterizing snapshot cadence+depth (F2 / F-17). These are
  reconstruction-side concerns, not recording concerns — exactly as the author scoped them.

Must-fix-before-DEPLOY (recording): none.
Must-fix-before-CONSUME (replica): F1.
Monitor-after: F2, F3.

---

## Findings (ranked)

### F1 — MEDIUM (consume-gate, NOT deploy-gate): the `type:"update"` path is unverified AND its tests are self-referential
- Evidence: `src/kbtc/recorders/bullish.py:141-162` (defensive update path, absolute-SET, `qty "0"`
  removal via `PriceLevelBook.set`), honestly tagged UNVERIFIED at `bullish.py:37-41`; docs F-16
  (`docs/feeds.md`) and `docs/facts.md`. Tests `tests/test_recorders_bullish.py:134-184` exercise the
  update path only with INLINE synthetic frames (`_snap`/`_update` helpers, lines 122-131) that encode
  the *assumed* semantics; the real fixture `tests/fixtures/bullish/l2.jsonl` is snapshot-only (matches
  the wire: 43/43 snapshots).
- Why it matters: the update-path tests assert the code does what the code assumes — they give zero
  independent evidence the assumption matches real Bullish L2 updates. If real updates are depth-capped
  diffs (or signal removal by omission rather than `qty "0"`), the absolute-SET path could leave ghost /
  stale levels — the exact REV-029 F1 class. The snapshot hot path is genuinely immune (full clear+rebuild,
  `_reset_from_snapshot` at `bullish.py:174-177`), so this is purely an update-path risk.
- Mitigating: the path is NOT the hot path, is never hit on the current wire, is correctly disclosed as
  UNVERIFIED, and on a chain break it arms the in-band resync + drops stale updates (`bullish.py:151-158`)
  rather than blindly applying — so even if wrong it fails toward resync, not silent corruption, on a gap.
  But a *gapless* wrong-semantics update would still corrupt silently.
- Fix (before CONSUME): elicit real `type:"update"` frames in a longer capture (or get the Bullish L2
  update-delta/removal doc), verify absolute-SET + removal signaling + depth behaviour, then admit to the
  replica. Optional hardening until then: have the processor treat any `type:"update"` as a gap/resync
  trigger (force a fresh snapshot) instead of applying the unverified delta — maximally safe, and costs
  nothing while updates are never seen.

### F2 — LOW (monitor-after): snapshot cadence is uncharacterized vs the 120 s staleness watchdog
- Evidence: `IoConfig.stale_timeout_s` default 120 s (`base.py:582`); `_is_data_bullish` counts only
  `V1TALevel2` frames as DATA (`health.py` added block), correctly excluding acks/keepalive-pongs. Cadence
  and depth explicitly unrecorded — docs F-17.
- Why it matters: if BTCUSD L2 snapshots are change-driven (not a fixed heartbeat) and the book ever goes
  quiet for >120 s, the watchdog would force a reconnect. This is self-healing (reconnect → fresh snapshot)
  and recording-safe, but produces reconnect churn / log noise. For a continuous BTCUSD book this is very
  unlikely, so low severity.
- Fix: measure snapshot cadence over a longer capture (F-17). If change-driven with plausible quiet spells,
  raise `stale_timeout_s` for this feed in `run.py`. No action needed to deploy.

### F3 — LOW / informational: snapshot-path seq gap is log-only by design (correct, but note the implication)
- Evidence: `bullish.py:124-139` — on a `sequenceNumberRange` chain break between snapshots, `_emit_gap`
  fires (recorded on `self.gaps`, logged at WARNING) but `needs_resnapshot` is explicitly cleared to False
  (`bullish.py:135`), so no resync is armed. `_emit_gap` sets `needs_resnapshot=True` via the base
  `resync_on_gap` default; the explicit `=False` immediately after is what produces the self-heal, and the
  live loop checks `needs_resnapshot` only after `process` returns — so order is correct. Verified by
  `test_sequence_gap_on_snapshot_logged_but_self_heals`.
- This is the right behaviour (each snapshot is a complete, authoritative book). Implication worth keeping
  in mind for any microstructure study: a logged snapshot-path gap means intermediate book *states* between
  two snapshots were missed, even though the resulting book is complete and current. Honestly documented.

### F4 — NIT: minor discriminator drift and reused JSON-RPC id (not load-bearing)
- The processor's snapshot fallback `typ == "snapshot" or (typ != "update" and ("bids" in body or
  "asks" in body))` (`bullish.py:124`) differs slightly from the normalizer's
  `_is_recorded_complete_snapshot` (`pipeline.py`: `dataType=="V1TALevel2" and type != "update"`, no
  bids/asks requirement). The two could diverge only on a pathological `V1TALevel2` frame that has neither
  `type` nor any `bids`/`asks` key — which does not occur on the wire (snapshots always carry `type` +
  arrays). Immaterial, but a future divergence point if the wire ever changes.
- `subscribe_messages` and `resubscribe_messages` reuse `id:"1"` for both unsubscribe and subscribe
  (`bullish.py:210-225`). Harmless because no code correlates JSON-RPC responses by id; purely cosmetic.

### F5 — POSITIVE: safety / blast radius clean (verified)
- `base.py` is NOT in the diff (confirmed via `git diff --name-only`), as the author claimed.
- No touch to `execution/guard.py`, `.claude/`, or `settings.json` (confirmed).
- `bullish.py` imports only `json`, `typing`, and `.base` — no new third-party deps, no network/order code.
- Public, no-auth market-data WS; `required_env=()`; no secret path (`run.py` `_build_bullish`, FEEDS entry).
- Wiring edits are additive and mirror the gemini/cryptocom seams (imports kept alphabetical in
  resync.py/run.py; health classifier + make_processor + data-classifier registry; extract.py exch_ts;
  pipeline.py complete-snapshot discriminator). Full suite green (395), so gemini/cryptocom/other venues
  did not regress.

---

## Checklist resolution (against the launcher's hunt list)

1. Snapshot-only reality vs UNVERIFIED update path:
   (a) clear+rebuild correct and ghost-immune — YES (`_reset_from_snapshot` clears then rebuilds; every
   frame authoritative). (b) update path correct-or-SAFE — SAFE on a gap (resync/drop), but a gapless
   wrong-semantics update could corrupt silently → F1. (c) discrimination robust — YES on the real wire
   (minor pathological drift, F4). (d) honestly disclosed UNVERIFIED — YES (code + F-16 + facts).
2. Seq-chain gap detection: snapshot path logs + self-heals, no resync, gap still recorded for
   observability — VERIFIED correct (`test_sequence_gap_on_snapshot...`). A full snapshot genuinely needs
   no resync. Update path arms resubscribe + drops stale — correct (`test_update_path...`).
3. Book correctness: flat alternating price,qty parse (`_apply_levels`, `range(0, len-1, 2)`) correct, drops
   a stray orphan safely; SET/0-removal via `PriceLevelBook`; Decimal at boundary; clear+rebuild no leak/
   mis-order (SortedDict). PASS.
4. Keepalive: ping 120 s vs 5-min idle close — safe margin. keepalive-pong classified as NOT data
   (`_is_data_bullish`), so a pong-only stream reads STALE — VERIFIED (`test_ack_and_keepalive_pong...`).
5. Normalizer parity: pipeline reuses the SAME processor via `make_processor`, so offline reconstruction is
   byte-identical book logic; `exch_ts_ns` from `timestamp` ms→ns; no trades by design — VERIFIED
   (`test_normalizer_completeness_and_exch_ts`).
6. `_is_data_bullish`: V1TALevel2 counts as data; ack + keepalive-pong excluded → keepalive-only feed reads
   STALE — VERIFIED.
7. Safety / blast radius: clean — see F5.
8. Tests have teeth: snapshot/seq-gap/self-heal/foreign-symbol/classifier/normalizer tests fail if the
   corresponding logic breaks; fixture faithful to the wire (flat arrays, sequenceNumberRange, snapshot-
   only). The ONE gap in coverage is F1: the update-path tests are self-referential (synthetic frames
   encoding the assumption), so they prove consistency, not correctness.

## What would change my mind (to full PASS incl. consume)
- A longer read-only capture that actually elicits `type:"update"` frames, confirming the absolute-SET +
  `qty "0"` removal semantics and whether evicted/depth-capped levels are removed — closing F-16.
- Measured snapshot cadence + published depth confirming the 120 s watchdog cannot false-trip and depth is
  adequate for the BRTI replica — closing F-17.
