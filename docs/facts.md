# Verified facts

Every fact the code depends on, with source and verification date.
Anything not in this table is UNVERIFIED.

## KXBTC15M contract terms (ROADMAP 0.1)

| Fact | Value | Source (URL) | Verified on | By |
|---|---|---|---|---|
| Settlement source | CF Benchmarks **Bitcoin Real-Time Index (BRTI)** — the CME CF Bitcoin Real-Time Index. (Not BRRNY, which is a once-daily reference rate.) Cadence/precision now pinned in 0.3: internal calc every 200 ms; **standard dissemination 1 Hz at top-of-second**, $0.01 precision, 24/7. | API series `settlement_sources` + market `rules_primary`: `https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M`; contract terms PDF `https://assets.kalshi.com/contract_terms/CRYPTO.pdf`; BRTI methodology v17.0 (see 0.3 table / `docs/brti-spec.md`) | 2026-10-16 | quant-modeler |
| Settlement averaging window | Simple arithmetic mean of **60 BRTI prints over the final 60 s** before the reference time. Final value rounded to nearest $0.01 of BTC price. **n=60 VERIFIED** ("60 RTI prices are collected"). **1 s spacing now VERIFIED** — 0.3 confirmed the BRTI standard feed is 1 Hz at top-of-second (prints aligned to integer UTC seconds). **The window-endpoint alignment (prints at close−59…close vs. the 60 s strictly before close) stays PARTIAL** — the 60 s averaging is a *Kalshi* construct, not defined in the CF RTI methodology, so this off-by-one-second placement (which shifts the load-bearing near-expiry variance correction) can only be closed by Kalshi's exact wording or by empirically regressing both candidate averages vs. published settlements. Also PARTIAL: whether Kalshi ingests the 1 Hz or 200 ms feed. | Market `rules_primary` ("the simple average of the sixty seconds of CF Benchmarks' BRTI before [close]"); series `important_info` ("60 RTI prices are collected..."); contract terms PDF; BRTI cadence from methodology v17.0 §6.2/§7 (see 0.3) | 2026-10-16 | quant-modeler + index-replicator |
| Contract type / strike mechanism | **Up/down**, not a fixed-strike ladder. One market per 15-min window (ticker suffix `-00`). Resolves **YES iff the 60 s BRTI average before CLOSE is ≥ the 60 s BRTI average before OPEN** (ties → YES). The "strike" is the realized open-window 60 s average, fixed at open; pre-open the API shows `Target price: TBD`. No `strike_type`/`floor_strike`/`cap_strike` fields present. | Market `rules_primary` ("...is at least the simple average of the sixty seconds of BRTI before [open], then... resolves to Yes"); absence of strike fields in market JSON; contract terms PDF ("at least X" = "X or greater") | 2026-10-16 | quant-modeler |
| Open / close / expiration | Each market trades exactly **15 minutes**, aligned to clock quarter-hours (:00/:15/:30/:45), **24/7**. `close_time` = top of the 15-min boundary = end of the settlement reference window. `frequency` = "fifteen_min". Times in UTC (human rules quoted in ET). NOTE: `expiration_time`/`latest_expiration_time` is a ~1-week-out administrative backstop — **do not** use it as the settlement instant; use `close_time`. | Market `open_time`/`close_time` (e.g. open 2026-10-18T02:45:00Z, close 03:00:00Z); series `frequency`; contract terms PDF | 2026-10-16 | quant-modeler |
| Tick size | Minimum tick **$0.001 (0.1¢)**, tapered deci-cent structure: 0.1¢ steps in [0.000, 0.100] and [0.900, 1.000], 1¢ steps in (0.100, 0.900). Sub-penny ticks exist in the wings. | Market `price_level_structure` = "tapered_deci_cent", `price_ranges`; contract terms PDF ("Minimum Tick size... shall be $0.001") | 2026-10-16 | quant-modeler |
| Position limits | **$25,000 per strike, per Member** Position Accountability Level (a reporting/accountability threshold, **not** confirmed as a hard contract cap). A hard per-market/per-series cap, if any, lives in Kalshi's general Rulebook / membership agreement — **UNVERIFIED** (PARTIAL). No `risk_limit`/position-limit field in market or series JSON. | Contract terms PDF `https://assets.kalshi.com/contract_terms/CRYPTO.pdf` | 2026-10-16 | quant-modeler |

## KXBTC15M fees (ROADMAP 0.2)

