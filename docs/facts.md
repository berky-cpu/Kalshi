# Verified facts

Every fact the code depends on, with source and verification date.
Anything not in this table is UNVERIFIED.

## KXBTC15M contract terms (ROADMAP 0.1)

| Fact | Value | Source (URL) | Verified on | By |
|---|---|---|---|---|
| Settlement source | CF Benchmarks **Bitcoin Real-Time Index (BRTI)** — the CME CF Bitcoin Real-Time Index. (Not BRRNY, which is a once-daily reference rate.) Cadence/precision now pinned in 0.3: internal calc every 200 ms; **standard dissemination 1 Hz at top-of-second**, $0.01 precision, 24/7. | API series `settlement_sources` + market `rules_primary`: `https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M`; contract terms PDF `https://assets.kalshi.com/contract_terms/CRYPTO.pdf`; BRTI methodology v17.0 (see 0.3 table / `docs/brti-spec.md`) | 2026-10-16 | quant-modeler |
| Settlement averaging window | Simple arithmetic mean of **60 BRTI prints over the final 60 s** before the reference time. Final value rounded to nearest $0.01 of BTC price. **n=60 VERIFIED** ("60 RTI prices are collected"). **1 s spacing now VERIFIED** — 0.3 confirmed the BRTI standard feed is 1 Hz at top-of-second (prints aligned to integer UTC seconds). **The window-endpoint alignment (prints at close−59…close vs. the 60 s strictly before close) stays PARTIAL** — the 60 s averaging is a *Kalshi* construct, not defined in the CF RTI methodology, so this off-by-one-second placement (which shifts the load-bearing near-expiry variance correction) can only be closed by Kalshi's exact wording or by empirically regressing both candidate averages vs. published settlements. Also PARTIAL: whether Kalshi ingests the 1 Hz or 200 ms feed — a **secondary-source** web search (Berk, Claude chat, 2026-10-21) indicates **1 Hz**, consistent with "60 prices over 60 s", but it is **NOT explicitly stated in Kalshi's or CF's primary docs**, so this stays UNVERIFIED-primary; the definitive resolution is Kalshi's exact wording or empirical regression of the candidate averages (1 Hz vs 200 ms × endpoint placement) vs published settlements. Note EXP-003 found the ±1 s endpoint shift is only a ~$0.05 bias lever, so the sub-second err-vs-drift offset (+0.30) is unlikely to be a cadence/endpoint artifact. | Market `rules_primary` ("the simple average of the sixty seconds of CF Benchmarks' BRTI before [close]"); series `important_info` ("60 RTI prices are collected..."); contract terms PDF; BRTI cadence from methodology v17.0 §6.2/§7 (see 0.3); 1 Hz lean = secondary web search 2026-10-21 (unverified-primary) | 2026-10-16 (1 Hz secondary note 2026-10-21) | quant-modeler + index-replicator |
| Contract type / strike mechanism | **Up/down**: one market per 15-min window; the "strike" is the realized open-window 60 s BRTI average, fixed at open. Resolves **YES iff the 60 s BRTI average before CLOSE ≥ the open-window average** (ties → YES). **Ticker suffix = the close minute-of-hour (`-00`/`-15`/`-30`/`-45`), NOT always `-00`** (corrected in 1.2). On an **active/finalized** market the strike IS exposed: `floor_strike` (float, the open-window average, = `yes_sub_title` "Target Price") and **`strike_type = "greater_or_equal"`** — Kalshi's own confirmation of the ≥, ties→YES rule. (0.1 originally saw only *pre-open* markets showing `Target price: TBD`, hence the earlier "no strike fields" note — superseded.) | Market `rules_primary` ("...is at least the simple average of the sixty seconds of BRTI before [open], then... resolves to Yes"); `floor_strike`/`strike_type` on active/finalized market JSON (1.2 fixtures); contract terms PDF ("at least X" = "X or greater") | 2026-10-16 (strike fields/suffix corrected 1.2) | quant-modeler + data-engineer |
| Open / close / expiration | Each market trades exactly **15 minutes**, aligned to clock quarter-hours (:00/:15/:30/:45), **24/7**. `close_time` = top of the 15-min boundary = end of the settlement reference window. `frequency` = "fifteen_min". Times in UTC (human rules quoted in ET). NOTE: `expiration_time`/`latest_expiration_time` is a ~1-week-out administrative backstop — **do not** use it as the settlement instant; use `close_time`. | Market `open_time`/`close_time` (e.g. open 2026-10-18T02:45:00Z, close 03:00:00Z); series `frequency`; contract terms PDF | 2026-10-16 | quant-modeler |
| Tick size | Minimum tick **$0.001 (0.1¢)**, tapered deci-cent structure: 0.1¢ steps in [0.000, 0.100] and [0.900, 1.000], 1¢ steps in (0.100, 0.900). Sub-penny ticks exist in the wings. | Market `price_level_structure` = "tapered_deci_cent", `price_ranges`; contract terms PDF ("Minimum Tick size... shall be $0.001") | 2026-10-16 | quant-modeler |
| Position limits | **$25,000 per strike, per Member** Position Accountability Level (a reporting/accountability threshold, **not** confirmed as a hard contract cap). A hard per-market/per-series cap, if any, lives in Kalshi's general Rulebook / membership agreement — **UNVERIFIED** (PARTIAL). No `risk_limit`/position-limit field in market or series JSON. | Contract terms PDF `https://assets.kalshi.com/contract_terms/CRYPTO.pdf` | 2026-10-16 | quant-modeler |

