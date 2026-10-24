# REV-034 — Kalshi-relayed CF Benchmarks index-value recorder (`kalshi-cfbenchmarks`)

- Date: 2026-10-24
- Reviewer: red-team (adversarial, read-only)
- Scope: NEW `src/kbtc/recorders/kalshi_cfbenchmarks.py` + tests + fixture; MOD shared wiring
  (`run.py`, `resync.py`, `health.py`), `docs/feeds.md`, `docs/facts.md`, `deploy/*`.
- Mission framing: market-data READ only (no order path); additive new feed/systemd instance.

## Verdict: PASS-WITH-CAVEATS

Safe to commit and deploy as a new additive recorder instance. The shared-wiring edits are safe
for the existing feeds (market/orderbook recorder behaviour is byte-identical). No must-fix before
deploy. Two monitor-after items and the three pre-flagged UNVERIFIED items (F-18/F-19/F-20) do not
block recording raw.

`uv run pytest -q` -> **422 passed** (confirmed).

---

## Safety (checklist item 8) — PASS

- No order path anywhere in the module. `urls=[ENDPOINT_PROD, ENDPOINT_PROD_LEGACY]` are the
  market-data WS hosts (reads only); no REST/order endpoint, no execution-layer import.
- The demo-order guard, the hooks directory, and the agent settings file are all UNTOUCHED
  (git status clean for all three).
- Credentials: reused via `make_kalshi_header_factory` -> `default_kalshi_signer_factory`; key id +
  PEM read from env only, never logged. The new module logs only an error `code`/`detail` on a WS
  `error` frame — no credential surface.
- `required_env = (KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH)` — correct, matches the market feed.
- No new dependencies. `deploy/systemd` + `env.example` changes are documentation/comments only;
  nothing is enabled (instances are started manually).

## 1. Isolation from the live market recorder (critical) — PASS

- Separate systemd instance `kbtc-recorder@kalshi-cfbenchmarks`, separate process, separate
  `IoConfig` (`channel="cfbenchmarks"` -> writer path `data/raw/kalshi/cfbenchmarks`), its own
  connection and its own processor instance. The market feed uses `channel="orderbook_delta"`
  (`kalshi.py:376`) -> path `data/raw/kalshi/orderbook_delta`. No path collision.
- Because they are separate OS processes there is zero shared mutable memory: a crash /
  resubscribe storm / watchdog reconnect on one cannot touch the other. The only shared thing is
  the API credential, used to auth a second connection.
- Concurrent-connection cap: this feed opens exactly ONE connection carrying BOTH channel
  subscriptions (single `IoConfig`, two subscribe frames — `kalshi_cfbenchmarks.py:186-190`,
  `subscribe_messages`). So it adds +1 authed WS per key -> 2 total with the market feed, within
  the documented 5-concurrent cap. Matches the probe observation (no disturbance).

## 2. Blast radius of shared-file edits — PASS

- `run.py`: purely additive — a new `_build_kalshi_cfbenchmarks` builder and a new
  `FEEDS["kalshi-cfbenchmarks"]` entry. The existing `kalshi` FeedDef and `_build_kalshi` are
  unchanged.
- `health.make_processor`: new branch `if c.startswith("cfbenchmarks")` is reached ONLY for the
  cfbenchmarks channel; `("kalshi","orderbook_delta")` still routes to `KalshiProcessor` (test
  `test_health_make_processor_routes_cfbenchmarks_channel` + the suite confirm).
- `_KALSHI_DATA_TYPES` union with `_CFB_VALUE_TYPES`: the classifier is venue+type keyed and the
  orderbook feed never carries `cfbenchmarks_value*` types, so the market recorder's staleness
  classification is unchanged. Guarded by `test_data_classifier_value_is_data_control_is_not`
  (asserts `orderbook_delta` still classifies as DATA).
- `resync.py`: new builder only; existing builders untouched. `normalize.pipeline.stream_kalshi`
  routes cfbenchmarks frames through the no-book processor and the `typ not in
  {orderbook_snapshot,orderbook_delta}` guard (`pipeline.py:331`) continues, so it emits no
  book/trade rows — the market normalize path is unaffected.

## 3. Per-sid seq-gap handling — PASS

- `_check_seq` is keyed by `sid` in `_seq_by_sid`; the two channels get distinct server sids (5/6
  in the fixture). A gap on one sid cannot false-trigger the other. Forward jump -> one structured
  gap (never silent) + `needs_resnapshot`; `seq < expected` -> treated as dup/out-of-order, dropped
  with NO gap (no resync storm). Covered by `test_seq_gap_detected_and_arms_resync` (expected=3,
  got=4, exactly one gap) and `test_duplicate_seq_ignored_no_gap`.
- `reset_subscriptions()` clears both `sid_by_channel` and `_seq_by_sid` and is wired into
  `subscribe_factory` in `run.py`, so a reused processor re-anchors from each fresh connection's
  acks — no stale-sid accumulation across full reconnects. Covered by
  `test_reset_subscriptions_clears_sids_and_seq`.

## 4. Single writer path (not two) — PASS

