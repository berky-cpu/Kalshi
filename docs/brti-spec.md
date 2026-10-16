# BRTI implementable specification (CME CF Bitcoin Real-Time Index)

**Purpose.** Implementation spec for a replica of the CME CF Bitcoin Real-Time Index
(ticker **BRTI**), the index KXBTC15M settles to (settlement link verified in ROADMAP 0.1,
see `docs/facts.md`). Written strictly from CF Benchmarks / CME CF primary methodology
documents. Every load-bearing fact carries a VERIFIED / PARTIAL / UNVERIFIED status and a
source. Verification date for all entries: **2026-10-16**.

**Methodology version implemented against (cite this):**
- *CME CF Cryptocurrency Real Time Indices — Methodology Guide*, **Version 17.0, dated 21
  September 2026** (the algorithm, parameters, contingency rules). This is the current
  version as served from CF Benchmarks' docs host on 2026-10-16.
- *CME CF Cryptocurrency Pricing Products — Constituent Exchanges List*, **Version 13.7,
  24 August 2026** (the constituent table).
- *CME CF Cryptocurrency Pricing Products — Constituent Exchange Criteria*, **Version 8.5,
  11 May 2026** (eligibility thresholds).

> Scope note: this document specifies the **real-time index (BRTI)** only. The **60 s
> settlement average** that KXBTC15M resolves on is a Kalshi construct layered on top of
> BRTI (ROADMAP 0.1); the CF *Real Time Indices* methodology does **not** define any
> settlement window. See Section 5 for exactly what the methodology does and does not pin
> down about print timing.

---

## 1. Constituents

**Relevant Pair:** Bitcoin vs **U.S. Dollar** (BTC/USD). Quote asset includes markets where
USD is made fungible 1:1 with an *Accepted Asset* — currently only **USDC** — by the
constituent exchange as a means of transacting (so a venue's USDC-quoted BTC book can count
as BTC/USD input). **Status: VERIFIED.**

**Current BRTI constituent exchanges (active as of the v13.7 list, i.e. after the 27-Jul-2026
itBit suspension):**

| Exchange | Added | Notes |
|---|---|---|
| Bitstamp | 14-Nov-2016 | |
| Coinbase | 14-Nov-2016 | Table footnote: "Includes trades and orders from Coinbase, Coinbase Pro and Coinbase Prime" |
| Kraken | 14-Nov-2016 | |
| Gemini | 30-Aug-2019 | |
| LMAX Digital | 03-May-2021 | |
| Bullish Exchange | 30-Dec-2024 | |
| Crypto.com | 31-Mar-2025 | |

**Seven active constituents.** **Status: VERIFIED** (CF Benchmarks Constituent Exchanges
List v13.7, BRTI table).

> Provenance note on add-dates: the **7-count**, the **itBit 27-Jul-2026 suspension**,
> **Bullish 30-Dec-2024** and **Crypto.com 31-Mar-2025** are corroborated by the document's
> change-log. The per-venue **BTC add-dates for Gemini (30-Aug-2019) and LMAX (03-May-2021)**
> are taken from the Administrator's **published roster table** in the same PDF, not from the
> change-log, so treat those two dates as roster-sourced. (The change-log also contains a
> "LAMX Digital" typo for LMAX at v12.4.) The authoritative live roster is published on the
> Administrator's site (`https://www.cfbenchmarks.com`) and `https://www.cmegroup.com/cryptocurrencies`.

**Suspended / removed from BRTI (do NOT include):**

