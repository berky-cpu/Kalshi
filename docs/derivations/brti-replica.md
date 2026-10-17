# The BRTI replica — order-book → index, stage by stage

Scope: ROADMAP 2.2, the module `src/kbtc/index/`. This note derives the CME CF Bitcoin
Real-Time Index (**BRTI**) construction as we implemented it, one stage at a time, and ties
each formula to the function that computes it. Source of truth for the algorithm is
`docs/brti-spec.md` (CME CF *Cryptocurrency Real Time Indices Methodology Guide*, v17.0,
21 Sep 2026); every constant there is VERIFIED unless flagged. Parameter ambiguities the spec
leaves open are exposed as `BrtiParams` knobs (never hard-coded) and listed as open items O-3,
O-4, O-5, O-9 / F4 — the tracking-error harness closes them empirically.

This note does **not** re-derive the 60 s settlement-window averaging — BRTI is an
*instantaneous* index, and the averaging that KXBTC15M settles on is a Kalshi layer on top of
it. That lives in `docs/derivations/settlement-averaging.md`; here BRTI is the per-second print
that averaging consumes. Replica tracking error enters that note's strike `E[A]`/`ln K` as a
first-order term, so getting this stage right is load-bearing for pricing.

The pipeline is one function, `compute_index` in `src/kbtc/index/brti.py`, following the §3.8
pseudocode: `prefilter_books` → `DeviationFilter.apply` → `order_size_cap` → `consolidate(cap)`
→ `uncross` → `dynamic_spacing` → `price_volume_curve` → `utilized_depth` → `index_value`. We
walk the stages in the order they fire.

---

## 1. Consolidation and the order-size cap `C_T`

### 1.1 Mechanics

At each Effective Time `T` the included venues' books are merged into **one consolidated
book**: sizes at the same price are summed across venues, bids sorted descending, asks
ascending (`consolidate` in `consolidate.py`). There is **no explicit per-venue weight** — a
venue's influence is exactly its contribution to the merged depth. Before merging is used
downstream, each *consolidated price level* is capped at `C_T`: a level with aggregate size
above `C_T` enters as exactly `C_T` (`consolidate(books, cap=C_T)`; `min(size, C_T)` per level,
`test_consolidate_caps_each_level_at_C_T`).

### 1.2 Why a cap exists

The index is a depth-weighted average of resting liquidity. Without a cap, a single large
order (real or spoofed) at one price level would dominate the weighting and let one participant
move the published index at will. `C_T` is an anti-manipulation device: it bounds how much any
one price level can contribute, so the index reflects the *shape* of the book, not the largest
single order sitting in it. The cap is deliberately **dynamic** — it scales with the current
book's own size distribution, so it neither clips a genuinely deep market nor lets an outlier
through in a thin one.

### 1.3 The formula (§3.7, Eq 4–5)

`C_T` is computed from the **uncapped** consolidated book (`order_size_cap`), in four steps:

1. **Near-top pool** (`_near_top_sizes`, Eq 4a/4b). Take, per side, every level within ±5% of
   the best price, but **at least the top 50** levels when the book has them:
   `ac_T = max( #{levels within 1.05·best_ask}, min(|asks|,50) )`, symmetrically on the bid.
   Pool those sizes into one sample `S` of length `n`. (The ±5% band / top-50 floor are
   `cap_near_top_pct=0.05`, `cap_min_levels=50` in `params.py`.)
2. **Trim count** `k = ⌊0.01·n⌋` (1% of each tail).
3. **Trimmed mean `s̄`** (Eq 4e): drop the bottom/top `k`, average the rest.
   **Winsorized set `S′`** (Eq 4f): *replace* the bottom/top `k` with the `k`-th / `(n−k−1)`-th
   order statistics (`_winsorize` — `test_winsorize_replaces_tails_not_trims`), from which we
   take the **winsorized sample std `σ`** with `ddof=1` (Eq 4h).
4. **Cap** (Eq 5): `C_T = s̄ + 5σ`.