- Both channels write to one `kalshi/cfbenchmarks` path, but every frame carries a top-level
  `type` equal to the channel name (`cfbenchmarks_value` vs `cfbenchmarks_value_5hz`), so an
  offline split is unambiguous (`VALUE_TYPES` is exactly these two). The 5 Hz frame also carries
  `value_usd`/`source_ts_ms` and no `avg_60s_data`; the 1 Hz frame carries the averages — a second
  disambiguator. No information loss.

## 5. Resync = resubscribe ENABLED — CORRECT for a value stream (monitor-after)

- The rationale is sound: no book, no snapshot, so a gap = missed prints and a resubscribe has
  nothing to double-apply (contrast the orderbook recorder's `DisabledResyncer`, which exists only
  to avoid book double-count). The cfbenchmarks processor does NOT override `begin_resync`, so the
  base default runs (`synced=False` only) — crucially it does NOT set `awaiting_snapshot`, so
  recording continues uninterrupted after a resubscribe (correct; there is no snapshot to wait
  for). `apply_snapshot` is a no-op that clears `needs_resnapshot`.
- F-20 (new-sid double-receive) is genuinely benign: raw rows duplicate, deduplicable offline by
  `source_ts_ms`; no corruption. Confirmed.
- MONITOR-AFTER (edge case not in F-20): the in-band resync path (`ResubscribeResyncer.__call__`)
  does NOT call `reset_subscriptions()` — only a full reconnect (via `subscribe_factory`) does. If
  the server re-opens the stream on the *same* sid with seq reset to 1, then after a gap advanced
  the anchor (e.g. to 4), every resubscribed frame has `seq < expected` and is silently dropped
  from the in-memory `values` tail (and never re-anchors) until the next full reconnect. Impact is
  bounded to the live introspection tail: (a) RAW is written to disk BEFORE `process()`
  (`base.py:909`), so NO recorded datum is lost; (b) the staleness watchdog uses
  `monitor.observe(raw)` -> `_is_data_kalshi(raw)` which keys off the raw `type`, independent of
  `_check_seq`, so the feed is NOT falsely marked stale and does not reconnect-loop. Verdict:
  benign for the recording mission; worth a log watch. A one-line fix (re-anchor on the fresh
  `subscribed` ack in the resync path, or reset subscriptions there) would make the in-memory tail
  self-heal without a reconnect — not required before deploy.

## 6. Data completeness — PASS

- The FULL `msg` body is recorded verbatim via `_event_body` + `return [{... **body}]` /
  `values.append(body)`; `data` is kept as the transmitted JSON string (not re-parsed). Both
  averages are preserved. `last_60s_windowed_average_15min` (the forming settlement average) is
  captured when present — `test_last_60s_windowed_average_15min_captured_when_present` asserts
  value/`window_size==60`/`window_end_ts_exclusive`. Authoritative copy is the raw written by
  `writer.py` regardless of processor logic.

## 7. Benchmark / fair value — N/A

- Pass-through recorder; no fair-value model. The settlement-identity question is correctly
  deferred (F-19, UNVERIFIED) and does not block recording.

## Facts / UNVERIFIED (checklist item 6) — acceptable

- F-18 (`window_size==0` on normal `avg_60s_data`), F-19 (whether KXBTC15M settles off EXACTLY
  `last_60s_windowed_average_15min`), F-20 (double-receive on resubscribe) are all flagged
  UNVERIFIED in the module docstring, `resync.py`, `docs/feeds.md` section 5E and `docs/facts.md`.
  The recorder records raw verbatim and interprets none of them, so none block recording. Good
  discipline: the `window_size==0` quirk is stored as-is, not "corrected".

## 8. Tests have teeth — PASS (with a bookkeeping note)

- The tests fail if seq/parse/classifier logic breaks (gap detection, duplicate suppression,
  field capture incl. both averages + settlement field, 5 Hz field set, classifier DATA vs
  control, routing). The fixture is faithful: two `subscribed` acks, a 1 Hz frame, a 5 Hz frame, a
  final-minute frame carrying `last_60s_windowed_average_15min`, and a real seq gap (2->4 on sid 5).
- NOTE (not a defect): the file contains **9** test functions, not 10 as stated in the task brief.
  No parametrization. Re-count before ticking the ROADMAP line if the "10" was an acceptance
  criterion.
- Minor suggestion (monitor-after): add a dedicated cross-sid assertion (gap on sid 5 does not
  perturb sid 6's anchor) — currently only implied by the single-gap count in the shared fixture.

---

## Must-fix before deploy
- None.

## Monitor-after
1. Same-sid-reset resubscribe edge (item 5): watch logs for a cfbenchmarks stream whose
   in-memory tail goes quiet while raw keeps landing; if seen, re-anchor on the fresh `subscribed`
   ack in the resync path. No data loss risk.
2. Confirm the authed-connection count stays at 2/key once deployed (market + cfbenchmarks);
   re-check if any other authed Kalshi WS instance is added.
3. Reconcile the test-count (9 vs 10) against the ROADMAP acceptance criterion.

## What would change my mind (to FAIL)
- Evidence the second authed connection perturbs or trips the market recorder's connection (per-key
  cap, shared session) in a sustained run — the probe and code say it does not.
- A code path that lets a cfbenchmarks gap/resubscribe drop frames from DISK (it cannot: writer
  writes raw before `process`), or that re-arms resync in a tight loop (it does not: `seq<expected`
  emits no gap).
- Any order endpoint, protected-file edit, or credential in logs (none found).
