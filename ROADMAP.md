# Roadmap

Each item lists an owner agent and acceptance criteria. Tick an item only when the
criteria are met, tests pass, and `red-team-reviewer` has signed off (link the review).

## Phase 0 — Verify the ground truth (owner: lead + quant-modeler)

- [x] **0.1 Contract terms.** From Kalshi's current KXBTC15M rulebook/contract terms, record in
  `docs/facts.md`: settlement source (BRTI?), averaging window and sampling, how the
  strike/reference is set, open/close times, tick size, position limits.
  *Accept:* every field has a source URL + date; `pricing.py` defaults updated if any differ.
  *Done 2026-10-16:* [facts](docs/facts.md), [ADR-001](docs/decisions/ADR-001-settlement-model.md),
  review [REV-001](notes/reviews/REV-001-roadmap-0.1-contract-terms.md) (PASS). Window (60×1 s)
  confirmed; contract is UP/DOWN (≥, ties→YES); strike = realized open-window average.
  *Open:* 1 s spacing/window-endpoint is PARTIAL (inferred); position hard cap UNVERIFIED.
- [x] **0.2 Fees.** Pull `GET /series/KXBTC15M` (`fee_type`, `fee_multiplier`) and the latest
  fee schedule PDF. *Accept:* `fees.py` constants confirmed or corrected; test added.
  *Done 2026-10-16:* [facts](docs/facts.md), review [REV-002](notes/reviews/REV-002-roadmap-0.2-fees.md)
  (PASS), note [fee-curve](docs/derivations/fee-curve.md). API primary-verified `fee_type="quadratic"`,
  `fee_multiplier=1` → taker 0.07, maker-free; constants unchanged, `effective_taker_rate` added.
  *Open:* 0.07/0.0175 coefficients, round-up/per-order, maker-free are PARTIAL (single-source
  corroboration; Kalshi fee PDF 429'd) — needs a human/authenticated PDF fetch to reach primary.
- [x] **0.3 BRTI methodology.** From the CF Benchmarks methodology guide + constituent list:
  constituents, publication cadence (1 s vs 200 ms), aggregation steps (order-book
  mid/“price-volume curve”, spacing, deviation filters, weighting).
  *Accept:* written spec in `docs/brti-spec.md` precise enough to implement.
  *Done 2026-10-16:* [brti-spec](docs/brti-spec.md), [facts](docs/facts.md), review
  [REV-003](notes/reviews/REV-003-roadmap-0.3-brti-spec.md) (PASS, Round 2). Methodology v17.0;
  7 constituents (itBit suspended 27-Jul-2026); 200 ms internal / 1 Hz top-of-second standard feed;
  full aggregation math transcribed and independently re-verified by red-team.
  *Open (→ Phase 2, empirical):* λ=10.3/v_T operator (O-2, top priority) and cap mean trimmed-vs-
  winsorized (O-4) feed every print; settlement-window endpoint (O-5) + which feed cadence (O-6);
  recorder coverage of the 7 constituents + USDC mapping (O-7/O-8).
- [x] **0.4 Feed specs.** For Coinbase `level3`/`full`, Kraken v2 `level3`, Bitstamp
  `live_orders`, Binance/Bybit perp: endpoint, auth, subscription message, sequence
  numbers, snapshot/resync procedure, rate limits. *Accept:* `docs/feeds.md`.
  *Done 2026-10-16:* [feeds](docs/feeds.md), [facts](docs/facts.md), review
  [REV-004](notes/reviews/REV-004-roadmap-0.4-feeds.md) (PASS). Gap-detection re-verified vs primary:
  Coinbase `sequence`, Kraken CRC32 checksum, Binance `pu` (≠ spot), Bybit snapshot-reset.
  *Open (→ Phase 1.1):* Bitstamp WS v2 page Incapsula-blocked (host/envelope/limits PARTIAL);
  Binance diff-depth speeds + aggTrade/limits PARTIAL (SPA unrendered); Kraken 3-way host conflict;
  Coinbase `level3` resync inferred. **Phase 0 complete.**

## Phase 1 — Record everything (owner: data-engineer)

- [x] **1.1** Venue adapters on top of `recorders/writer.py`: Kalshi (`orderbook_delta`,
  `trade`, market lifecycle), Coinbase L3, Kraken L3, Bitstamp live orders, Binance + Bybit
  perp L2/trades. Gap detection via sequence numbers, auto-reconnect + resnapshot.
  *Accept:* each adapter has a replay test on a recorded fixture; gaps are logged, not silent.
  *Done 2026-10-16 (five crypto venues):* `src/kbtc/recorders/{base,coinbase,kraken,bitstamp,binance,bybit}.py`
  + replay tests w/ seeded gaps (65 pass). Review [REV-005](notes/reviews/REV-005-roadmap-1.1-recorders.md)
  (FAIL→PASS, Round 2). Split IO layer vs. pure processor; `resync_on_gap` keeps trade-tape gaps log-only;
  Kraken CRC32 externally validated vs the documented example (`1063832831`). **Kalshi split to 1.1b.**
  *Resync-FETCH (F8): DONE 2026-10-16* — `src/kbtc/recorders/resync.py` (REST-snapshot for Coinbase/Binance/
  Bitstamp; in-band resubscribe for Kraken/Bybit), review [REV-008](notes/reviews/REV-008-resync-fetch.md)
  (FAIL→PASS; R1 caught a Bitstamp silent book-corruption, fixed via a microtimestamp floor). 101 tests pass.
  *Open:* Bitstamp endpoint/envelope now VERIFIED (human fetch); Binance stream speeds/aggTrade stay PARTIAL
  (doc pages gone). `run_recorder` live-socket loop still unit-untested (exercised by the 1.3 VPS run).
- [x] **1.1b Kalshi WS adapter — schema finalized + live-verified 2026-10-17.** Quick-start WS doc (human fetch, 2026-10-16) VERIFIED the
  connection layer: prod/legacy/demo endpoints, auth headers + signing string, subscribe
  `{id,cmd,params:{channels,market_tickers}}`, unsubscribe via `sids`, message types, reconnect+keepalive
  — all match `recorders/kalshi.py`. **Still UNVERIFIED** (the quick-start lacks it; needs the dedicated
  **channel-reference** page, not the quick-start): the `orderbook_delta`/`orderbook_snapshot` **payload
  schema** (yes/no vs bid/ask; price format — note ticker uses `_dollars` strings) and the **seq gap
  semantics** (quick-start shows no top-level `seq`). *Accept:* verify payload + seq from the channel
  reference, replace the synthetic fixture with a captured real frame, record in `docs/feeds.md` +
  `docs/facts.md`, red-team sign-off. Owner: data-engineer.
  *Also (from REV-008):* Kalshi in-band **resync is a fail-safe no-op** until this is done — before enabling
  it in the live run, add **sid-pruning** (on a resync `orderbook_snapshot`, restrict deltas/trades to the
  snapshot's sid) OR reconnect-based resync, with a test that an old-sid delta after the fresh snapshot
  leaves the book unchanged.
  *Schema fixed + VERIFIED on the live wire 2026-10-17:* the adapter had GUESSED field names
  (`price`/`delta`/`yes`/`no`) and would not have parsed real frames; rewritten to the official schema
  (`yes_dollars_fp`/`no_dollars_fp` `[price_dollars,count_fp]` pairs; delta `price_dollars` + SIGNED
  `delta_fp`; dollar-string prices, Decimal). An authed capture from the box (Berk provisioned a read-only
  Kalshi key) **confirms on the wire**: payload NESTED under `msg`, `seq` per-sid resetting to 1 per
  reconnect (handled — each snapshot re-anchors), ~40k-level snapshots. Review
  [REV-017](notes/reviews/REV-017-roadmap-1.1b-kalshi-orderbook-schema.md) (PASS). **257 tests.**
  **`trade` schema also VERIFIED on the wire:** nested `msg` `{trade_id, yes_price_dollars/no_price_dollars
  (dollar strings), count_fp, taker_side yes/no, taker_outcome_side, taker_book_side bid/ask, is_block_trade,
  ts/ts_ms}` — the normalizer now emits price=`yes_price_dollars`/size=`count_fp` (was a wrong cents-int
  guess); the silent legacy fallback is replaced by a loud schema-drift WARNING.
  **Reconnect root cause (corrected):** an earlier note wrongly blamed "no keepalive — drops ~every 4 s".
  That was the OLD adapter raising `KeyError: 'price'` on every delta (`run_recorder` reconnects on any
  `process()` exception). With the fixed adapter the connection is STABLE — a live box run captured
  **10,113 frames / 22 s on ONE connection, 0 crashes**; a direct WS probe held 40 s / 27k frames with and
  without ping. No keepalive needed. Box has Kalshi creds (key in `/etc/kbtc/env`, PEM `640 root:kbtc`).
  **Schema DONE.** Carried to **1.3 (deploy)**, not schema: continuous 24/7 capture needs dynamic ticker
  management (KXBTC15M rolls a new market every 15 min vs a static `KBTC_KALSHI_TICKERS`); the in-session
  resubscribe-resync re-emitting a snapshot and the unsubscribe-`sids` envelope stay INFERRED (the full
  reconnect→snapshot→re-anchor path IS verified).
- [x] **1.2** Settlement capture: record every KXBTC15M market's final result and settlement value.
  *Done 2026-10-16:* `src/kbtc/recorders/settlement.py` + tests (78 pass). Review
  [REV-006](notes/reviews/REV-006-roadmap-1.2-settlement.md) (FAIL→PASS, Round 2). Kalshi REST
  exposes the numeric settlement value (`expiration_value` = close reference, `floor_strike` = open
  reference) — no BRTI reconstruction needed for ground truth; fields recorded in `docs/facts.md`.
  Completeness is enforced (missing-boundary / closed-unsettled / `result_inconsistent` all logged,
  never silent); idempotent/resumable. Empirically confirmed `result=="yes" iff close≥open` (0.1 ≥-mechanic).
  *Open:* `expiration_value` "= 60 s close-window BRTI avg" identity + window endpoint stay PARTIAL
  (Phase-2 empirical); no live poll loop / systemd unit yet (→ relates to 1.3).
- [ ] **1.3** VPS deployment (`deploy/`): systemd units, log rotation, disk alarms, daily
  upload/backup. *Accept:* 72 h unattended run with < 0.1 % gap time per feed.
  *Code built 2026-10-17 (run itself pending a VPS):* runner `python -m kbtc.recorders.run <feed>`
  (7 feeds, env-only auth, no order path); `deploy/` systemd units (recorder@, settlement/health/backup/
  disk-alarm timers, hardened non-root), generic rclone `backup.sh` (copy + verify, non-destructive),
  `disk_alarm.py`, journald logging, `deploy/README.md` runbook. Review
  [REV-009](notes/reviews/REV-009-deploy-layer.md) (PASS). **126 tests pass.**
  *Before the 72 h run (Berk, on the box):* pick `KBTC_BACKUP_REMOTE` + rclone config; wire an alert
  notifier (OnFailure stub); `uv add cryptography` (Kalshi signing); Kalshi resync stays a fail-safe
  no-op until 1.1b. The run exercises `run_recorder`'s live loop (currently `# pragma: no cover`) and
  is measured by the 1.4 health report's `gap_time_fraction < 0.1% AND msgs_per_s >= floor` gate.
  *Deployed 2026-10-17* on a Hetzner CPX31 (Ashburn, US-East). **Live & recording 24/7:** Bitstamp
  (live_orders), Kraken (level3), Kalshi settlement capture, + health/disk timers — all healthy (0 gaps).
  B2 backup WORKING (rclone `[b2]` → `b2:kbtc-data-by/raw`, nightly timer enabled, last run OK).
  Coinbase retail (1.1d) deployed + live (3rd constituent). Kalshi orderbook schema done (1.1b).
  *Dynamic Kalshi capture 2026-10-17:* `src/kbtc/recorders/kalshi_dynamic.py` — continuous 24/7 KXBTC15M
  capture on one stable connection: a connect-time `subscribe_factory` discovers open markets via the public
  REST, and a background poller (`KBTC_KALSHI_POLL_S`, default 45 s) sends `update_subscription`
  `add_markets`/`delete_markets` as markets roll (verified envelope + live wire: add on the same sid, no
  reconnect, ack type `ok`). Modes: dynamic (`KBTC_KALSHI_SERIES=KXBTC15M`), static (`KBTC_KALSHI_TICKERS`),
  legacy. Error-26/27 rejections now log loudly; stale sids cleared per-reconnect. Review
  [REV-018](notes/reviews/REV-018-dynamic-kalshi-tickers.md) (PASS; MEDIUMs fixed). **287 tests.**
  **Cross-roll validated + DEPLOYED 2026-10-18:** a live run spanning the 03:30Z roll captured 242,872
  records with **0 reconnects** (2 subscribe acks for the whole 10-min window) and the new market
  (`...2345-45`) auto-added **20 s after the boundary** (= the poll interval) via `update_subscription`
  (4 `ok` acks), getting its snapshot on the same connection — new market picked up with no disruption to
  the continuing one. `kbtc-recorder@kalshi` now enabled on Ashburn with `KBTC_KALSHI_SERIES=KXBTC15M`,
  recording 24/7 (485 msg/s, 0 gaps). **All four recorder groups now live** (Bitstamp + Kraken L3 +
  Coinbase retail L2 + Kalshi book/dynamic + Kalshi settlement).
  *Roll-gap fix 2026-10-18 (found + fixed via the live dashboard):* the first deploy left a ~49 s hole at
  each 15-min roll — the new market's OPEN-window (strike-forming 60 s) — because the poller only added it
  on the next tick. Fixed by PRE-SUBSCRIBING the upcoming market before it opens (`discover_upcoming_tickers`,
  `status=="initialized"`, hard-cap 2 lookahead; grace-delayed removal keeps a just-closed market's final
  frames). Live cross-roll re-measure: **max gap 49.1 s → 0.120 s**, new market subscribed 187 s before open.
  `KBTC_KALSHI_LOOKAHEAD`/`KBTC_KALSHI_REMOVE_GRACE_S` config; poll interval no longer bounds roll coverage.
  Review [REV-019](notes/reviews/REV-019-kalshi-roll-gap-presubscribe.md) (PASS). **294 tests.**
  *Also fixed via the dashboard:* both boxes' first unattended nightly B2 backup had failed (EU: rclone.conf
  root-only vs kbtc-run backup → `640 root:kbtc`; Ashburn: 2 stale partial files tripped `--immutable` →
  deleted + re-backed-up); both systemd backups now succeed, verified. `deploy/status.sh` (→
  `/usr/local/bin/kbtc-status`) is the live data-collection dashboard.
  *Incident 2026-10-18 (caught via the dashboard ~11h in):* `unattended-upgrades` restarted all recorders
  at 06:34; spot+perps recovered, but (1) Kraken's level3 token-subscribe silently failed → heartbeats-only,
  11h no data (recovered on restart; **self-heal + alerting pending**), and (2) Kalshi both missed the live
  market — discovery trusted the `status=open` server filter but the tradeable market is `status=active`
  (REV-019 residual, realized) — and leaked to 2.3 GB/11h. **Fixes:** live discovery is now client-side
  (`_is_live`: active/open live, time-window fallback; no `status=open` filter); memory bounded O(1)
  (`trades`/`lifecycle` → capped deques, per-market `books` evicted on removal/settle, `_seq_by_sid` cleared
  on reconnect). Review [REV-020](notes/reviews/REV-020-kalshi-incident-active-discovery-memleak.md) (PASS).
  **300 tests.**
  *Self-heal DONE 2026-10-18:* `run_recorder` now has a `DataStalenessMonitor` watchdog — if no DATA frame
  (heartbeats/acks don't count, via health's data classifier) arrives for `stale_timeout_s` (crypto 120s,
  Kalshi 300s), it forces a reconnect → re-token/re-discover/re-subscribe. This auto-recovers exactly the
  connected-but-silent stall that caused the 11h incident (minutes, not hours). Review
  [REV-021](notes/reviews/REV-021-data-staleness-watchdog.md) (PASS; can't false-trip at startup, worst case
  is a benign reconnect, no data loss). **310 tests.**
  *Alerting + upgrade-taming DONE 2026-10-18:* (#3b) ntfy failure alerting — `OnFailure` push on
  backup/disk/settlement + an out-of-process `kbtc-liveness` timer (5 min) that pages on a genuinely-stale
  feed, keyed on newest-DATA-frame age (NOT file mtime — heartbeats keep mtime fresh), thresholds above the
  watchdog so it only fires if self-heal failed; (#4) `unattended-upgrades`/needrestart set to list-only +
  auto-reboot off on both boxes so security patches install WITHOUT bouncing the recorders. Review
  [REV-022](notes/reviews/REV-022-alerting-and-upgrade-taming.md) (PASS; F1 curl-DNS socket-family + F2
  checker-self-page applied). **323 tests.** The 06:34 incident class is now defended three ways: won't
  restart → self-heals if it does → alerting backstop. (Not covered: total box death — an external
  dead-man's-switch is the complement, deferred.)
  *Monitoring perf fix 2026-10-20:* at 5.6 GB the health report scanned the whole tree (`data_time_bounds`
  decompressed every record to find max-ts) → minutes/run, and `kbtc-status` under `watch -n 30` stacked 6
  concurrent scans and pinned the box (looked "unhealthy"; feeds were fine). Fixed: health bounds now read
  ~1 file/feed (window-bounded, metrics byte-identical — the gap/CRC validator is unchanged), and
  `kbtc-status` uses a fast DATA-age table (`liveness --print-status`, one newest file/feed, never POSTs)
  instead of the full replay → completes in seconds, can't stack. Review
  [REV-023](notes/reviews/REV-023-health-dashboard-perf.md) (PASS, speed-only). **330 tests.**
  *EU perp box added 2026-10-17* — a Hetzner CX22 (EU, x86_64) because Binance (HTTP 451) and Bybit
  (HTTP 403) **geo-block the US Ashburn IP** (verified). **Live & recording 24/7:** `binance-perp`
  (depthUpdate + persisted REST snapshot → complete books) and `bybit-perp` (orderbook.50 snapshot+delta),
  both public/no-auth, + health/disk timers — health report all-green, **0 seq-gaps, 100% uptime**. This
  unblocks **2.3**. B2 backup wired + verified 2026-10-17 (same `b2:kbtc-data-by/raw`, disjoint venue dirs;
  upload path confirmed end-to-end; nightly timer enabled).
  The clean 72 h <0.1%-gap acceptance run is still pending (needs backups wired on both boxes).
- [x] **1.4** Health report script: per-feed uptime, message rates, gaps, disk usage.
  *Done 2026-10-16:* `src/kbtc/recorders/health.py` + CLI + tests (92 pass). Review
  [REV-007](notes/reviews/REV-007-roadmap-1.4-health.md) (PASS, Round 2). Per-feed disk/rate/coverage +
  sequence gaps (by replaying recorded raw through the real per-venue processors). Coverage/rate count
  **data frames only** (control/heartbeat frames excluded) so a stalled-but-heartbeating feed can't look
  healthy. **1.3's gate = `gap_time_fraction < 0.1% AND msgs_per_s >= floor`** (the `healthy` flag), not
  uptime alone; CLI exits non-zero on any unhealthy feed.
  *Backlog (before gating Binance on 1.3):* add a Binance `depthUpdate` jump fixture for `_prime_binance`;
  per-feed `min_rate` for sparse feeds.

## Phase 2 — Normalize + replicate the index (owner: data-engineer, index-replicator)

- [x] **2.1** Normalizer: raw → parquet books (L3 → L2 reconstruction), trades, Kalshi books.
  *Accept:* reconstructed L2 matches venue snapshots at checkpoints.
  *Done 2026-10-17:* `src/kbtc/normalize/` (reuses the recorder processors; event-driven top-N L2
  snapshots; `Decimal(38,8)` prices; books/kalshi_books/trades/settlements parquet). Review
  [REV-010](notes/reviews/REV-010-roadmap-2.1-normalizer.md) (FAIL→PASS, Round 2). Acceptance
  demonstrated for **Kraken** via the real CRC32 worked example (`1063832831`) reproduced through the
  emit path (non-circular). 153 tests pass.
  *Open:* Coinbase/Binance/Bitstamp books are flagged **`synced=False`** (the recorder doesn't persist
  the REST baseline — see 1.1c); their exact checkpointing is deferred to the live run. Bybit checkpoint
  informational; Kalshi schema + Binance aggTrade mapping UNVERIFIED (1.1b / feeds.md F-2).
  *Perf fix 2026-10-17 (surfaced by the first real-data run):* `OrderIdBook.levels()` rescanned every
  resting order + re-sorted the whole book on **every** event (O(N·M)) — a ~1 hr/hour pathology that hung
  the normalizer on real Bitstamp L3. Replaced with an incrementally-maintained per-side price→qty
  aggregate (SortedDict); top-N is now O(n), ~396× faster (≈9 s/hr vs ≈60 min/hr, synthetic). `self.orders`
  stays authoritative so Kraken CRC/time-priority is byte-identical. Review
  [REV-013](notes/reviews/REV-013-normalizer-levels-perf.md) (PASS). New dep `sortedcontainers`.
  *Extended to `PriceLevelBook` 2026-10-20 (REV-024):* the same `levels()` O(N·M) fix was missing on
  `PriceLevelBook` (Coinbase level2) — it `sorted()`-ed all ~40k levels per event, which stalled the first
  3-constituent 2.2 study's Coinbase normalize (100% CPU, ~0.3 MB/min). Fixed with the same SortedDict
  aggregate (~6887× on a 40k-level book); top-of-book byte-identical to before.
  [REV-024](notes/reviews/REV-024-pricelevelbook-levels-perf.md) (PASS). **361 tests.**
  *Real-data validation 2026-10-17:* a bounded 1 h slice normalized in 2.86 s / 174 MB on the box with
  **Kraken 4080/4080 CRC checkpoints MATCH** (venue-anchored, non-circular) → L3→L2 reconstruction correct
  on live data; valid books+settlements parquet produced.
  *Streaming + atomic writes 2026-10-17 (the full day then OOM'd — buffered whole run in RAM, 7.5 GB):*
  pipeline cores refactored into generators; `__main__` flushes to a `PartitionedParquetWriter` every
  50k rows (peak RSS now flat ~4.4 MB regardless of run length vs the old linear growth). Each partition
  is written ATOMICALLY (`part.parquet.tmp` → `os.replace` only on clean `close()`), so a crash/hard-kill
  never leaves a glob-visible partial that `index/loader.py` could silently ingest. Byte-for-row parity
  with the buffered path verified. Review [REV-014](notes/reviews/REV-014-normalizer-streaming-atomic.md)
  (PASS; MEDIUM crash-atomicity raised→closed). **246 tests pass.**
  *At-scale acceptance PASSED 2026-10-17:* the full real day normalized on the 7.6 GB box in **4:26, peak
  RSS ~201 MB (no OOM)**, producing **Kraken 284,171/284,171 CRC checkpoints MATCH** — L3→L2 reconstruction
  correct on real multi-hour data, venue-anchored and non-circular. 40 MB clean parquet (Bitstamp 28.7 MB +
  Kraken 12.7 MB books + settlements); zero leftover `.tmp` (atomic publish confirmed on a real run).
  **2.1 is validated end-to-end on live data.** (Bitstamp book completeness still awaits its REST-baseline
  persistence on the live feed — 1.1c/1.3; its CRC-less `deferred_rest_snapshot` path shows 0 checkpoints.)
- [x] **2.1a Replay/health fixes (surfaced by the live run 2026-10-17).** *Done 2026-10-17.*
  (a) **health: exclude the settlement batch dataset** (was reading 0% uptime). (b) **health: per-feed window
  from own first data** + a **cross-feed down-from-start gate** so a late window can't MASK a feed that was
  actually down (another feed covering the lead-in flags it; `LATE-START !` column). Review
  [REV: 2.1a(a,b)](notes/reviews/) via red-team FAIL→PASS; commit `bdf1c85`.
  (c) **Kraken reconstruction fix** (commit below): the ~8.5k CRC "gaps"/hr were THREE reconstruction bugs,
  not data loss — (1) float round-trip in parsing dropped the wire's exact digits; (2) in-level queue order
  must be **time priority** (oldest `ts` first), not insertion order; (3) a qty-increase `modify` adopts a
  fresh `ts` (moves to queue back). Now **0 CRC mismatches across 8,193 real updates**; red-team PASS
  (independently reproduced all real checksums + the published oracle). **Correction:** live Kraken was NOT
  clean — the broken snapshot CRC had it snapshot-polling (~1.5k snapshots/hr, same connection → "0 restarts"
  was a false-health signal). **Requires redeploy to the box** (same `KrakenProcessor`) for live recording to
  track incrementally. *LOW follow-ups:* (i) make the `ts` sort robust to timestamp-format variation (currently
  ISO-string compare); (ii) **emit periodic in-band Kraken snapshots** to bound the OFFLINE-replay gap on a
  genuine drop (more relevant now that snapshots are rare post-fix) — before the index/P&L phases consume it.

- [ ] **1.1d Coinbase Advanced Trade L2 adapter (retail, free).** The existing Coinbase adapter targets
  Coinbase **Exchange** `level3`/`full`, which (confirmed 2026-10-17) needs an **institutional Prime account
  (~$5,000 application)** — not viable. Build a new adapter for Coinbase's **retail Advanced Trade**
  WebSocket **`level2`** (price-level book) instead — sufficient for the BRTI price-volume curves; different
  endpoint/auth and schema, so it needs its own feed-spec + adapter + replay test.
  *Built 2026-10-17 (PROVISIONAL — not yet a counted constituent):* `src/kbtc/recorders/coinbase_advanced.py`
  feed `coinbase-advanced-level2`, endpoint `wss://advanced-trade-ws.coinbase.com`, **public/no-auth**,
  price-level `PriceLevelBook` (`side` bid/offer, `new_quantity` full-size, `"0"`=remove), subscribes
  `level2` + the required `heartbeats` keep-alive. Gap detection on the **per-connection** `sequence_num`
  over the full stream (heartbeats/acks advance it, don't false-gap); snapshot re-anchors; self-snapshotting
  so books normalize **complete** (`synced=True`), unlike Bitstamp. Writer path `coinbase_advanced/level2`
  (isolated from `coinbase/{full,level3}`). Spec VERIFIED from docs.cdp.coinbase.com (2026-10-17) →
  feeds.md §5A + facts.md. Review [REV-015](notes/reviews/REV-015-roadmap-1.1d-coinbase-advanced.md)
  (PASS-with-caveats; F2/F3 fixed). **251 tests pass.**
  **F1 CLOSED 2026-10-17 (live capture replayed clean):** deployed to Ashburn; the real capture confirms the
  spec on the wire — `sequence_num` +1 contiguous across l2_data→subscriptions→l2_data with the in-stream
  snapshot at seq 0 (40,872 levels / ~5 MiB), and the production normalizer replay gave 49,640 book rows,
  `synced` all True, an uncrossed book (best_bid 81124.80 / best_ask 81124.81), 0 seq-gaps. **Coinbase is now
  a verified 3rd BRTI constituent** → the index runs on 3 verified (Bitstamp + Kraken + Coinbase). Owner:
  data-engineer. (Snapshot-on-*reconnect* will be exercised by natural 24/7 disconnects; no per-message CRC
  oracle exists for Coinbase L2, so completeness is tracked via `synced` + the uncrossed-book/0-gap replay.)
  *Live-only bug found + fixed on first deploy 2026-10-17:* the level2 opening snapshot is one ~1.6 MiB /
  ~40k-level frame that exceeded the websockets default 1 MiB `max_size` → frame rejected → silent
  reconnect-loop, nothing recorded. Added `IoConfig.max_size` (default 2²⁰, byte-identical for all existing
  feeds) and set Coinbase Advanced to `None` (unbounded). Review
  [REV-016](notes/reviews/REV-016-ws-max-size.md) (PASS). A wire probe from the box incidentally **confirmed
  F1's premise** (seq +1 across l2_data + subscriptions, in-stream snapshot at seq 0) — so the pending live
  capture will close F1. **254 tests pass.**

- [x] **1.1c Persist REST snapshots into recordings (recorder completeness).** The recorder applies REST
  order-book snapshots (initial sync + resync) in memory but wrote only inbound WS frames, so
  Coinbase/Binance/Bitstamp recordings could not rebuild a COMPLETE book offline.
  *Done 2026-10-17:* the REST resyncer now persists each snapshot into the stream as a tagged envelope
  (`__kbtc_rest_snapshot__`); the normalizer consumes it → `synced=True` complete books; backward-compatible
  with old snapshot-less recordings. Review [REV-011](notes/reviews/REV-011-roadmap-1.1c-snapshot-persistence.md)
  (PASS). Replay test rebuilds a complete book (pre-existing resting orders present) from a recording that opens
  mid-stream; cross-wired/malformed snapshots are skipped+logged, not crashed. **161 tests pass.** Live
  per-venue REST JSON shape is exercised by the 1.3 VPS run (`run_recorder` persist wiring is `# pragma: no cover`).
- [ ] **2.2** BRTI replica from constituent books per `docs/brti-spec.md`.
  *Accept (RE-TARGETED — [ADR-002](docs/decisions/ADR-002-replica-role-and-metric.md), 2026-10-24):*
  the original "tracking p95 < $5" is RETIRED. The published BRTI is now observed directly (the
  `kalshi-cfbenchmarks` feed), so the replica is a lead-signal/fallback/backtest-reconstruction tool,
  not the fair-value level source. New acceptance: replica/feed-induced **fair-value error in cents per
  (τ, |z|) bucket** vs the Kalshi half-spread (a "safe-to-quote" region), with the real gate =
  **incremental Brier vs the Kalshi mid** (Phase 3, walk-forward, ≥3–5 independent days). $-tracking
  (median/p95/bias/slope b/ρ₁) kept as a diagnostic. See ADR-002 for the full rationale + EXP-003/005.
  *Build done 2026-10-17 (result still open):* `src/kbtc/index/` replicates the full CF v17.0 pipeline
  (consolidate → cap `C_T` = trimmed mean + 5σ → uncross → PV curves → utilized depth `D=0.5%` →
  depth-weighted mid with **λ = 1/(0.3·v_T)** → §5.3 deviation filter, 5%→2.5% hysteresis). All §6
  constants are `BrtiParams`; no look-ahead (O(log n) as-of book, compute failure → GAP never
  interpolated). Tracking harness (`tracking.py`) averages the replica over the 60×1 s window at both
  endpoints vs. Kalshi `floor_strike`/`expiration_value`, with an **out-of-sample `evaluate_oos`** that
  tunes the free levers (endpoint O-5, `cap_mean` O-4, `median_base` O-9) on TRAIN dates only, freezes,
  and reports p95 on a **disjoint, date-split TEST set** (hard overlap guard). Coverage (`venues_used`) is
  attached to every headline stat; published values are Decimal end-to-end. Review
  [REV-012](notes/reviews/REV-012-roadmap-2.2-brti-replica.md) (FAIL→PASS). **209 tests pass.**
  **First result 2026-10-20 — [EXP-001](notes/experiments/EXP-001-brti-tracking-2of3-firstpass.md),
  red-team VALID ([REV-025](notes/reviews/REV-025-exp001-tracking-2of3.md)):** a 2-of-3-constituent
  (Bitstamp+Kraken) walk-forward (TRAIN 10-19 n=22 / held-out TEST 10-20 n=30) gives **median $3.15 / p95
  $8.997 / max $10.34** at the frozen `T-60..T-1` endpoint — **MISSES p95<$5**, as expected for thin
  coverage. The miss is NOT a replica/averaging defect: 60×1 s averaging matches the derivation (0/191
  window violations), §5 filters behave as specified, Kraken reconstruction CRC-clean, replica in
  believable BTC range. Drivers: (1) only 2 of the ~3 spot constituents (real BRTI uses ~5–6); (2) a
  **Bitstamp self-crossed-book bug** — Bitstamp present+synced in ~159/191 windows but dropped by §5.2.1(c)
  in ~107 (persistent best_bid>best_ask) → replica is Kraken-only ~62% of windows. O-4/O-9 inert at 2
  venues; open≡close (chained markets). **Still not ticked** — the target isn't met and coverage is partial.
  **Follow-ups:** (a) fix the Bitstamp L3→L2 crossed book (Coinbase-severity data bug — IN PROGRESS); (b)
  deploy the PriceLevelBook perf fix (REV-024) → re-normalize Coinbase (fast) → re-run as a full
  3-constituent study; (c) accumulate more days (larger n, full test day).
  **Full 3-constituent result 2026-10-21 — [EXP-002](notes/experiments/EXP-002-brti-tracking-3of3.md),
  red-team VALID ([REV-027](notes/reviews/REV-027-exp002-tracking-3of3.md)):** Bitstamp crossed-book bug
  fixed (a40b452 / [REV-026](notes/reviews/REV-026-bitstamp-reseed-resurrection.md); crossing 62.9%→10.3%
  on re-normalized data, Bitstamp usable ~37%→~90%). Walk-forward TRAIN {10-18,10-19} n=112 / held-out
  TEST 10-20 n=80 (all 3 venues): **median $2.69 / p95 $7.02 / max $9.32 / bias −$1.29** at `T-60..T-1` —
  **still MISSES p95<$5**, improved on every metric vs EXP-001 (p95 $8.997→$7.02, n 30→80). Red-team
  confirmed the −$1.29 bias is NOT a defect (conservative-direction construction lag + missing venues;
  replica undershoots/compresses extremes, bias scales with vol — opposite of leakage). Caveats: config
  grid O-4/O-9 **empirically inert even at 3 venues** (§5.3 deviation filter 0/14,460 → re-tuning won't
  help); TEST is a single autocorrelated day (n=80 ≈ 1 independent sample — treat as a point estimate).
  **Still not ticked.** Diagnostic [EXP-003](notes/experiments/EXP-003-lag-bias-diagnostic.md) then
  REFUTED the forward-fill-lag hypothesis: books are already fresh (median age 0.02–0.10 s, 10 s gate
  almost never fires), staleness is anti-correlated with vol (−0.58), the direct lag test has the wrong
  sign (`corr(err,drift)=+0.30` → replica leads *into* the move, opposite of lag), and 10 s→1 s cutoff
  does nothing. Much of EXP-002's h=15/high-vol concentration does NOT reproduce on TRAIN (bias −$0.34,
  non-monotone) → largely a single-day (10-20) artifact. **Revised path to p95<$5:** (1) add constituents
  — Gemini first (IN PROGRESS); (2) investigate the sub-second averaging-window **endpoint alignment**
  (the +0.30 err-vs-drift slope; O-5/O-6 + the "1 Hz vs 200 ms BRTI feed" PARTIAL in facts.md) — NOT a
  staleness/latency fix (dead end); (3) accumulate ≥3–5 independent held-out days for a credible p95/CI.
- [ ] **2.3** Lead–lag study: perp flow vs. replica (Hayashi–Yoshida). Report horizons.

## Phase 3 — Fair value + signals (owner: quant-modeler)

- [ ] **3.1** Realized-vol estimator (microstructure-robust EWMA on 1–5 s returns, intraday
  seasonality). *Accept:* out-of-sample QLIKE vs. naive baseline.
- [ ] **3.2** Fair value = `pricing.digital_up_probability` with live σ and window state.
  *Accept:* calibration plot + Brier score vs. Kalshi mid on held-out days.
  **Key test:** does the model beat Kalshi's own mid out of sample? If not, stop and rethink.
- [ ] **3.3** Short-horizon drift from OFI/TFI (L3, with cancellation filtering) and perp lead.
  *Accept:* incremental Brier improvement on walk-forward splits.

## Phase 4 — Backtester (owner: backtest-engineer)

- [ ] **4.1** Event-driven simulator over recorded Kalshi books: back-of-queue fill model,
  queue depletion from observed trades/cancels, latency parameter, exact fees.
- [ ] **4.2** Quoting policy from `quoting.py` + pull rules (jump guard, near-strike endgame,
  thin depth). *Accept:* markouts at 5 s / 30 s / settlement, P&L net of fees, inventory
  stats, per-day breakdown; sensitivity to latency and queue assumptions.

## Phase 5 — Paper trading on Kalshi demo (owner: backtest-engineer)

- [ ] **5.1** Paper trader using the guard; same code path as backtest policy.
- [ ] **5.2** 2+ weeks live-data paper run; compare realized vs. backtest fills.
  *Accept:* report Berk can present. **Going live is Berk's decision, made outside this repo.**