The literal Eq 5 symbol is the bare/trimmed `s̄` (Eq 4e), so the default is
`cap_mean="trimmed"`. The winsorized-mean reading `s̄′ + 5σ` is a documented alternative and a
real lever — on a right-skewed book the two differ (`test_order_size_cap_o4...`), which is why
it is a tune-on-train / report-OOS knob (**O-4**), not a cosmetic choice. `σ` is the winsorized
sample std under either reading.

> Note (F4): the spec says "uncapped consolidated book" but not whether that is taken
> *before* or *after* cross-venue uncrossing (§1.4). We default to **pre-uncross**
> (`cap_pre_uncross=True`, matching the §3.8 pseudocode); it is a parameter, close it
> empirically.

### 1.4 Worked number — reproducing `C_T = 6.472136`

Take asks of size `[1,2,3]` at `100/101/102` and bids `[1,2,3]` at `100/99/98`. All six levels
are within ±5% of their best, so the pool is all six sizes: `S = [1,1,2,2,3,3]`, `n = 6`,
`k = ⌊0.06⌋ = 0`. With `k = 0` the trimmed set is the whole sample and the winsorized set is
unchanged, so `s̄ = mean = 2.0` and `σ = ` sample std with `ddof=1`:

```
σ² = Σ(xᵢ − 2)² / (n−1) = (1+1+0+0+1+1)/5 = 4/5 = 0.8,   σ = √0.8 = 0.894427
C_T = s̄ + 5σ = 2.0 + 5·0.894427 = 6.472136
```

Verified against `order_size_cap()` (script output below) — matches to the micro:

```
pool=[1, 1, 2, 2, 3, 3]  n=6  k=floor(0.01*n)=0
trimmed mean s-bar = 2.0
winsorized sample std (ddof=1) = 0.894427  (= sqrt(0.8)=0.894427)
C_T = s-bar + 5*sigma = 2.0 + 5*0.894427 = 6.472136
code order_size_cap() = 6.472136  expected 6.472136
```

This is `test_order_size_cap_hand_trimmed_mean_plus_5sigma`. The `5σ` width is why the cap only
bites on genuine outliers: a level must exceed the pool mean by five winsorized standard
deviations before it is clipped.

---

## 2. Uncross — removing crossed / locked books

### 2.1 Mechanics

After capping and merging, the consolidated best bid can sit **at or above** the consolidated
best ask — even when every individual venue's book is internally sane — because a high bid on
one venue and a low ask on another now share one book. BRTI resolves this by **uncrossing**
(`uncross` in `consolidate.py`, §3.2-2a): while `best_bid ≥ best_ask`, consume
`min(size_at_best_bid, size_at_best_ask)` from each side; a level driven to zero is removed, a
partially consumed level keeps its price with the residual size. Stop when `best_bid < best_ask`
or one side is exhausted. (This is distinct from the §5.2.1 *per-venue* crossed-book check in
§5 below, which drops a whole venue that is crossed against *itself*.)

### 2.2 Why

The price-volume curves (§3) assume a normal book: you walk *up* the asks paying more, *down*
the bids receiving less, so `askPV ≥ bidPV` at every depth and the half-spread `midSV` is
non-negative. A crossed consolidated book would give a negative spread and a meaningless
"utilized depth". Uncrossing matches off the overlapping liquidity the way an auction would —
the crossed quantity would trade instantly in reality — leaving a residual book that is
properly one-sided-ordered.

### 2.3 Residual book

Worked in `test_uncross_consumes_min_size_until_separated`: bids `[(101,5),(100,3)]` against
asks `[(100,2),(101,4)]`.

```
best 101 ≥ 100: consume min(5,2)=2  → bid(101,3), ask(100) gone
best 101 ≥ 101: consume min(3,4)=3  → bid(101) gone, ask(101,1)
now best_bid 100 < best_ask 101 → stop
residual: bids [(100,3)], asks [(101,1)]
```

