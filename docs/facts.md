# Verified facts

Every fact the code depends on, with source and verification date.
Anything not in this table is UNVERIFIED.

## KXBTC15M contract terms (ROADMAP 0.1)

| Fact | Value | Source (URL) | Verified on | By |
|---|---|---|---|---|
| Settlement source | CF Benchmarks **Bitcoin Real-Time Index (BRTI)** — the CME CF Bitcoin Real-Time Index, published once per second, 24/7. (Not BRRNY, which is a once-daily reference rate.) | API series `settlement_sources` + market `rules_primary`: `https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M`; contract terms PDF `https://assets.kalshi.com/contract_terms/CRYPTO.pdf`; CME CF BRTI methodology `https://www.cmegroup.com/trading/files/bitcoin-real-time-index-methodology-version-2.pdf` | 2026-10-16 | quant-modeler |
| Settlement averaging window | Simple arithmetic mean of **60 BRTI prints over the final 60 s** before the reference time. Final value rounded to nearest $0.01 of BTC price. **n=60 is VERIFIED** ("60 RTI prices are collected"). **The 1 s sample spacing and the exact window endpoint alignment (prints at close−59…close vs. the 60 s strictly before close) are INFERRED** from "BRTI is a 1 Hz index" — **PARTIAL**. This off-by-one-second placement shifts the load-bearing near-expiry variance correction; cite the BRTI/settlement timing from the CF methodology to upgrade to VERIFIED. | Market `rules_primary` ("the simple average of the sixty seconds of CF Benchmarks' BRTI before [close]"); series `important_info` ("60 RTI prices are collected... average of these prices, rounded to the nearest 2 decimal places"); contract terms PDF; spacing inferred from CME CF BRTI methodology (1 Hz) | 2026-10-16 | quant-modeler |
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

### Verification notes
- Ticker-specific values (open/close examples) are from the 2026-10-18 session markets live at fetch
  time (2026-10-16); the structural facts (window, source, tick, 15-min frequency, up/down mechanic)
  are series-level and stable.
- The `CRYPTO.pdf` is a parameterized template covering several crypto products. Its template default
  "Expiration Time... 10:00 AM ET" and "latest Expiration Date one week after" describe the daily/other
  crypto products, **not** the 15-min cadence; the 15-min timing comes from the API market objects.
- `WebFetch` could not decode the compressed PDF; it was read via `curl` + `pypdf`.