| Fact | Value | Source (URL) | Verified on | By |
|---|---|---|---|---|
| `fee_type` | **"quadratic"** | Kalshi series API `https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M` (`"fee_type": "quadratic"`) | 2026-10-16 | quant-modeler |
| `fee_multiplier` (M_taker) | **1** (dimensionless scalar on the base rate, NOT a bp value or the rate itself) | Kalshi series API, same endpoint (`"fee_multiplier": 1`) | 2026-10-16 | quant-modeler |
| Market-level fee fields | **None** — fees are series-level; no `fee_type`/maker field on the market object | Kalshi markets API `?series_ticker=KXBTC15M` (absence confirmed) | 2026-10-16 | quant-modeler |
| Taker formula & rate | `round_up_to_cent(M_taker · 0.07 · C · P · (1−P))` → KXBTC15M taker rate = 1 · 0.07 = **0.07**. **PARTIAL (single-source corroboration):** the 0.07 base, round-UP, and C·P·(1−P) shape are quoted by Allium's fee docs, and Allium's own worked table (100 contracts → 0.63/1.32/1.75/1.32/0.63 at P=0.10/0.25/0.50/0.75/0.90) is internally self-consistent with the 0.07 formula — but the formula and the table come from the **same third party (Allium)**, so this is one source, not independent corroboration. Kalshi's own fee-schedule PDF could **not** be fetched on 2026-10-16 (HTTP 429 / JS app-shell), so primary access is unconfirmed. | `https://docs.allium.so/historical-data/predictions/kalshi/series-fee-changes.md`; `https://allium.so/blog/kalshi-fees-why-the-cost-peaks-near-50-cents/`; cross-check vs. Kalshi API `fee_type`/`fee_multiplier` | 2026-10-16 | quant-modeler |
| Maker formula & rate | `round_up_to_cent(M_maker · 0.0175 · C · P · (1−P))`; baseline M_maker = 0. **PARTIAL (corroboration)**, same PDF-access caveat as above. | Allium fee docs (same URLs) | 2026-10-16 | quant-modeler |
| Rounding | Round **UP** to the next whole cent, **per order**. **PARTIAL** — round-up corroborated; per-order (vs per-fill) wording not re-read from the primary PDF. | Allium blog ("rounds the calculated fee up to the next whole cent") | 2026-10-16 | quant-modeler |
| KXBTC15M maker regime | **MAKER-FREE** (treated as such). **PARTIAL** — inferred from baseline M_maker=0 and the absence of any maker-fee field in the series/market JSON; the authoritative live maker-fee series list is on `kalshi.com/fee-schedule`, which could not be fetched on 2026-10-16. | Kalshi series/market API (absence) + baseline schedule | 2026-10-16 | quant-modeler |
| Caps / minimums / crypto-specific fee | **None found** (UNVERIFIED-as-absent, not asserted) | — | 2026-10-16 | quant-modeler |