| Exchange | Suspended | Notes |
|---|---|---|
| Bitfinex | Apr-2017 | Original 2016 constituent, suspended |
| OKCoin.com (HK) | Apr-2017 | Original 2016 constituent, suspended |
| itBit | **27-Jul-2026** | The July-2026 list change. itBit (Paxos) formally suspended from BRR/BRRNY/BRRAP/**BRTI** and the ETH/LTC products (list v13.6, 27 Jul 2026). itBit had earlier been suspended from BRR only on 25-Jan-2019, reinstated 01-Feb-2019. |

> **July 2026 change (the one the task flags):** itBit Paxos was suspended from BRTI on
> **27-Jul-2026** (methodology list v13.6). A replica covering dates on/after 2026-07-27
> must exclude itBit; a replica of earlier dates must include it. **Status: VERIFIED.**

**Eligibility criteria (for completeness; not needed to run the replica, but defines who is in
the list):** A venue qualifies if it offers spot trading of the Relevant Pair, exposes trade
and order data via API "with sufficient reliability, detail and timeliness", and in the
Oversight Committee's opinion meets five criteria, the quantitative one being a volume
threshold: for **addition to an existing index**, "the average daily volume the venue would
have contributed during the observation window for the Reference Rate of the Relevant Pair
exceeds 3% over a continuous period of 90 calendar days" (new-index launch uses 180 days).
Continued inclusion is reviewed if the contribution "fall[s] below 3% for any Reference
Rate". Criteria 2–5 cover fair/transparent markets and anti-manipulation controls, no undue
barriers/risk, legal/regulatory compliance (KYC/AML), and cooperation + a CME data-sharing
agreement. **Status: VERIFIED** (Constituent Exchange Criteria v8.5, §3).

**Implication for our recorders:** of the seven, we must confirm which we actually record.
Any constituent we do NOT record (candidates the task names: **Gemini, Bullish, Crypto.com,
LMAX**) is a source of replica error that the tracking-error study must quantify by comparing
a full-constituent vs recorded-subset reconstruction. The methodology weights venues only
implicitly (through their contribution to the consolidated book — see §3); it is **not**
equal-weighted or volume-weighted per venue, so a missing venue's impact is non-linear and
depends on where its liquidity sits in the consolidated book.

---

## 2. Publication cadence

- **Internal calculation (Effective Time `T`) for BRTI: every 200 milliseconds.** The
  Real Time Index Parameters table (§6.2) lists BRTI's Effective Time as **"Every 200
  milliseconds"** with a Retrieval Lag Threshold of **10 seconds**. **Status: VERIFIED.**
- **Dissemination (the feed you actually get):** BRTI specification (§7) states
  Dissemination Time = **"Standard: Approximately every second (top-of-second) of each day
  for the entire year including weekends and holidays. Maximum: Approximately every 200
  milliseconds (upon request)."** **Status: VERIFIED.**
- **Dissemination precision: 0.01 U.S. Dollars** (BRTI spec, §7). **Status: VERIFIED.**

**Resolution of the 0.1 inferred point:** BRTI is computed every **200 ms** internally; the
*standard* published cadence is **1 Hz at top-of-second**, and a 200 ms feed is available
"upon request". So ROADMAP 0.1's "1 Hz" is correct for the *standard* public feed, but the
true index updates 5× faster. Which cadence Kalshi ingests for its 60 s settlement average is
a Kalshi question, not answered by the CF methodology (see §5). **Status: VERIFIED** for the
cadence itself; its link to Kalshi settlement remains **PARTIAL**.

---

## 3. Aggregation algorithm (implementable)

The index is **order-book based** (not trade based), calculated from the Relevant Order Books
of all constituent exchanges at each Effective Time `T`. There is **no explicit per-venue
weight**: all venues' books are merged into one consolidated book and the index is a depth-
weighted average over that consolidated book.

### 3.1 Symbols / parameters (BRTI values)

| Symbol | Meaning | BRTI value | Source |
|---|---|---|---|
| `T` | Effective time (calc instant) | every 200 ms | §6.2 |
| `s` | Spacing (price-volume curve granularity, in BTC volume units) | **Dynamic** (computed per §3.6) | §6.2 |
| `D` | Max deviation from mid defining utilized depth | **0.5%** | §6.2 |
| PED | Potentially-erroneous-data deviation parameter | **5%** | §6.2 |
| `C_T` | Order size cap (per price level, BTC) | **Dynamic** (per §3.7) | §6.2 |
| `λ` | Exponential weighting rate | **λ = 10.3 / v_T** (scales with utilized depth) | §6.1 |
| `τ` | Target levels (dynamic-spacing target) | **100** | §6.1 |
| Retrieval Lag Threshold | staleness cutoff | **10 s** | §6.2 |

> Note on `λ`: the Constants table renders Lambda as `10.3  v_T` (bare space; the PDF text
> layer does not preserve the operator). The reading must be **λ = 10.3 / v_T** on
> **dimensional grounds**: in `e^(−λv)` the exponent `λv` must be dimensionless, and both `v`
> and `v_T` are in BTC, so `λ` must have units of 1/BTC. Only `10.3/v_T` is dimensionally
> valid — a fixed scalar `10.3` is dimensionless (invalid) and `10.3·v_T` has units of BTC
> (invalid). A consequence: `λ·v_T = 10.3` is then constant, so the exponential weight always
> decays to ~`e^(−10.3) ≈ 3.4e−5` at the utilized depth — a scale-invariant, sensible design.
> **Status: PARTIAL** — the operator could not be confirmed from the PDF text layer; confirm
> via a rendered PDF and/or empirical print-match (see O-2, the top-priority open item).

### 3.2 Order-book consolidation (steps 1–2)

1. At `T`, collect the Relevant Order Book of each **included** constituent (after the
   staleness/erroneous/potentially-erroneous filters of §4 have removed any venue or entry).
   Each book is a list of `(price, size)` levels, bids descending by price, asks ascending,
   with size = aggregate size of all orders at that price.
2. Merge all venues' levels into **one consolidated book**. When merging, **cap each price
   level's size at the order size cap `C_T`**: if the aggregated size at a bid/ask price
   exceeds `C_T`, it enters the consolidated book with size exactly `C_T`.
   - 2a. **Uncross the consolidated book.** While highest bid price ≥ lowest ask price:
     remove `min(size_at_best_bid, size_at_best_ask)` from each side; any level reduced to
     zero is removed, a partially consumed level keeps its price with residual size. Stop
     when best bid < best ask, or when one side is fully consumed.

### 3.3 Price-volume curves (step 3)

Build four curves over cumulative volume `v`, sampled on the grid `v ∈ {s, 2s, 3s, …}`:

- **Ask PV curve** `askPV_T(v)`: the marginal ask price at which cumulative volume `v` is
  bought from the consolidated book. Formally (Eq. 1a, then snapped to the grid in 1c):
  `âskPV_T(v) = ap_{T,j+1}` where `Σ_{i=1..j} ȧs_{T,i} < v ≤ Σ_{i=1..j+1} ȧs_{T,i}`, and
  `askPV_T(v) = âskPV_T(s·⌈v/s⌉)`.
- **Bid PV curve** `bidPV_T(v)` (Eq. 1b/1d): the marginal bid price to sell cumulative
  volume `v`, defined symmetrically on the bid side.
- **Mid PV curve** (Eq. 1e): `midPV_T(v) = (askPV_T(v) + bidPV_T(v)) / 2`.
- **Mid spread-volume curve** (Eq. 1f): `midSV_T(v) = askPV_T(v) / midPV_T(v) − 1`
  (the fractional deviation of the ask curve from the mid curve at depth `v`).

### 3.4 Utilized depth (step 4, Eq. 2)

`v_T = max( v_i  such that  midSV_T(v_i) ≤ D  and  midSV_T(v_{i+1}) > D ,  s )`

i.e. the largest cumulative volume on the grid for which the mid-spread curve is still within
`D = 0.5%` of mid, floored at one spacing step `s`. (If essentially no depth qualifies, `v_T`
falls back to `s`, and the index reduces to the consolidated best mid — §4.2 "A Note on
Properties".)

### 3.5 Exponential weighting and the index value (steps 5–6, Eq. 3)

Weight the mid PV curve by the normalized exponential density up to `v_T`:

```
NF  = Σ_{v ∈ {s,2s,…,v_T}}  λ · e^(−λ v)          # normalization factor, so weights sum to 1
BRTI_T = Σ_{v ∈ {s,2s,…,v_T}}  midPV_T(v) · (1/NF) · λ · e^(−λ v)
```

So BRTI is the depth-weighted average of the consolidated mid price-volume curve, with weights
decaying exponentially in depth (near-top-of-book levels dominate). Final value rounded to the
dissemination precision **$0.01**. **Status of the full algorithm: VERIFIED** (transcribed
from §4.1.1–4.1.2, Eq. 1a–3).

### 3.6 Dynamic spacing `s` (§4.1.4, Eq. 6–7)

`s` is **not** fixed for BRTI; it is computed each `T` from the consolidated book so the grid
reflects current liquidity. Two candidates are combined by a `min`:

- **Level-based** (Eq. 6a): `s_lev = min(total_ask_liq, total_bid_liq) / τ`, with `τ = 100`
  target levels. (`aΣ_{T,|A_T|}`, `bΣ_{T,|B_T|}` are the total cumulative sizes of the ask
  and bid sides.)
- **KDE-based** (Eq. 6b–6j): on the **lower-liquidity side**, take the size sample set,
  winsorize the bottom/top `k = ⌊0.01 n_T⌋`, compute winsorized std `σ_side`, Silverman
  bandwidth `h = 1.06 · σ_side · n_T^(−1/5)`, Gaussian KDE `f_KDE(s)`, and set
  `s_KDE = argmax_s f_KDE(s)` if `n_T ≥ 4`, else `s_KDE = ` the smallest sampled size.
- **Final (Eq. 7):** `s_T = min(s_lev, s_KDE)`.

**Status: VERIFIED** (formulas transcribed). Implementation caveat: `argmax_s f_KDE(s)` needs
a search grid/optimizer; the methodology does not specify the grid, so make it a parameter and
pick resolution that is immaterial to the output (Open Item O-3).

### 3.7 Dynamic order size cap `C_T` (§4.1.3, Eq. 4–5)

Computed from the **uncapped** consolidated book, then applied in step 2:

1. Count "near-top" levels: `ac_T = max( max{ i : ap_{T,i} ≤ 1.05·ap_{T,1} }, min(|A_T|,50) )`
   on the ask side; `bc_T = max( max{ i : bp_{T,i} ≥ 0.95·bp_{T,1} }, min(|B_T|,50) )` on the
   bid side. (So: all levels within ±5% of best, but at least the top 50 per side when
   available.)
2. Pool those sizes into `S_T` (ascending). Trim count `k = ⌊0.01 n_T⌋`.
3. Winsorize `S_T` at `k` from each end → `S′_T`; compute winsorized mean `s̄′` and winsorized
   sample std `σ` (Eq. 4f–4h).
4. **Cap (literal/default reading): `C_T = s̄ + 5σ`** (Eq. 5), where **`s̄` is the TRIMMED
   mean of Eq. 4e** (bare symbol, no prime) and **`σ` is the winsorized sample std of
   Eq. 4h**. Under this literal reading nothing is vestigial: the trimmed mean `s̄` (Eq. 4e)
   feeds Eq. 5 and the winsorized mean `s̄′` (Eq. 4g) feeds `σ` (Eq. 4h). An alternative
   inference `C_T = s̄′ + 5σ` (winsorized mean) is documented but is NOT the literal symbol.

**Status: PARTIAL** — the structure is VERIFIED but the exact mean entering Eq. 5 (trimmed
`s̄` per the literal symbol vs winsorized `s̄′`) is a close-empirically item; see Open Item
O-4. `σ` in Eq. 5 is the winsorized sample std (Eq. 4h) under either reading.

   > Minor open item (F4): the methodology says the cap is computed from "the uncapped
   > consolidated order book" but does not state whether that book is taken **before** or
   > **after** the cross-venue uncrossing of §3.2-2a. This replica assumes **pre-uncross**
   > (cap computed from the raw merged book, as in the §3.8 pseudocode). Make this a
   > parameter; close by matching published BRTI.

### 3.8 Reference pseudocode

```
def brti(books_by_venue, params):          # books at a single Effective Time T
    books = drop_stale_and_erroneous(books_by_venue, params)     # §4
    books = drop_potentially_erroneous(books, PED=0.05)          # §4.3 (median-mid filter)
    if not books: raise CalculationFailure                       # §5.5

    uncapped = consolidate(books)                                # merge, no cap yet (pre-uncross)
    C_T      = trimmed_mean(uncapped) + 5*winsorized_std(uncapped)  # §3.7 Eq 5: C_T = s̄ + 5σ
                                                                 #   s̄ = TRIMMED mean (Eq 4e, bare symbol);
                                                                 #   σ  = winsorized sample std (Eq 4h)
    cons     = consolidate(books, cap=C_T)                       # §3.2 step 2 (cap per level)
    cons     = uncross(cons)                                     # §3.2 step 2a

    s_T      = dynamic_spacing(cons, tau=100)                    # §3.6 Eq 6–7
    grid     = [s_T, 2*s_T, 3*s_T, ...]
    askPV    = ask_curve(cons, grid); bidPV = bid_curve(cons, grid)
    midPV    = (askPV + bidPV) / 2
    midSV    = askPV / midPV - 1
    v_T      = utilized_depth(midSV, grid, D=0.005)              # §3.4 Eq 2
    lam      = 10.3 / v_T                                        # §6.1  (PARTIAL reading)

    w        = [lam*exp(-lam*v) for v in grid if v <= v_T]
    NF       = sum(w)
    return round( sum(midPV(v)*wi/NF for v,wi in zip(grid,w)), 2 )   # $0.01
```

---

## 4. Input handling / edge cases (§5 Contingency Calculation Rules)

All **VERIFIED** (transcribed from §5.1–5.5):

1. **Delayed / stale data (§5.1).** If `CalculationTime − RetrievalTime` for a venue's book
   exceeds the **Retrieval Lag Threshold (10 s for BRTI)**, that venue is **disregarded** for
   that Calculation Time. If *all* venues exceed it → **calculation failure** (no print).
   - `RetrievalTime` definition matters for our recorder: for a REST/request-response API it
     is the request time; for a websocket feed it is "the most recent time as of which the
     Calculation Agent has a valid Order Book from an unbroken connection". So a websocket
     book is considered current as long as the connection is alive and valid — staleness is
     measured from the last valid update on an unbroken connection, not from the last message.
2. **Erroneous books (§5.2.1).** A venue's book is flagged erroneous and **disregarded** if:
   (a) it cannot be parsed / wrong format; (b) it has **no bids or no asks**; (c) it is
   **crossed** (its own best bid ≥ best ask). All venues erroneous → calculation failure.
   - Note: the per-venue crossed-book check (c) is distinct from the consolidated-book
     uncrossing in §3.2-2a. A single venue that is internally crossed is dropped entirely;
     cross-venue crossing is resolved by uncrossing the merged book.
3. **Erroneous prices / entries (§5.2.2).** Any individual level with a **non-numeric,
   non-positive price or size** is dropped (just that entry, not the whole book).
4. **Potentially-erroneous data (deviation filter) (§5.3).** Per Calculation Time:
   - Compute each venue's own mid = (best bid + best ask)/2.
   - Compute the **median** of all venues' mids.
   - If a venue's `|venue_mid / median_mid − 1|` exceeds the **Potentially Erroneous Data
     Parameter (5% for BRTI)**, that venue's book is **disregarded** for that Calculation
     Time and the flag is reported to the Oversight Committee.
   - **Hysteresis (§5.3.4):** once dropped, a venue stays dropped until its deviation falls
     below **50% of the parameter (i.e. 2.5% for BRTI)**, then it is reinstated. (This makes
     the filter stateful across time — the replica must carry per-venue dropped/active state,
     not recompute statelessly each tick.)
   - All venues flagged → calculation failure.
5. **Expert judgement (§5.4).** Not used in day-to-day calculation; reserved for
   extraordinary circumstances under codified policy. (Not reproducible in a replica — note
   as a divergence source only.)
6. **Calculation failure (§5.5).** If the index cannot be computed (all venues stale, all
   erroneous, all potentially-erroneous, or any other blocking reason) the index for that
   Calculation Time is **not published** (gap in the series). A replica must emit a gap, not
   interpolate.

**Snapshot vs incremental handling:** the methodology specifies the index operates on the
"Relevant Order Book" = currently unmatched limit orders aggregated by price as reported
through the venue API; it does **not** prescribe snapshot-vs-delta reconstruction mechanics —
that is the recorder's responsibility (maintain a correct live book per venue; the "retrieval
time" is the last valid state on an unbroken connection). **Status: PARTIAL** — book
*maintenance* mechanics are out of scope of the methodology.

---

## 5. Settlement-window timing

**What the methodology pins down (VERIFIED):**
- BRTI prints are produced at **top-of-second** on the standard feed (and every 200 ms on the
  max feed). So the discrete 1 Hz prints that a 60 s average would consume are aligned to
  integer UTC seconds.
- Dissemination precision is **$0.01**, calculated 24/7/365.

**What it does NOT pin down (the 0.1 open item):** The *Real Time Indices* methodology defines
**no settlement window and no averaging** — BRTI is an instantaneous index. The 60 s average
KXBTC15M resolves on comes from **Kalshi's** contract rules (0.1), not from this document.
Therefore the endpoint question — whether the 60 s average ending at reference time `T`
covers prints at `[T−59 s, T]` (inclusive of the `T` print) or the 60 s strictly before,
`[T−60 s, T−1 s]` — **cannot be resolved from the CF Real Time Indices methodology**. It is a
Kalshi definitional matter.

> **Resolution status: PARTIAL (unchanged from 0.1).** The BRTI side is now firm (top-of-
> second 1 Hz prints, $0.01), but the window endpoint alignment is a Kalshi convention. To
> close it we need either (a) Kalshi's precise wording of which 60 prints are averaged, or
> (b) empirical reconstruction: replicate BRTI, compute both `[T−59,T]` and `[T−60,T−1]`
> averages, and regress against published KXBTC15M settlement values to see which off-by-one
> reproduces them. (b) is the recommended closer and feeds directly into the tracking-error
> study. A separate CF *settlement*/reference-rate methodology exists for the BRR family
> (daily/fixing TWAP products) but does **not** govern BRTI or the KXBTC15M 60 s average.