The residual is the book *after* the marketable overlap has been cleared: top-of-book is the
first bid/ask that do not cross, and everything above the clearing level is gone. If one side
is fully consumed the book can end up one-sided — downstream that produces a gap
(`empty_after_uncross`, §5.5), not a bogus print.

---

## 3. Price–volume curves and utilized depth `D = 0.5%`

### 3.1 Building the curves (§3.3, Eq 1a–1f)

On a volume grid `v ∈ {s, 2s, 3s, …}` (spacing `s` from §3.6 below), `price_volume_curve`
(`curves.py`) reads four quantities off the residual book. `_CumCurve.marginal(v)` returns the
**marginal price** at cumulative volume `v` — the price of the level that contains the `v`-th
BTC unit (smallest level index whose cumulative size ≥ `v`):

- `askPV(v)` — marginal ask price to buy cumulative `v` from the consolidated asks;
- `bidPV(v)` — marginal bid price to sell cumulative `v` into the consolidated bids;
- `midPV(v) = (askPV(v) + bidPV(v)) / 2` — the mid price-volume curve;
- `midSV(v) = askPV(v)/midPV(v) − 1` — the fractional half-spread at depth `v`.

Marginal prices stay exact `Decimal`; only the volume axis and the ratio `midSV` are float
(precision policy in `params.py` — no money boundary is crossed in float). `midSV` is
non-decreasing in `v` for a sane book: deeper fills cost more, so the spread around the mid
widens with depth.

### 3.2 Utilized depth (§3.4, Eq 2)

```
v_T = max( largest grid v with midSV(v) ≤ D  (before the first breach),  s )
```

`utilized_depth` (`curves.py`) walks the curve and keeps the last `v` whose half-spread is
still within `D = 0.5%` of mid (`deviation_from_mid=0.005`), floored at one spacing step `s`.
The curve walk itself stops one grid step **after** the first breach, so there is always a
pre-breach point to read (`price_volume_curve`). If even `v = s` already breaches `D`, then
`v_T = s` and the index collapses to the consolidated **best mid** — the thin-book fallback
(§4.2 of the spec; `test_utilized_depth_floors_to_s_when_first_point_breaches`,
`test_compute_index_floor_to_best_mid_hand`).

**Where `D = 0.5%` comes from and why it matters.** `D` *defines* what "utilized depth" means:
it is the deepest you can transact while staying within half a percent of mid. It is the index's
notion of "liquidity that actually matters for price discovery" — anything past the point where
the spread blows beyond 0.5% is deemed too far out to inform the fair price and is excluded from
the weighting. `D = 0.5%` is the BRTI value from §6.2 (VERIFIED). Worked example
(`test_pv_curve_and_utilized_depth_hand`): asks `[(101,2),(102,3)]`, bids `[(100,2),(99,3)]`,
`s = 1`. At `v = 1,2` the mid is `100.5` and `midSV = 0.5/100.5 = 0.4975% ≤ 0.5%`; at `v = 3`
the mid is still `100.5` but `midSV = 1.5/100.5 = 1.4925% > 0.5%` → breach, so
`v_T = max(2, s=1) = 2`.

---

## 4. The exponentially depth-weighted mid, with `λ = 1/(0.3·v_T)`

### 4.1 Mechanics (§3.5, Eq 3)

BRTI is the mid price-volume curve averaged over `v ∈ {s, …, v_T}` with weights that decay
exponentially in depth (`index_value` in `curves.py`):

```
w(v)   = λ · e^(−λ v)
NF     = Σ_{v=s..v_T} w(v)                       # normalization, weights sum to 1
BRTI_T = Σ_{v=s..v_T} midPV(v) · w(v) / NF
```

rounded to the `$0.01` dissemination precision (`CENT`, `ROUND_HALF_UP`). Near-top-of-book mid
prices get the most weight; deeper levels fade out. Only the (transcendental) weights and the
weighted sum are float; the `Decimal` mid prices are converted at the final sum, which is
rounded exactly where the methodology rounds.

### 4.2 Why `exp(−λ·depth)`, and what `v_T` is

