# REV-015 — Red-team review: ROADMAP 1.1d (Coinbase Advanced Trade retail `level2` adapter)

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/coinbase_advanced.py`, `tests/test_recorders_coinbase_advanced.py`,
  `tests/fixtures/coinbase_advanced/level2.jsonl`, wiring in `src/kbtc/recorders/{run,resync,health}.py`
  and `src/kbtc/normalize/{pipeline,extract}.py`, `docs/feeds.md` §5A + `docs/facts.md`.
- **Verdict:** **PASS-WITH-CAVEATS** (provisional build), + F2/F3 fixed and re-confirmed.
  `uv run pytest -q` → 251 passed. Safety clean: `execution/guard.py`, `.claude/*`, `recorders/writer.py`
  untouched; `required_env=()` (public, no credentials); pure processor + IoConfig + subscribe builders,
  market-data WS read only, no order path.

## What it does
A recorder for Coinbase's RETAIL Advanced Trade WS `level2` channel — a 3rd BRTI constituent, free/public,
DISTINCT from the institutional Exchange `full`/`level3` adapter ($5k-blocked). Endpoint
`wss://advanced-trade-ws.coinbase.com`; subscribes to `level2` + the REQUIRED `heartbeats` keep-alive;
messages arrive on channel `l2_data` with a per-connection `sequence_num`; price-level book
(`side` bid/offer, `new_quantity` full size, `"0"` = remove). Feed `coinbase-advanced-level2`, writer path
`coinbase_advanced/level2` (isolated from `coinbase/{full,level3}`). Spec VERIFIED from
`docs.cdp.coinbase.com` (2026-10-17), recorded in feeds.md §5A + facts.md.

## Verified correct (independently re-derived)
- **Gap detection over the FULL connection stream:** the `sequence_num` check runs on every message before
  the `chan != l2_data` early-return, so heartbeats (seq 1,4) and the subscriptions ack (seq 0) advance the
  counter and never read as gaps; the real jump 5→8 logs exactly one gap (expected 6, got 8), arms
  `needs_resnapshot`, and the gapped update is NOT applied. A `snapshot` re-anchors `last_seq` BEFORE the
  numeric check (so a reconnect's restarted-low seq isn't mis-read) and can't mask a real gap (a snapshot
  rebuilds the book from scratch). Duplicate/lower seq ignored, book untouched.
- **Price-level book** (reuses the tested `PriceLevelBook`): new price SETS; existing price OVERWRITES (not
  additive); `"0"` removes; remove of a nonexistent level is a clean no-op; `offer`→ASK/`bid`→BID; unknown
  side token ignored (not mis-sided); Decimal from exact strings, no float.
- **Path isolation:** `make_processor`/`_DATA_CLASSIFIERS` route on EXACT venue string (`coinbase_advanced`
  before `coinbase`); no substring routing exists → no misrouting with the Exchange adapter.
- **Completeness:** first `l2_data` snapshot → `synced=True` (a COMPLETE constituent book offline, unlike
  Bitstamp); gap → `pending_resync`; resync snapshot → back to True.
- **Primary-source fidelity:** feeds.md/facts.md record endpoint, public/no-JWT, subscribe (+heartbeats),
  the `level2`→`l2_data` channel-name mismatch, the update schema, and the per-connection `sequence_num`
  rule — VERIFIED with URLs; limits PARTIAL; fixture labeled synthetic; no CRC oracle → checkpoint
  METHOD_DEFERRED.

## Findings
- **F1 [MEDIUM — acceptance-gating, NOT a code defect; OPEN]:** the fixture is SYNTHETIC, authored to the
  assumed spec, so the gap/resync test proves code-matches-assumed-spec, not spec-is-real. The load-bearing
  claims (per-CONNECTION `sequence_num`, heartbeats advancing it, resubscribe→fresh snapshot) must be
  confirmed against a **live capture from the US box** before Coinbase is COUNTED as a verified constituent
  or fed to the BRTI replica (CLAUDE.md rule 4). **Provisional build only until the live-capture replay.**
- **F2 [LOW — FIXED]:** the processor never actually emits `synced=False` (on a gap the diverged book is
  frozen/not re-emitted until the resync snapshot). Docstring corrected; test strengthened to assert
  `all(r["synced"] is True)` + explicit processor-level `(synced, needs_resnapshot, awaiting_snapshot)`
  transitions across snapshot→updates→gap→resync.
- **F3 [LOW — FIXED, re-confirmed]:** a 2nd gap while `awaiting_snapshot` used to re-emit a gap + re-arm the
  resubscribe → resubscribe-thrash under sustained loss against Coinbase's PARTIAL/unknown rate limits. Now
  gated `if not self.awaiting_snapshot:` so only the FIRST gap logs+arms; later gaps during the wait are
  silent (but `last_seq` still advances, so no desync and a real post-resync gap is still caught). Matches
  Kraken/Bybit quiet-until-snapshot. New test asserts exactly one gap logged / one resync armed across two
  pre-resync gaps. Reviewer drove the processor directly to confirm no stuck-True state and no masked gap.
- F4/F5 [NIT]: `method` label "deferred_rest_snapshot" cosmetic for an in-band resync; `exch_ts_ns` uses the
  envelope `timestamp` (batch publish) not per-update `event_time` — documented, fine.

## Standing (gating the RESULT, not the build)
The 1.1 replay-test acceptance for a feed with no CRC oracle can only be CLOSED by a clean live-capture
replay. Until then: build merged, Coinbase recorded on the Ashburn box, but **not counted** as a verified
constituent — the BRTI replica still runs on 2 verified (Bitstamp + Kraken) + 1 provisional (Coinbase).