**Load-bearing implication:** this one-second placement shifts the near-expiry variance
correction in `docs/derivations/settlement-averaging.md`. Keep the window endpoint a
**parameter** (`window = [T-59, T]` vs `[T-60, T-1]`) and resolve empirically; do not hard-code.

---

## 6. Versioning

| Document | Version | Date | Used for |
|---|---|---|---|
| CME CF Cryptocurrency Real Time Indices — Methodology Guide | **17.0** | 21 Sep 2026 | algorithm, parameters (§2–§4 above) |
| CME CF Constituent Exchanges List | **13.7** | 24 Aug 2026 | constituent table (§1) |
| CME CF Constituent Exchange Criteria | **8.5** | 11 May 2026 | eligibility (§1) |

Relevant methodology history: dynamic depth since v2.0 (2017); dynamic order size cap since
v7.0 (Dec 2019); the v17.0 guide adds the **Dynamic Spacing** section (KDE-based `s`). A
replica must note which methodology version applies to the dates it reconstructs — e.g. if we
backtest pre-2019 data the order-size-cap rule differs. For our in-scope dates (2026) v17.0
applies.

---

## 7. Open / UNVERIFIED items

| ID | Item | Status | What would close it |
|---|---|---|---|
| O-1 | **CME-hosted PDFs unreachable.** `cmegroup.com/trading/files/bitcoin-real-time-index-methodology*.pdf` returned **HTTP 403** ("IP blocked due to suspected web scraping") to curl; WebFetch of the CME education pages was not used as a primary source. All methodology facts here come from the **CF Benchmarks docs host** (`docs.cfbenchmarks.com`), which is the administrator and co-publisher — a primary source — so facts are VERIFIED, but the CME mirror could not be cross-checked. | PARTIAL (source-access) | A human/authenticated fetch of the CME-hosted methodology PDF, or the CME education page, to cross-check v17.0. |
| O-2 | **λ = 10.3 / v_T operator (TOP-PRIORITY open item).** The Constants table renders Lambda as `10.3  v_T` (bare space — the PDF text layer does not carry the operator). The reading is fixed to `λ = 10.3 / v_T` on **dimensional grounds**: `λv` in `e^(−λv)` must be dimensionless and `v, v_T` are both BTC, so `λ` must be 1/BTC — only `10.3/v_T` qualifies (fixed `10.3` is dimensionless; `10.3·v_T` is BTC; both invalid). This yields the scale-invariant property `λ·v_T = 10.3` (weight decays to ~`e^(−10.3)` at the utilized depth). Because `λ` sets the depth-weighting applied to **every** print, confirming it is the single highest-impact remaining verification. | PARTIAL | Read the λ row in a rendered v17.0 PDF **and/or** confirm empirically by matching published BRTI prints — close this first. |
| O-3 | **`argmax_s f_KDE(s)` search grid** for dynamic spacing is unspecified (continuous argmax over a KDE). | PARTIAL | Methodology appendix (none found) or empirical: use a fine grid and show output invariance. |
| O-4 | **Which mean enters the order-size cap Eq. 5.** The literal Eq. 5 symbol is the **bare/trimmed mean `s̄` (Eq. 4e)** — no prime — so the default reading is `C_T = s̄ + 5σ`. (pypdf preserves primes elsewhere — it renders `s ′` on Eq. 4g and `s ′_side` on Eq. 6e — so the absence of a prime on Eq. 5 is real, not an extraction artifact.) The **winsorized mean `s̄′`** is the alternative inference. Under the literal reading nothing is vestigial: the trimmed mean (Eq. 4e) feeds Eq. 5 and the winsorized mean (Eq. 4g) feeds `σ` (Eq. 4h); the winsorized reading would force Eq. 4e to be dead text. `σ` is the winsorized sample std (Eq. 4h) under either reading. | PARTIAL | Close empirically by matching replica caps to published BRTI prints. |
| O-5 | **Settlement-window endpoint** `[T−59,T]` vs `[T−60,T−1]` — not defined by the RTI methodology (Kalshi construct). | PARTIAL (see §5) | Kalshi contract wording, or empirical regression of replica vs published KXBTC15M settlements. |
| O-6 | **Which cadence Kalshi ingests** (1 Hz standard vs 200 ms max feed) for its 60 s average. | PARTIAL | Kalshi settlement documentation / empirical. |
| O-7 | **USDC fungibility in practice for BTC/USD** — methodology permits USDC-quoted books as BTC/USD input; whether any BRTI constituent's BTC book is USDC-quoted (vs native USD) affects which of our recorded feeds map to a constituent. | UNVERIFIED | Per-venue confirmation of the exact market(s) CF ingests per constituent (not published at symbol level). |
| O-8 | **Per-venue recording coverage** — which of the 7 constituents we actually record, and the error from any missing (Gemini / Bullish / Crypto.com / LMAX). | UNVERIFIED (depends on our recorders) | Audit `data/clean` venue coverage; quantify in the tracking-error note. |