## KXBTC15M fees (ROADMAP 0.2)

| Fact | Value | Source (URL) | Verified on | By |
|---|---|---|---|---|
| `fee_type` | **"quadratic"** | Kalshi series API `https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M` (`"fee_type": "quadratic"`) | 2026-10-16 | quant-modeler |
| `fee_multiplier` (M_taker) | **1** (dimensionless scalar on the base rate, NOT a bp value or the rate itself) | Kalshi series API, same endpoint (`"fee_multiplier": 1`) | 2026-10-16 | quant-modeler |
| Market-level fee fields | **None** — fees are series-level; no `fee_type`/maker field on the market object | Kalshi markets API `?series_ticker=KXBTC15M` (absence confirmed) | 2026-10-16 | quant-modeler |
| Taker formula & rate | `round_up(M · 0.07 · C · P · (1−P))` → KXBTC15M taker rate = 1 · 0.07 = **0.07**. **VERIFIED (primary) 2026-10-16** from Kalshi's own fee schedule (PDF + page, human fetch): formula verbatim `fees = round up(M x 0.07 x C x P x (1-P))`, `M` default 1; and Kalshi's General Trading Fees table matches our values (100 contracts → 0.07/0.63/1.32/1.75 at P=0.01/0.10/0.25/0.50). | Kalshi fee PDF (`kalshi-fee-schedule.pdf`) + `https://kalshi.com/fee-schedule` | 2026-10-16 | quant-modeler + Berk (fetch) |
| Maker formula & rate | `round_up(M · 0.0175 · C · P · (1−P))`, `M` default **0** unless the series is flagged. **VERIFIED (primary)** — formula verbatim in the fee PDF; maker series show $0.02–$0.44 / 100 contracts (= 0.0175 rate). | Kalshi fee PDF + page | 2026-10-16 | quant-modeler + Berk (fetch) |
| Rounding | Round **UP to the next whole cent**, per order. **VERIFIED-with-nuance (table-reconciled):** the PDF formula text literally says "rounds up … to a centicent" ($0.0001), but Kalshi's own General Trading Fees TABLE rounds the 1-contract fee to a whole cent ($0.01/$0.02…), so the effective granularity is a **cent** — a reconciliation from the table, not a verbatim quote. `fees.py` `ROUND_CEILING` to $0.01 matches the table and is strictly conservative (never under-states cost). | Kalshi fee PDF (General Trading Fees Table) | 2026-10-16 | quant-modeler + Berk (fetch) |
| KXBTC15M maker regime | **MAKER-FREE. VERIFIED (primary)** — on `kalshi.com/fee-schedule`, "Most markets" (multiplier 1) carry only taker fees; the Non-standard (maker-fee) series list is alphabetical and runs KXBALLONDOR → KXBTCMAX125 with **no KXBTC15M in between** (exactly where it would sort), so KXBTC15M is not a maker-fee series. | `https://kalshi.com/fee-schedule` (Standard + Non-standard fee tables, human fetch) | 2026-10-16 | quant-modeler + Berk (fetch) |
| Caps / minimums / crypto-specific fee | **None found** (UNVERIFIED-as-absent, not asserted) | — | 2026-10-16 | quant-modeler |

### Fee verification notes
- **Upgraded to primary-VERIFIED 2026-10-16** (Berk fetched Kalshi's own fee schedule, which 429'd for
  agents). The earlier single-source (Allium) corroboration is now confirmed by Kalshi's primary PDF +
  `kalshi.com/fee-schedule`: taker `round up(M·0.07·C·P·(1−P))`, maker `round up(M·0.0175·C·P·(1−P))`
  (M default 1 taker / 0 maker), fee table rounds to a whole cent, and KXBTC15M is NOT on the maker-fee
  series list. `fees.py` values were already correct; PARTIAL tags there can be dropped.
- Nuance: the PDF formula text says round "to a centicent", but Kalshi's own fee TABLE shows whole-cent
  1-contract fees ($0.01/$0.02), so the effective rounding is a cent — matching our implementation.
- `fee_multiplier` is M_taker (a scalar on the fixed 0.07 base); `fees.py` exposes `effective_taker_rate(m)=m·0.07`.

## BRTI methodology (ROADMAP 0.3)

Full implementable spec: `docs/brti-spec.md`. Methodology cited: **CME CF Cryptocurrency Real Time
Indices Methodology Guide v17.0 (21 Sep 2026)**, Constituent Exchanges List v13.7 (24 Aug 2026),
Criteria v8.5 (11 May 2026) — all from CF Benchmarks' own docs host (the Administrator/Calculation
Agent = primary source). Red-team independently re-fetched and confirmed the VERIFIED rows below.