The weighting answers "how much should a fill `v` BTC deep inform the fair price?" Top-of-book
is the most informative and should dominate; informativeness should fall off smoothly with
depth, never going negative and never jumping. An exponential kernel `e^{−λv}` is the natural
one-parameter memoryless choice: constant *fractional* decay per unit depth (`d/dv ln w = −λ`),
strictly positive, monotone. `v_T` (§3.2) is the **utilized depth** — the cutoff past which the
spread exceeds `D`, i.e. the maximum depth the index considers at all. So the kernel runs from
top-of-book out to exactly the utilized depth and no further.

### 4.3 Deriving the constant `λ = 1/(0.3·v_T)` — and the correction

`λ` has units of 1/BTC (it multiplies a BTC depth in the exponent), and `v_T` is the only BTC
scale in the problem. So on dimensional grounds `λ` **must** be a pure number over `v_T`:
`λ = c / v_T` for some constant `c`. This makes the weighting **scale-invariant** — double the
book's depth and `v_T` doubles, `λ` halves, and the weight profile `e^{−λv} = e^{−c·(v/v_T)}`
over the normalized depth `v/v_T ∈ (0,1]` is identical. BRTI weights the *shape* of the book,
not its absolute size.

The spec pins `c`: the §6.1 Constants cell is the typeset fraction

```
λ = 1 / (0.3 · v_T)     ⇒   c = 1/0.3 = 3.333…
```

i.e. `lam_factor = 0.3` in `params.py`, `lam = 1.0/(params.lam_factor*v_T)` in `index_value`.
The `0.3` means the exponential reaches one e-fold at 30% of the utilized depth, and decays to

```
e^(−λ·v_T) = e^(−1/0.3) = e^(−3.333…) ≈ 3.57%
```

of the top weight by the time you reach `v_T`. That is a **flat** depth profile — the deepest
utilized level still carries ~3.6% of the top weight.

> **Correction, pinned (O-2).** An earlier reading had `λ = 10.3/v_T`. That was a PDF
> extraction artifact: `pypdf` flattened the typeset fraction `1 / (0.3 v_T)` by running the
> numerator `1` into the denominator `0.3`, producing the string `"10.3 v_T"`. A rendered view
> of the v17.0 PDF §6.1 (human fetch, 2026-10-16) confirms the cell is the fraction, so the
> correct constant is `1/0.3 = 3.333`, **not** `10.3`. The dimensional argument above was right
> about the *form* (`…/v_T`, so `λ·v_T` is constant and the weighting is scale-invariant) but
> the old constant implied a wildly different, almost-top-of-book-only weighting:
> `e^(−10.3) ≈ 3.4×10⁻⁵`, five orders of magnitude smaller at `v_T`. The corrected `0.3`
> fraction is far flatter, so deeper liquidity genuinely moves the replica — this is why the
> constant is load-bearing, not a footnote. The replica uses `0.3`; `lam_factor` is a parameter
> so the (now unlikely) alternative could still be swept.

### 4.4 Decay check — `λ·v_T = 3.333`, weight ≈ 3.57%

From `test_index_value_lambda_decays_to_3p6pct_at_vT`, using the §3.2 worked book
(`v_T = 2.0`), verified against `index_value()`:

```
v_T = 2.0   lam = 1/(0.3*v_T) = 1.666667
lam*v_T = 3.333333   expected 1/0.3 = 3.333333
weight at utilized depth e^(-lam*v_T) = 0.035674  (~3.57%)
(wrong old reading e^(-10.3) = 3.363e-05)
```

Note `λ·v_T = 1/0.3` is **independent of `v_T`** — that is the scale-invariance made numeric.

---

## 5. The deviation filter (§5.3) — 5% → 2.5% hysteresis

### 5.1 Mechanics

Per Effective Time, `DeviationFilter.apply` (`filters.py`) computes each surviving venue's own
mid `(best_bid + best_ask)/2`, takes the **median of venues' mids**, and drops any venue whose
relative deviation from the median exceeds the Potentially-Erroneous-Data parameter:

