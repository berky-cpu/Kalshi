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
- [ ] **0.2 Fees.** Pull `GET /series/KXBTC15M` (`fee_type`, `fee_multiplier`) and the latest
  fee schedule PDF. *Accept:* `fees.py` constants confirmed or corrected; test added.
- [ ] **0.3 BRTI methodology.** From the CF Benchmarks methodology guide + constituent list:
  constituents, publication cadence (1 s vs 200 ms), aggregation steps (order-book
  mid/“price-volume curve”, spacing, deviation filters, weighting).
  *Accept:* written spec in `docs/brti-spec.md` precise enough to implement.
- [ ] **0.4 Feed specs.** For Coinbase `level3`/`full`, Kraken v2 `level3`, Bitstamp
  `live_orders`, Binance/Bybit perp: endpoint, auth, subscription message, sequence
  numbers, snapshot/resync procedure, rate limits. *Accept:* `docs/feeds.md`.

## Phase 1 — Record everything (owner: data-engineer)

- [ ] **1.1** Venue adapters on top of `recorders/writer.py`: Kalshi (`orderbook_delta`,
  `trade`, market lifecycle), Coinbase L3, Kraken L3, Bitstamp live orders, Binance + Bybit
  perp L2/trades. Gap detection via sequence numbers, auto-reconnect + resnapshot.
  *Accept:* each adapter has a replay test on a recorded fixture; gaps are logged, not silent.
- [ ] **1.2** Settlement capture: record every KXBTC15M market's final result and settlement value.
- [ ] **1.3** VPS deployment (`deploy/`): systemd units, log rotation, disk alarms, daily
  upload/backup. *Accept:* 72 h unattended run with < 0.1 % gap time per feed.
- [ ] **1.4** Health report script: per-feed uptime, message rates, gaps, disk usage.

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