### Fee verification notes
- The API fields (`fee_type="quadratic"`, `fee_multiplier=1`) are **primary-VERIFIED** (Kalshi's own API).
  The 0.07/0.0175 coefficients, round-up, and per-order application rest on **single-source (Allium)
  corroboration only** — the formula and Allium's worked table come from the same third party, so they
  are self-consistent, not independently cross-checked. Kalshi's primary fee-schedule PDF
  (`kalshi.com/fee-schedule`, `kalshi.com/docs/kalshi-fee-schedule.pdf`) returned HTTP 429 to WebFetch and
  a JS app-shell to curl on 2026-10-16. **To upgrade to primary-VERIFIED:** a human/authenticated fetch of
  the Kalshi fee PDF (or a hand-transcribed real Kalshi fee charge used as an independent test oracle)
  should confirm the 0.07/0.0175, round-UP-per-order, and that KXBTC15M is not on the maker-fee series
  list. `fees.py` and `tests/test_fees.py` retain PARTIAL tags until then.
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
| λ (exp weighting rate) | **λ = 10.3 / v_T** — **PARTIAL** (operator not confirmable from PDF text layer; dimensional analysis forces 1/BTC ⇒ only `/v_T` valid). **TOP-priority** open item — sets depth-weighting of every print; close via rendered PDF or empirical print-match. | Methodology v17.0 §6.1 (`brti-spec.md` O-2) | 2026-10-16 | index-replicator |
| Order-size cap mean | `C_T = s̄ + 5σ`, literal **s̄ = trimmed mean (Eq. 4e)**, σ = winsorized std (Eq. 4h) — **PARTIAL** (winsorized `s̄′` is an alternative reading; close empirically). | Methodology v17.0 §4.1.3, Eq. 5 (`brti-spec.md` O-4) | 2026-10-16 | index-replicator |
| Settlement window endpoint | **Not defined by the CF RTI methodology** — the 60 s average is a Kalshi construct. `[T−59,T]` vs `[T−60,T−1]` stays **PARTIAL** (resolve empirically; see 0.1). | Methodology v17.0 (absence) | 2026-10-16 | index-replicator |

### BRTI verification notes
- **CME mirror (`cmegroup.com/trading/files/...`) returned HTTP 403** (scraping block) on 2026-10-16 — same
  pattern as the Kalshi fee PDF — so the CME copy could NOT be cross-checked; all facts come from the CF
  Benchmarks docs host (primary). Red-team independently re-fetched the CF PDFs and confirmed the VERIFIED rows.
- Open items carried to Phase 2 (full list in `docs/brti-spec.md` §7): **O-2 λ operator (top priority)** and
  **O-4 cap mean** both feed every index print → resolve empirically vs. published BRTI before trusting fair
  values. **O-7/O-8**: which of the 7 constituents we actually record (Gemini/Bullish/Crypto.com/LMAX may be
  missing) and USDC-vs-native-USD market mapping — UNVERIFIED, audit `data/clean` in the tracking-error study.

## Venue feeds (ROADMAP 0.4)

Full recorder contract: `docs/feeds.md`. Load-bearing summary (gap-detection + resync drive ROADMAP
1.1's "gaps logged, not silent"). Red-team independently re-verified the Coinbase `sequence`, Kraken
CRC32 checksum (incl. worked example `1063832831`), and Binance `pu` rules against primary docs.

| Venue | BRTI? | Endpoint (prod) | Auth | Gap-detection field | Status |
|---|---|---|---|---|---|
| Coinbase Exchange `full`/`level3` | constituent | `wss://ws-direct.exchange.coinbase.com` | API key + HMAC-SHA256 (reqd since 2023-07-01) | **`sequence`** (jump > 1) | VERIFIED (`level3` resync + max-conns PARTIAL) |
| Kraken v2 `level3` | constituent | `wss://ws-l3.kraken.com/v2` (⚠ 3-way host conflict) | REST `GetWebSocketsToken` → `token` | **`checksum`** (CRC32 top-10/side; NO seq number) | VERIFIED (host + resync PARTIAL) |
| Bitstamp `live_orders_{pair}` | constituent | `wss://ws.bitstamp.net` (unconfirmed) | public | **`microtimestamp`** + MarketEventID (`since_id` backfill) | PARTIAL (WS v2 page Incapsula-blocked) |
| Binance USDⓈ-M `@depth`+`@aggTrade` | lead signal | `wss://fstream.binance.com` | public | **`pu`** (`pu == prev.u`; ≠ spot's `U==prev.u+1`) | VERIFIED resync/`pu` (speeds/trades/limits PARTIAL) |
| Bybit v5 linear `orderbook`+`publicTrade` | lead signal | `wss://stream.bybit.com/v5/public/linear` | public | **`type=="snapshot"`** reset (no numeric `u`-gap rule) | VERIFIED incl. trades |

### Feed verification notes
- We **read market data only** — Coinbase HMAC / Kraken token are used solely to subscribe; no order path in a recorder.
- Open items (full list in `docs/feeds.md` §7): **Bitstamp** WS v2 page is Incapsula-blocked → endpoint
  host, `bts:subscribe` envelope, snapshot reconciliation, and limits PARTIAL (schema + `order_data`
  backfill confirmed from the reachable REST primary). **Binance** diff-depth stream speeds + U/u/pu field
  example are from a search excerpt (SPA body unrendered) — PARTIAL; the resync/`pu` algorithm is VERIFIED.
  **Kraken** host is a 3-way conflict (`ws-l3` / `ws-auth` / `ws`) → recorder tries all three with fallback.
  Both Bitstamp and the Binance stream pages want a human/authenticated fetch before coding.

### Verification notes
- Ticker-specific values (open/close examples) are from the 2026-10-18 session markets live at fetch
  time (2026-10-16); the structural facts (window, source, tick, 15-min frequency, up/down mechanic)
  are series-level and stable.
- The `CRYPTO.pdf` is a parameterized template covering several crypto products. Its template default
  "Expiration Time... 10:00 AM ET" and "latest Expiration Date one week after" describe the daily/other
  crypto products, **not** the 15-min cadence; the 15-min timing comes from the API market objects.
- `WebFetch` could not decode the compressed PDF; it was read via `curl` + `pypdf`.