---

## Source table

| # | Fact | Value | Source URL | One-line quote | Status | Date |
|---|---|---|---|---|---|---|
| 1 | Constituents (BRTI) | Bitstamp, Coinbase, Kraken, Gemini, LMAX Digital, Bullish, Crypto.com (itBit suspended 27-Jul-2026) | `https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges.pdf` (v13.7, 24 Aug 2026); live roster `https://www.cfbenchmarks.com` / `https://www.cmegroup.com/cryptocurrencies` | BRTI table: "Bitstamp 14-Nov-16 … Kraken 14-Nov-16 … Gemini 30-Aug-19 … Bullish Exchange 30-Dec-24 … Crypto.com 31-Mar-25 … LMAX Digital 03-May-21"; "itBit … 27-Jul-26" | VERIFIED (7-count + itBit suspension change-logged; Gemini/LMAX add-dates roster-sourced — see §1 note) | 2026-10-16 |
| 2 | Relevant pair / Accepted Asset | BTC vs USD; USDC fungible 1:1 | Methodology v17.0 §2–§3 | "markets where the quote asset is made fungible with Accepted Assets on a 1:1 basis"; Accepted Asset List: "USD Coin USDC" | VERIFIED | 2026-10-16 |
| 3 | Eligibility threshold | ≥3% ADV over 90d (add) / 180d (launch) + 4 qualitative criteria | `https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges%20Criteria.pdf` (v8.5, 11 May 2026) | "the average daily volume the venue would have contributed … exceeds 3% over a continuous period of 90 calendar days" | VERIFIED | 2026-10-16 |
| 4 | Calculation cadence (internal) | Every 200 ms; retrieval lag 10 s | Methodology v17.0 §6.2 | BRTI row: "Every 200 milliseconds … 10 seconds" | VERIFIED | 2026-10-16 |
| 5 | Dissemination cadence | Standard 1 Hz top-of-second; max 200 ms on request | Methodology v17.0 §7 | "Standard: Approximately every second (top-of-second)… Maximum: Approximately every 200 milliseconds (upon request)" | VERIFIED | 2026-10-16 |
| 6 | Dissemination precision | $0.01 | Methodology v17.0 §7 | BRTI "Dissemination Precision 0.01 U.S. Dollars" | VERIFIED | 2026-10-16 |
| 7 | Algorithm (consolidate→curves→utilized depth→exp weight) | see §3 | Methodology v17.0 §4.1.1–4.1.2, Eq. 1a–3 | "The CME CF … Real Time Index is then given by the sum of the weighted mid price-volume curve" | VERIFIED | 2026-10-16 |
| 8 | Deviation-from-mid D | 0.5% | Methodology v17.0 §6.2 | BRTI "Deviation from Mid (D) 0.5%" | VERIFIED | 2026-10-16 |
| 9 | λ, τ constants | λ = 10.3/v_T; τ = 100 | Methodology v17.0 §6.1 | "Lambda (λ) 10.3 v_T; Target Level (τ) 100" | PARTIAL (λ reading) | 2026-10-16 |
| 10 | Order size cap | Dynamic, literal `C_T = s̄ + 5σ` (s̄ = trimmed mean Eq 4e; σ = winsorized std Eq 4h) | Methodology v17.0 §4.1.3, Eq. 5 | "The order size cap … C_T = s̄ + 5σ" (bare s̄, no prime) | PARTIAL (which mean enters Eq 5; see O-4) | 2026-10-16 |
| 11 | Dynamic spacing | `s_T = min(s_lev, s_KDE)`, τ=100 | Methodology v17.0 §4.1.4, Eq. 6–7 | "The spacing … s_T = min(s_lev, s_KDE)" | VERIFIED | 2026-10-16 |
| 12 | Staleness rule | drop venue if lag > 10 s; all stale → failure | Methodology v17.0 §5.1 | "the Constituent Exchange is disregarded … exceeds the Retrieval Lag Threshold" | VERIFIED | 2026-10-16 |
| 13 | Erroneous book | drop if unparseable / empty side / crossed | Methodology v17.0 §5.2.1 | "If the Relevant Order Book crosses, it is flagged as erroneous" | VERIFIED | 2026-10-16 |
| 14 | Deviation filter | drop if |mid − median mid| > 5%; reinstate < 2.5% | Methodology v17.0 §5.3 | "exceeds the Potentially Erroneous Data Parameter … disregarded"; "less than 50% of the Potentially Erroneous Data Parameter … re-instated" | VERIFIED | 2026-10-16 |
| 15 | Calculation failure | not published (gap) | Methodology v17.0 §5.5 | "the CME CF … Real Time Index for that Calculation Time is not published" | VERIFIED | 2026-10-16 |
| 16 | Settlement window endpoint | not defined by RTI methodology (Kalshi construct) | Methodology v17.0 (absence) | — | PARTIAL | 2026-10-16 |
| 17 | Methodology version | v17.0, 21 Sep 2026 | `https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf` | "Version: 17.0 Version Date: 21 September 2026" | VERIFIED | 2026-10-16 |
| 18 | CME mirror unreachable | HTTP 403 | `https://www.cmegroup.com/trading/files/bitcoin-real-time-index-methodology.pdf` | "This IP address is blocked due to suspected web scraping activity" | n/a (access note) | 2026-10-16 |
