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
- [ ] **1.1b Kalshi WS adapter — finalize.** Quick-start WS doc (human fetch, 2026-10-16) VERIFIED the
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

- [ ] **2.1** Normalizer: raw → parquet books (L3 → L2 reconstruction), trades, Kalshi books.
  *Accept:* reconstructed L2 matches venue snapshots at checkpoints.
- [ ] **2.2** BRTI replica from constituent books per `docs/brti-spec.md`.
  *Accept:* tracking error vs. published settlement values reported (median, p95, max);
  target p95 < $5 on settlement averages.
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
