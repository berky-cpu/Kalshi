# REV-017 — Red-team review: ROADMAP 1.1b (Kalshi orderbook WS schema correction)

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/kalshi.py`, `src/kbtc/normalize/{pipeline,extract}.py`,
  `tests/test_recorders_kalshi.py`, `tests/fixtures/kalshi/orderbook.jsonl`, `tests/test_normalize_parquet.py`,
  `tests/test_resync.py`, `docs/facts.md`, `docs/feeds.md`.
- **Verdict:** **PASS (provisional — schema-correctness fix).** `uv run pytest -q` → 257 passed. Safety clean:
  `guard.py`/`.claude/`/`writer.py` untouched; reads-only market data; no order path; no new dep.

## Why
The adapter was coded against GUESSED field names (`price`/`delta`/`yes`/`no`) and would NOT have parsed real
Kalshi frames. Schema re-verified from the official doc (docs.kalshi.com/websockets/orderbook-updates,
2026-10-17) and the adapter rewritten to it.

## Verified (re-derived, not just trusting the tests)
- **Snapshot** `_apply_snapshot_body`: reads `yes_dollars_fp`/`no_dollars_fp`, BOTH optional (`if not rows:
  continue` → a missing side makes NO empty/garbage key), levels `[price_dollars, count_fp]` → Decimal.
- **Delta** `_apply_delta`: `price_dollars` + SIGNED `delta_fp`, `new = old + delta_fp`, level removed at
  `<= 0` (delta-to-exactly-0 removes; below-0 removes with no negative resting size).
- **`msg`-wrapper robustness** `_event_body`: returns `msg["msg"]` only if a dict else the top-level object;
  flat and nested frames parse to the identical book; an `{"type":"error","msg":"..."}` string-msg frame
  correctly falls through to top-level (no mis-route). Normalizer (pipeline/extract) use the same extraction.
- **Decimal throughout** (`_dec` = `Decimal(str(x))`); no float in the book/trade paths.
- **Gap detection** per-sid +1 (seq<exp ignore, seq>exp gap+advance+drop, ==exp apply); seeded-gap fixture
  detects it and does not apply the gapped delta.
- NIT (reconcile): facts.md listed unsubscribe-via-`sids` as VERIFIED though the code marks it inferred —
  wording aligned (now INFERRED/UNVERIFIED).

## LIVE-WIRE CAPTURE 2026-10-17 (closes the schema ambiguities; found a new blocker)
Captured a real authed KXBTC15M orderbook (box, `KXBTC15M-26OCT172000-00`), which **confirms**:
- Payload is **NESTED under `msg`** (the doc example was flattened). Adapter's robust handling is correct.
- Field names/types/signs exactly as above; real snapshot ~40k levels, dollar-string prices
  ("0.0010"), fixed-point sizes ("1226720.00"), `delta_fp="-100.00"` signed.
- `sid` per-subscription; **`seq` per-sid and RESETS to 1 on each reconnect** — handled correctly: each
  `orderbook_snapshot` re-anchors `_seq_by_sid[sid] = seq` before the gap check (kalshi.py:149), so a
  post-reconnect delta stays in-order.
- `trade` payload NOT exercised (no trade in the window) → trade units stay UNVERIFIED.

**NEW pre-deploy blocker (connection stability):** the capture showed the Kalshi WS dropping the connection
~every 4 s (16 `subscribed` acks / 5 snapshots in 22 s). The recorder sends NO keepalive
(`ping_interval_s=None`, no `ping_message`). The 24/7 Kalshi book recorder must not be enabled until the
Kalshi WS keepalive is added and the close reason is confirmed. Schema correctness is unaffected.

## Status (Round 1 — superseded by Round 2 below)
Schema VERIFIED (doc + live wire). Adapter parses real frames correctly. Round 1 flagged a "keepalive"
reconnect blocker and an unverified trade payload — **both resolved in Round 2**: the reconnect was the
old adapter's `KeyError` (not keepalive), and the trade schema is now live-verified. See Round 2.

## Round 2 — trade schema + reconnect root cause + final 1.1b sign-off (2026-10-17)
- **Reconnect mystery SOLVED (my earlier keepalive diagnosis was WRONG):** the ~4 s reconnect was the OLD
  adapter raising `KeyError: 'price'` on every `orderbook_delta`; `run_recorder` catches ANY `process()`
  exception as a disconnect and reconnects. With the verified-schema adapter deployed the connection is
  STABLE — a live box run captured **10,113 frames in 22 s on a single connection, 0 processor crashes**
  (`subscribed:3`), and a direct WS probe was stable 40 s / 27k frames with AND without ping. No keepalive
  needed. The wrong "no-keepalive, drops every 4 s" note was removed from facts.md.
- **Trade schema VERIFIED on the live wire** and the adapter/normalizer corrected: real trades are nested
  under `msg` = `{trade_id, market_ticker, yes_price_dollars, no_price_dollars (dollar strings), count_fp
  (fixed-point), taker_side (yes/no), taker_outcome_side, taker_book_side (bid/ask), is_block_trade, ts,
  ts_ms}` — NOT the old `yes_price` cents-int guess. `_kalshi_trades` emits price=`yes_price_dollars`
  (Decimal, YES convention), size=`count_fp`, side=`taker_side`, `exch_ts_ns` from `ts_ms`. Fixture's trade
  frame replaced with the real nested schema (orderbook stays flat → both shapes exercised).
- **Legacy-fallback hardened (red-team LOW, applied):** the silent `yes_price`/`count` fallback is removed;
  a missing verified field now logs a WARNING (`kalshi_trade_schema_drift`) and skips the trade, so a future
  Kalshi field rename surfaces loudly instead of silently mis-parsing a differently-scaled field.
- **Red-team Round 2: PASS.** 1.1b SCHEMA acceptance MET — orderbook + trade both live-verified, adapter
  parses real frames with 0 crashes, seq-reset-on-reconnect independently confirmed (snapshot re-anchors
  `_seq_by_sid`), docs corrected and sourced, synthetic guesses replaced by live-captured truth. 257 tests.
- **Carried to 1.3 (deploy phase), NOT 1.1b schema:** continuous 24/7 capture needs dynamic ticker
  management (KXBTC15M rolls a new market every 15 min vs a static `KBTC_KALSHI_TICKERS`); the in-session
  resubscribe-without-disconnect resync re-emitting a snapshot, and the unsubscribe-`sids` envelope, stay
  INFERRED (the full reconnect→snapshot→re-anchor path IS verified). A real block trade
  (`is_block_trade=true`) should be spot-checked once seen.