```
|venue_mid / median_mid − 1| > PED = 5%   ⇒  drop venue this tick (and every tick until reinstated)
```

This runs *after* the §5.1/§5.2 per-venue drops (`prefilter_books`: no-book, unsynced, stale
`>10 s`, empty-side, self-crossed — `test_prefilter_drops_each_erroneous_class`), and its own
drop-all case yields a calculation-failure gap (§5.5).

The median is the robust center: a single rogue venue cannot move a median of three or more, so
measuring each venue against the median rather than the mean is itself an anti-manipulation
choice. `median_base` (**O-9**) selects whether the median is over **all** surviving venues
(literal §5.3 default, `"all"`) or only currently-**active** (non-dropped) ones; after any drop
the two differ and change who reinstates (`test_deviation_filter_median_base_all_vs_active_differ`),
so it too is a tune-on-train / report-OOS lever.

### 5.2 Why hysteresis (§5.3.4)

A venue is reinstated only once its deviation falls back below **half** the parameter:

```
dropped venue reinstated  ⇔  |venue_mid / median_mid − 1| < 2.5%   (= 50% of PED)
```

(`ped=0.05`, `ped_reinstate=0.025`.) The two thresholds differ on purpose. With a single
threshold, a venue hovering right at 5% would flap in and out on every tick — each flip changing
the consolidated book and so the published index, injecting noise that is an artifact of the
filter, not the market. The 5%-out / 2.5%-in band (classic Schmitt-trigger hysteresis) forces a
venue that was excluded to come **convincingly** back inside before it rejoins, so transitions
are rare and deliberate. This makes the filter **stateful across time**: the replica must carry
the per-venue dropped set, not recompute it from scratch each tick — hence `DeviationFilter` is
an object constructed once and threaded through the whole window
(`replica_window_average` passes one instance across the 60 ticks).

Worked (`test_deviation_filter_hysteresis_5pct_drop_2p5pct_reinstate`): four anchor venues at
mid 100 hold the all-venues median near 100 while venue `c` swings.

```
c at 200 → dev 100%  > 5%   ⇒ dropped
c at 104 → dev 4%    in (2.5%,5%)  ⇒ STAYS dropped (not yet convincingly back)
c at 102 → dev 2%    < 2.5%  ⇒ reinstated
```

The 4% tick is the whole point: already inside the 5% drop threshold, but held out until it
clears 2.5%.

---

## 6. The tracking-error harness, and why train/test discipline matters

### 6.1 Methodology

`src/kbtc/index/tracking.py` scores the replica against ground truth. For each settled KXBTC15M
market we reconstruct the replica at 1 Hz over a 60 s window (`replica_window_average`), take the
simple 60-print mean (gaps excluded, `n_gap` recorded), and compare to Kalshi's published value
(`expiration_value` for the close window, `floor_strike` for the open). The signed error is
`replica_avg − published` in dollars (`evaluate_market` → `WindowResult`). Headline stats are
median / p95 / max absolute error (`error_stats`; facts.md 2.2 target **p95 < $5**), with
breakdowns by UTC hour (`by_time_of_day`) and by realized vol (`by_volatility`), plus the worst
markets (`worst_markets`). The constituent coverage (union of contributing venues) is physically
attached to every `ErrorStats` (`venues_used`) so a headline p95 can never be quoted without its
"2/7 constituents" caveat (F4) — with only Bitstamp + Kraken recorded today, the replica is a
subset reconstruction and the error is reported honestly as such.

### 6.2 Why tune on TRAIN, freeze, report on TEST

Three free levers have no primary-source answer and must be *fit* to data: the settlement-window
endpoint `[T−59,T]` vs `[T−60,T−1]` (**O-5**), the cap mean `cap_mean` (**O-4**), and the
deviation-filter median base `median_base` (**O-9**). The temptation is to compute the error for
every setting over *all* days, pick the best, and report that best p95 — but that number is
guaranteed to be optimistic. You have used the same days both to *choose* the setting and to
*grade* it, so the choice has partly fit the noise in those specific days; the p95 you report is
the minimum over several candidates, a biased-low estimator of out-of-sample error. Picking "the
endpoint with the smallest error" and then quoting that endpoint's error is in-sample overfit,
and the gap is largest for exactly the near-the-money, near-expiry windows where replica error
feeds the pricing spread (settlement-averaging.md §3).