| Fact | Value | Source (URL) | Verified on | By |
|---|---|---|---|---|
| Constituents (BRTI) | 7 active: **Bitstamp, Coinbase (incl. Pro/Prime), Kraken, Gemini, LMAX Digital, Bullish, Crypto.com**. **itBit suspended from BRTI 27-Jul-2026** (exclude on/after). Relevant pair BTC/USD; USDC fungible 1:1. | `https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges.pdf` (v13.7) | 2026-10-16 | index-replicator |
| Calc cadence (internal) | Every **200 ms**; retrieval-lag staleness cutoff 10 s | Methodology v17.0 §6.2 | 2026-10-16 | index-replicator |
| Dissemination cadence | **Standard 1 Hz top-of-second**; max 200 ms on request; precision **$0.01** | Methodology v17.0 §7 | 2026-10-16 | index-replicator |
| Aggregation | Order-book based, **no per-venue weight**: consolidate all venues → cap per level at `C_T` → uncross → bid/ask/mid price-volume curves on dynamic spacing `s_T` → utilized depth `v_T` (mid-spread ≤ **D=0.5%**) → exponentially depth-weighted mean of mid PV curve | Methodology v17.0 §4.1.1–4.1.2, Eq. 1a–3; §6.1/§6.2 | 2026-10-16 | index-replicator |
| Contingency filters | Drop stale venue (>10 s); drop erroneous book (unparseable / empty side / self-crossed); drop bad entries (non-numeric/non-positive); **potentially-erroneous median-deviation filter at 5%, reinstate <2.5% (stateful hysteresis)**; calc failure ⇒ **no print (gap), don't interpolate** | Methodology v17.0 §5.1–5.5 | 2026-10-16 | index-replicator |
| λ (exp weighting rate) | **λ = 1/(0.3·v_T) ≈ 3.33/v_T** — **VERIFIED 2026-10-16** (rendered PDF, human fetch: §6.1 Lambda cell is the fraction `1/(0.3 v_T)`; pypdf had flattened it to `"10.3 v_T"`, giving the earlier WRONG `10.3/v_T`). `λ·v_T = 1/0.3 ≈ 3.33` → weight decays to `e^(−3.33) ≈ 3.6%` at utilized depth (much flatter than the old `e^(−10.3)` assumption). | Methodology v17.0 §6.1 (`brti-spec.md` O-2, RESOLVED) | 2026-10-16 | index-replicator + Berk (fetch) |
| Order-size cap mean | `C_T = s̄ + 5σ`, literal **s̄ = trimmed mean (Eq. 4e)**, σ = winsorized std (Eq. 4h) — **PARTIAL** (winsorized `s̄′` is an alternative reading; close empirically). | Methodology v17.0 §4.1.3, Eq. 5 (`brti-spec.md` O-4) | 2026-10-16 | index-replicator |
| Settlement window endpoint | **Not defined by the CF RTI methodology** — the 60 s average is a Kalshi construct. `[T−59,T]` vs `[T−60,T−1]` stays **PARTIAL** (resolve empirically; see 0.1). | Methodology v17.0 (absence) | 2026-10-16 | index-replicator |

### BRTI verification notes
- **CME mirror (`cmegroup.com/trading/files/...`) returned HTTP 403** (scraping block) on 2026-10-16 — same
  pattern as the Kalshi fee PDF — so the CME copy could NOT be cross-checked; all facts come from the CF
  Benchmarks docs host (primary). Red-team independently re-fetched the CF PDFs and confirmed the VERIFIED rows.
- Open items carried to Phase 2 (full list in `docs/brti-spec.md` §7): **O-2 λ is now RESOLVED**
  (= `1/(0.3·v_T)`, corrected from the wrong `10.3/v_T`, 2026-10-16 rendered PDF). **O-4 cap mean**
  (trimmed vs winsorized) still feeds every print → resolve empirically vs. published BRTI. **O-7/O-8**:
  which of the 7 constituents we actually record (Gemini/Bullish/Crypto.com/LMAX may be missing) and
  USDC-vs-native-USD market mapping — UNVERIFIED, audit `data/clean` in the tracking-error study.

## Venue feeds (ROADMAP 0.4)