The harness enforces the honest protocol in `evaluate_oos`: split the **distinct dates**
chronologically into disjoint train / test sets (`chronological_split`, whole days so train and
test never share a day; `split_by_date` *raises* on any date overlap — a hard anti-leak
guarantee); tune `(config, endpoint)` on the TRAIN windows only (`tune_endpoint_and_config`
picks the lowest median `|error|`); **freeze** that choice; then compute the headline
`error_stats` **only** over the TEST windows of the frozen choice. `OOSReport` records the frozen
config, frozen endpoint, and both date sets, so the number is self-documenting and reproducible.
`summarize`/`best_endpoint` deliberately carry docstrings saying they are *in-sample exploration
only* — the p95 < $5 acceptance number must come from `evaluate_oos`, never from the full set.
This is the acceptance test `test_evaluate_oos_tunes_on_train_reports_on_test_only`: the endpoint
is tuned on day 1, and the day-2 error of the *rejected* endpoint is provably excluded from the
headline.

---

## Check yourself

1. **Why is `λ` of the form `c/v_T` rather than a fixed number, and what did fixing `c = 1/0.3`
   buy us over the old `c = 10.3`?**
   Dimensionally `λ` is 1/BTC and `v_T` is the only BTC scale, so `λ = c/v_T` is forced — which
   makes `λ·v_T = c` constant and the weighting **scale-invariant** (it weights the book's shape,
   not its absolute depth). The correct `c = 1/0.3 = 3.333` decays to `e^(−3.333) ≈ 3.6%` at the
   utilized depth — a flat profile where deep liquidity still matters. The old `c = 10.3` (a
   `pypdf` mis-flatten of the fraction `1/(0.3 v_T)`) gave `e^(−10.3) ≈ 3.4×10⁻⁵`, i.e. an
   almost top-of-book-only index — five orders of magnitude different at `v_T`. The form was
   right; the constant was wrong and load-bearing.

2. **Walk the cap `C_T = 6.472136` from the pool, and say what the `+5σ` is for.**
   Pool `[1,1,2,2,3,3]`, `n = 6`, `k = ⌊0.01·6⌋ = 0`, so trimmed mean `s̄ = 2.0` and winsorized
   sample std `σ = √(4/5) = 0.894427` (ddof = 1). `C_T = s̄ + 5σ = 2 + 5·0.894427 = 6.472136`.
   The cap bounds how much any one consolidated price level contributes, suppressing
   single-order manipulation; the generous `5σ` width means only genuine outliers (five
   winsorized SDs above the pool mean) are clipped.

3. **Why two thresholds (5% out, 2.5% in) in the deviation filter instead of one, and what does
   that force on the implementation?**
   A single threshold makes a venue sitting at the boundary flap in/out every tick, and every
   flip changes the consolidated book and thus the published index — pure filter noise. The
   5%/2.5% hysteresis band forces an excluded venue to come convincingly back inside before it
   rejoins, so transitions are rare. It forces the filter to be **stateful**: carry the
   per-venue dropped set across Effective Times (`DeviationFilter` is one object threaded through
   the window), not recomputed per tick.

4. **Why can't we report the p95 of the best-fitting window endpoint over all days?**
   Because choosing the endpoint and grading it on the same days fits the noise in those days:
   the reported p95 is a minimum over candidates, a biased-low estimate of true accuracy
   (in-sample overfit). `evaluate_oos` tunes `(endpoint, config)` on a chronological TRAIN date
   split, freezes it, and reports `error_stats` only on the disjoint TEST split — with a hard
   raise on any date overlap — so the headline p95 < $5 is genuinely out-of-sample.