Full recorder contract: `docs/feeds.md`. Load-bearing summary (gap-detection + resync drive ROADMAP
1.1's "gaps logged, not silent"). Red-team independently re-verified the Coinbase `sequence`, Kraken
CRC32 checksum (incl. worked example `1063832831`), and Binance `pu` rules against primary docs.

| Venue | BRTI? | Endpoint (prod) | Auth | Gap-detection field | Status |
|---|---|---|---|---|---|
| Coinbase Exchange `full`/`level3` | constituent | `wss://ws-direct.exchange.coinbase.com` | API key + HMAC-SHA256 (reqd since 2023-07-01) | **`sequence`** (jump > 1) | VERIFIED (`level3` resync + max-conns PARTIAL) |
| Coinbase **Advanced Trade** (retail) `level2` | constituent | `wss://advanced-trade-ws.coinbase.com` | **public (no JWT)** | **`sequence_num`** per-CONNECTION (jump > 1; counts heartbeats/acks too) | VERIFIED 2026-10-17 (docs.cdp.coinbase.com; in-band `level2` snapshot resync; limits PARTIAL) |
| Kraken v2 `level3` | constituent | `wss://ws-l3.kraken.com/v2` (⚠ 3-way host conflict) | REST `GetWebSocketsToken` → `token` | **`checksum`** (CRC32 top-10/side; NO seq number) | VERIFIED (host + resync PARTIAL) |
| Bitstamp `live_orders_{pair}` | constituent | `wss://ws.bitstamp.net` ✓ | public | **`event_id`/`pre_event_id` chain** (`pre_event_id` == prev `event_id`, like Binance `pu`); `since_id` REST backfill | VERIFIED 2026-10-16 (primary WS doc, human fetch) |
| Binance USDⓈ-M `@depth`+`@aggTrade` | lead signal | `wss://fstream.binance.com` | public | **`pu`** (`pu == prev.u`; ≠ spot's `U==prev.u+1`) | VERIFIED resync/`pu` (speeds/trades/limits PARTIAL) |
| Bybit v5 linear `orderbook`+`publicTrade` | lead signal | `wss://stream.bybit.com/v5/public/linear` | public | **`type=="snapshot"`** reset (no numeric `u`-gap rule) | VERIFIED incl. trades |
| Gemini v2 `marketdata` `l2` (BTCUSD) | constituent | `wss://api.gemini.com/v2/marketdata` | **public (no auth)** | **NONE — no sequence number exists**; best-effort **crossed-book** (`bid>ask`) detector + reconnect-reseed | VERIFIED 2026-10-21 (doc, *Archived* but endpoint LIVE) + 2026-10-20 (live wire); crossed-book detector PARTIAL (see note) |
| Crypto.com Exchange v1 `book` (BTC_USD spot) | constituent | `wss://stream.crypto.com/exchange/v1/market` | **public (no auth)** | **`pu`** (`pu == prev.u` delta chain; SNAPSHOT anchors `u`) + in-band resubscribe resync | VERIFIED 2026-10-21 (primary doc) + live wire BOTH boxes; `cs` checksum present-but-unused (F-12), limits PARTIAL |
| Kalshi `orderbook_delta`/`trade`/lifecycle | **trading venue** | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (legacy `api.elections.kalshi.com`; demo `external-api-ws.demo.kalshi.co`) — all VERIFIED | API key + RSA-PSS/Ed25519 sig over `ts+"GET"+"/trade-api/ws/v2"`, headers `KALSHI-ACCESS-KEY/SIGNATURE/TIMESTAMP` (ms) — READS ONLY, VERIFIED | **orderbook SCHEMA now VERIFIED** (2026-10-17): envelope `type`/`sid`/`seq`; snapshot `yes_dollars_fp`/`no_dollars_fp` (optional `[price_dollars,count_fp]` string pairs); delta `price_dollars`+SIGNED `delta_fp`+`side` yes/no. **LIVE-WIRE CONFIRMED 2026-10-17** (authed capture, KXBTC15M-26OCT172000-00): payload IS NESTED under `msg` (the doc example was flattened); `sid` per-subscription, `seq` per-sid and **RESETS to 1 on each reconnect** (handled — each `orderbook_snapshot` re-anchors `_seq_by_sid`). Real snapshot = 40k-ish levels, dollar-string prices ("0.0010") + fixed-point sizes ("1226720.00"); delta `delta_fp="-100.00"` signed. **unsubscribe-via-`sids` still INFERRED (code marks it UNVERIFIED).** **`trade` SCHEMA now VERIFIED (live wire 2026-10-17)**: nested under `msg` = `trade_id`(UUID), `market_ticker`, `yes_price_dollars`/`no_price_dollars` (DOLLAR strings, complements), `count_fp` (fixed-point size), `taker_side`/`taker_outcome_side` (yes/no), `taker_book_side` (bid/ask), `is_block_trade`, `ts`(s)/`ts_ms`(ms) — NOT the old `yes_price` cents-int. Normalizer emits price=`yes_price_dollars`, size=`count_fp`, side=`taker_side`, exch_ts from `ts_ms`. | **orderbook + trade schemas VERIFIED (doc + live wire 2026-10-17)**; `msg`-wrapper CONFIRMED nested, seq per-sid + resets-to-1-on-reconnect CONFIRMED (handled). **CORRECTION:** the earlier "no-keepalive, drops ~every 4 s" note was WRONG — the ~4 s reconnect was the OLD adapter raising `KeyError: 'price'` on every delta (run_recorder reconnects on any `process()` exception); with the verified-schema adapter the connection is STABLE (10,113 frames / 22 s, 0 crashes, single connection; raw WS also stable with/without ping per a 40 s / 27k-frame probe) — no keepalive needed. unsubscribe-`sids` stays INFERRED. **1.3 deploy follow-up (NOT a 1.1b schema item):** continuous 24/7 capture needs DYNAMIC ticker management — KXBTC15M rolls a new market every 15 min, so static `KBTC_KALSHI_TICKERS` only covers one window. |

### Feed verification notes
- We **read market data only** — Coinbase HMAC / Kraken token are used solely to subscribe; no order path in a recorder.
- **Coinbase Advanced Trade (retail) VERIFIED 2026-10-17** (docs.cdp.coinbase.com): endpoint
  `wss://advanced-trade-ws.coinbase.com`, **public — "A JWT is not required"** for market data (no auth wired);
  subscribe one channel per message (`level2` + REQUIRED `heartbeats` keep-alive, within 5 s of connect; idle cut
  60–90 s); data arrives on `channel:"l2_data"` (you subscribe to `"level2"`); `updates[]` carry `side`
  (`bid`/`offer`), `price_level`, `new_quantity` (FULL size; `"0"`=remove); **`sequence_num` is per-CONNECTION,
  +1 on every message incl. heartbeats/acks** → gap = jump > 1 (checked over the whole stream, not filtered to
  `l2_data`); first `l2_data` is a `snapshot` (COMPLETE baseline → `synced=True`); resync is in-band (resubscribe
  `level2` → fresh snapshot). L2 price-level book (`PriceLevelBook`), not order-id. Writer path
  `coinbase_advanced/level2`, feed `coinbase-advanced-level2`, `required_env=()`. **PARTIAL:** exact
  max-connections / message-rate limits not recorded (not load-bearing for one BTC-USD feed).
  **LIVE-WIRE CONFIRMED 2026-10-17** (Ashburn box): a direct wire probe showed `sequence_num` +1 contiguous
  across `l2_data` (0–7) → `subscriptions` (8–9) → `l2_data` (10–15) with the in-stream `snapshot` at seq 0
  (40,872 levels, a ~5 MiB frame — needs `max_size=None`, [REV-016]). The real capture replayed clean through
  the production normalizer: 49,640 book rows, `synced` all True, reconstructed book uncrossed
  (best_bid 81124.80 / best_ask 81124.81), 0 seq-gaps in the health replay. The per-connection-`sequence_num`
  and in-stream-snapshot claims are now VERIFIED on the wire (1.1d F1 closed); snapshot-on-*reconnect* will be
  exercised by natural 24/7 disconnects (health report would flag any resulting gap).
- **Gemini v2 `marketdata` `l2` VERIFIED 2026-10-21** (primary doc) **+ live wire 2026-10-20** (BRTI
  constituent; new in the 1.1-series). **Source doc:** `https://developer.gemini.com/websocket/archived/v2`
  (the v2 `marketdata` page; `docs.gemini.com/websocket-api/#market-data-version-2` 301-redirects here; the
  `.md` variant served clean markdown to `curl` — `docs.gemini.com` itself resisted WebFetch). **Gemini tags
  this page *Archived* ("replaced by the new WebSocket API") — the endpoint is STILL LIVE (confirmed on the
  wire 2026-10-20) but this is a standing deprecation RISK (see F-10).** Endpoint
  `wss://api.gemini.com/v2/marketdata`, **public, no auth**. Subscribe ONE frame
  `{"type":"subscribe","subscriptions":[{"name":"l2","symbols":["BTCUSD"]}]}`. Book messages are all
  `type:"l2_updates"` with `changes:[[side,price,qty],...]` (`side` `buy`→bid/`sell`→ask; `qty` is the FULL
  size at the level, absolute SET; `"0"`/`"0.0"`=remove). **GAP DETECTION — the critical finding: the v2 `l2`
  feed carries NO sequence number and NO ordering/gap field of any kind** (live probe found no
  `seq`/`socket_sequence`/`event_id` on any l2 message; `any_seq_field` empty over 40 frames). So a dropped
  message is NOT detectable by sequence. **Decision:** detect divergence by the only in-band signal available —
  a STRICTLY crossed book (`best_bid > best_ask`, which on a venue that never rests a bid above an ask means a
  missed level-removal) — and rely on **reconnect = reseed** (the feed sends a fresh full snapshot on every
  (re)subscribe). This is **best-effort / PARTIAL**: a missed update that does NOT cross the book is undetectable
  in-band and is only corrected at the next reseed (reconnect / staleness-watchdog force-reconnect / crossed-book
  resync). We did NOT invent a sequence field. **SNAPSHOT vs DELTA (no `type:"snapshot"` marker in v2):** the
  snapshot is the ONLY `l2_updates` that also carries a **`trades`** array (last-50 trades) and
  **`auction_events`** — VERIFIED on the wire (initial frame keys `[auction_events,changes,symbol,trades,type]`,
  full both-sides book ~4,563 levels, ~200 KiB; deltas keys `[changes,symbol,type]` ONLY, never `trades`, over
  many samples). This `trades`/`auction_events` signature is Gemini's analogue of Bybit's `type:"snapshot"` reset
  and cleanly closes the in-band-resync stale-delta race (a stale delta can't be mistaken for the rebuild
  snapshot). **Trades** arrive as separate `type:"trade"` frames (fields `symbol`/`price`/`quantity`/`side`
  (taker buy/sell)/`tid`/`event_id`/`timestamp` ms — VERIFIED). **Heartbeats**: server pushes `type:"heartbeat"`
  frames (seen on the wire); with the websockets library's own protocol ping, no client app-ping is needed.
  L2 price-level book (`PriceLevelBook`), `max_size=None` (full-book snapshot frame). Writer path
  `gemini/l2_btcusd`, feed `gemini-l2`, `required_env=()`. **PARTIAL/UNVERIFIED:** (a) the archived/deprecated
  status (F-10); (b) connection / message-rate limits not recorded (not load-bearing for one BTCUSD feed);
  (c) whether the crossed-book heuristic ever false-positives on a transient (believed no — the venue never
  rests a bid above an ask; a one-shot guard prevents any gap storm regardless). Probe was LOCAL read-only
  (connect→subscribe→~40 frames→disconnect), NOT run on the box.
- **Crypto.com Exchange v1 `book` VERIFIED 2026-10-21** (primary doc) **+ live wire 2026-10-21, both
  boxes** (BRTI constituent; new in the 1.1-series). **Source doc:** the v1 `book.{instrument_name}.{depth}`
  channel page `https://exchange-developer.crypto.com/exchange/v1/docs/api/websocket/ws-channel-book-instrument-name-depth`
  (the old `https://exchange-docs.crypto.com/exchange/v1/rest-ws/index.html` 301-redirects here; the
  site is a Docusaurus SPA that resisted WebFetch/curl on the root, but the per-channel pages served
  clean SSG HTML to `curl` — read via tag-strip). Level format `[Price, Quantity, NumOrders]` from the
  REST `public-get-book` page; the spot instrument `BTC_USD` confirmed from the public `get-instruments`
  REST read (`inst_type` `CCY_PAIR`; `BTCUSD-PERP` is the perpetual — NOT what we record). Endpoint
  `wss://stream.crypto.com/exchange/v1/market`, **public, no auth**. Subscribe ONE frame
  `{"id":1,"method":"subscribe","params":{"channels":["book.BTC_USD.50"],"book_subscription_type":"SNAPSHOT_AND_UPDATE","book_update_frequency":100},"nonce":<ms>}`
  (depth 50 = venue max; explicit depth now REQUIRED; `SNAPSHOT_AND_UPDATE` = 100 ms delta mode, the
  full-snapshot-only feed was retired 2025-02). Book data rides in `result` (`result.channel` starts
  `book`); `result.data[0]` is a SNAPSHOT (top-level `asks`/`bids` full book, `u`, `t`/`tt`, `cs`; NO
  `pu`) or a DELTA (`update` object with changed `asks`/`bids`, `u`, `pu`). `quantity` is the FULL
  (absolute SET) size; `"0"` removes the level. **GAP DETECTION — the real documented mechanism (NOT
  invented):** deltas apply in strict order where **`pu` of the new message == `u` of the previous
  message** (Binance-futures-style chain); the snapshot anchors `last_u`; a `pu` mismatch is a dropped
  message → log the gap + in-band resync (resubscribe → fresh snapshot). **VERIFIED on the live wire:
  37/37 (Ashburn-US) and 38/38 (EU-DE) consecutive `pu == prev.u` checks held, full snapshot then
  deltas, subscribe ack `code:0`.** **HEARTBEAT:** the server sends `public/heartbeat` ~every 30 s and
  the client MUST reply `public/respond-heartbeat` (echoing the id) within 5 s or it disconnects
  (doc-VERIFIED; a heartbeat was seen on the wire and answered). Implemented via a new general,
  backward-compatible seam: the pure processor queues the reply on `self.outbound`, `run_recorder`
  drains + sends it over the live socket right after `process` (empty for every other venue). **GEO
  (VERIFIED 2026-10-21):** market-data WS reachable from BOTH boxes (Ashburn US + EU DE) — NOT
  geo-restricted; **deploys to the Ashburn (US) box** with the other constituents. L2 price-level book
  (`PriceLevelBook`). Writer path `cryptocom/book_btcusd`, feed `cryptocom-book`, `required_env=()`.
  **PARTIAL/UNVERIFIED:** (a) the `cs` book-checksum algorithm is not on the current doc page — present
  on the wire but UNUSED and not invented (F-12); (b) connection / message-rate limits not recorded;
  (c) 30 s heartbeat cadence is doc-stated, observed once in the short probe. Probe was read-only
  (connect→subscribe→~40 frames→respond-heartbeat→disconnect), run on both VPS boxes, nothing left
  running.
- **Bitstamp VERIFIED 2026-10-16** (Berk fetched the primary WS doc): endpoint `wss://ws.bitstamp.net`,
  subscribe envelope `{"event":"bts:subscribe","data":{"channel":"live_orders_<market>"}}`, ≤1024 channels/conn.
  **Gap mechanism corrected:** `live_orders` events carry top-level **`event_id`/`pre_event_id`** ("allow you to
  detect and recover" on reconnect) — a chain (`pre_event_id` == prev `event_id`), like Binance `pu`. NOT a
  microtimestamp/+1 counter. The Bitstamp adapter is being updated to use this (the R2 microtimestamp-regression
  default was a stopgap). (`microtimestamp` ordering + `since_id` REST backfill still apply to the separate
  `detail/diff_order_book` channel.)
- **Bitstamp order-id reuse — VERIFIED (empirical) / UNVERIFIED (doc), 2026-10-20** (REV-026 F1,
  data-engineer). **Correctness-critical assumption for the reseed-resurrection guard** in
  `src/kbtc/recorders/bitstamp.py`: the guard keys purely on `data.id`, so if an id were reused within the
  120 s guard window a stale `_recent_deletes[id]` could silently DROP a genuinely-resting snapshot order
  (book under-counts, no crossing, no alarm — an invisible bias). **Empirical result: CLEAN.** Scanned every
  `order_created` id in the recorded raw for 2026-10-18/19/20 (`/opt/kbtc/data/raw/bitstamp/live_orders_btcusd/`):
  **21,129,299 ids, ZERO duplicates** — 0 within any single day (6.15M / 7.27M / 7.71M creates resp.) and 0
  across the full 3-day span (distinct_ids == creates_scanned). So no id reuse within any realistic guard
  window (verified over 3 days ≫ the 120 s window). The Bitstamp observed ids are large and monotonically
  increasing per connection, consistent with non-reuse. **Doc side UNVERIFIED:** the primary WS v2 page is
  JS-rendered and did not yield text to `WebFetch`; no explicit "ids are unique / never reused" statement was
  located in a primary doc. Risk if this ever changes: see the CORRECTNESS-CRITICAL note in `bitstamp.py`.
  Source: empirical scan of recorded raw (read-only, box root@5.161.236.203), 2026-10-20.
- **Binance** diff-depth stream speeds + aggTrade field list stay **PARTIAL** — Berk reports the USDⓈ-M stream
  doc pages no longer exist; the resync/`pu` algorithm is VERIFIED (How-to-manage page). Revisit if Binance republishes.
- **Kraken** host is a 3-way conflict (`ws-l3` / `ws-auth` / `ws`) → recorder tries all three with fallback.
- **Kalshi WS** (1.1b): VERIFIED from the quick-start WS PDF (human fetch) — prod/legacy/demo endpoints, auth
  headers + signing string `ts+"GET"+"/trade-api/ws/v2"`, subscribe `{id,cmd,params:{channels,market_tickers}}`,
  unsubscribe via `sids`, message types (`orderbook_snapshot`/`orderbook_delta`/`ticker`/`trade`/`market_lifecycle_v2`/
  `error`), reconnect-with-backoff, auto keep-alive. Ticker uses `_dollars` string fields.
- **Kalshi ORDERBOOK SCHEMA VERIFIED 2026-10-17** (`https://docs.kalshi.com/websockets/orderbook-updates`, read
  2026-10-17): envelope top-level `type` (`orderbook_snapshot`|`orderbook_delta`), `sid` (int, required), `seq`
  (int, required — "Sequential number that should be checked"), optional `id`, `sending_ts_ms`.
  **orderbook_snapshot:** `market_ticker` (str), `market_id` (str UUID), **`yes_dollars_fp`** / **`no_dollars_fp`**
  (both OPTIONAL — absent if no offers that side), each a list of `[price_in_dollars, count_fp]` STRING pairs, e.g.
  `["0.0800","300.00"]` (price = dollar string; size = fixed-point count string). **orderbook_delta:**
  `market_ticker`, `market_id`, **`price_dollars`** (str), **`delta_fp`** (str, SIGNED fixed-point contract delta,
  2 dp, e.g. `"-54.00"`), **`side`** (`yes`|`no`), optional `client_order_id`/`ts`(deprecated)/`ts_ms`/`subaccount`.
  This corrected the adapter, which had assumed `price`/`delta`/`yes`/`no` (margin-ws names) and would NOT have
  parsed real frames. Adapter parses prices/sizes as exact Decimal (dollars / fixed-point), never float.
- **Kalshi TRADE SCHEMA VERIFIED 2026-10-17** (live authed capture, box, KXBTC15M-26OCT172230-30): `trade` msg
  nested under `msg` = `trade_id` (UUID str), `market_ticker`, **`yes_price_dollars`** / **`no_price_dollars`**
  (DOLLAR strings, complements), **`count_fp`** (fixed-point size string), **`taker_side`** (yes/no),
  `taker_outcome_side` (yes/no), `taker_book_side` (bid/ask), `is_block_trade` (bool), `ts` (s), `ts_ms` (ms) —
  NOT the old `yes_price` cents-int / `count`. `normalize.extract._kalshi_trades` emits price=`yes_price_dollars`
  (YES price in dollars; `no_price_dollars` is its 1−p complement, not stored separately), size=`count_fp`,
  side=`taker_side`, exch_ts_ns from `ts_ms`. Decimal, never float.
- **Kalshi live-wire resolution 2026-10-17** (authed capture + direct probe): the `msg`-WRAPPER is CONFIRMED
  **nested** (`{type,sid,seq,msg:{...}}`); the official doc example was flattened. `sid` is per-subscription; `seq`
  is per-sid and **RESETS to 1 on each reconnect** (handled — every `orderbook_snapshot` re-anchors `_seq_by_sid`).
  The adapter still parses a FLAT payload robustly as defence-in-depth. **CORRECTION of an earlier misdiagnosis:**
  the ~4 s reconnect loop was NOT a missing keepalive — it was the OLD adapter raising `KeyError: 'price'` on every
  delta (run_recorder reconnects on any `process()` exception). With the verified-schema adapter the connection is
  STABLE: 10,113 frames / 22 s on ONE connection, 0 processor crashes (subscribed:3, delta:9326, trade:783); the
  raw WS is stable with and without a client ping (40 s / 27k-frame probe) → no keepalive needed. **Still
  INFERRED:** the unsubscribe-via-`sids` envelope (no live confirmation). **1.3 DEPLOY follow-up (NOT a 1.1b schema
  item):** continuous 24/7 capture needs DYNAMIC ticker management — KXBTC15M rolls a new market every 15 min, so a
  static `KBTC_KALSHI_TICKERS` covers only one window. 1.1b schema work is COMPLETE.

## KXBTC15M settlement fields (ROADMAP 1.2)

Captured from the Kalshi production REST API (reads only), 2026-10-16. All on the market object;
no dedicated settlement subresource (`/markets/<t>/settlement` → 404). Code: `src/kbtc/recorders/settlement.py`.

| Field | Value / meaning | Source | Verified on | By |
|---|---|---|---|---|
| `result` | official outcome `"yes"`/`"no"` (blank pre-settle) | `GET /trade-api/v2/markets?series_ticker=KXBTC15M&status=settled` + `/markets/<ticker>` | 2026-10-16 | data-engineer |
| `expiration_value` | string BTC price (e.g. `"81423.86"`) = **Kalshi's published numeric settlement quantity = the close reference**. **VERIFIED as the authoritative settlement value.** Its identity as specifically the *60 s BRTI close-window average* is **INFERRED/PARTIAL** (window-endpoint still open from 0.1/0.3) — but the field value itself is ground truth regardless of our name for it. | market object | 2026-10-16 | data-engineer |
| `floor_strike` | float BTC price = the **open-window reference / strike** (matches `yes_sub_title` "Target Price") | market object (active/finalized) | 2026-10-16 | data-engineer |
| `strike_type` | `"greater_or_equal"` — confirms the ≥, ties→YES rule from 0.1 | market object (active/finalized) | 2026-10-16 | data-engineer |
| `settlement_value_dollars` | per-contract YES payout `"1.0000"`/`"0.0000"` (NOT a BTC price) | market object | 2026-10-16 | data-engineer |
| status / enumeration | query filter `status=settled` → object `status="finalized"` (`status=finalized` as a filter is rejected; `closed`→0). Enumerate via `cursor`; `min_close_ts`/`max_close_ts` are **unix seconds, inclusive**, filtering on `close_time`. | markets list endpoint | 2026-10-16 | data-engineer |
| settle timing | `close_time` = economic close = end of settlement reference window (USE THIS); `settlement_ts` ~1–6 s later (`settlement_timer_seconds`=1); `expiration_time` is a ~1-week admin backstop — do NOT use (confirms 0.1) | market object | 2026-10-16 | data-engineer |

**Empirically confirmed relationships (every 1.2 fixture):** `result == "yes"` iff `expiration_value ≥ floor_strike`
(incl. the yes case 81504.11 ≥ 81501.40); and window N's `expiration_value` chains to window N+1's `floor_strike`.
This independently corroborates the 0.1 up/down ≥-mechanic. `settlement.py` enforces this invariant **at capture**
(a `result_inconsistent` issue is logged, not silently written, on any mismatch).

### Verification notes
- Ticker-specific values (open/close examples) are from the 2026-10-18 session markets live at fetch
  time (2026-10-16); the structural facts (window, source, tick, 15-min frequency, up/down mechanic)
  are series-level and stable.
- The `CRYPTO.pdf` is a parameterized template covering several crypto products. Its template default
  "Expiration Time... 10:00 AM ET" and "latest Expiration Date one week after" describe the daily/other
  crypto products, **not** the 15-min cadence; the 15-min timing comes from the API market objects.
- `WebFetch` could not decode the compressed PDF; it was read via `curl` + `pypdf`.
