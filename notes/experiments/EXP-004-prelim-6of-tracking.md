# EXP-004 (PRELIMINARY / DIRECTIONAL): multi-constituent BRTI tracking + venue ablation

> **STATUS: PRELIMINARY — NOT CREDIBLE, NOT COUNTING, NOT A ROADMAP TICK.**
> Single (~1-day) held-out TEST → a **point estimate**, not a credible p95. It is **confounded**
> (Bitstamp — a heavy USD constituent — had to be EXCLUDED, and the one held-out day is atypical), so
> it **does NOT overturn EXP-002**. The credible run is day-7 with ≥3–5 independent held-out days and
> Bitstamp fixed. For light red-team only.

- **Date / author (agent):** 2026-10-24, index-replicator. Code: installed `/opt/kbtc/src` @ HEAD
  `7277c5a` (pulled origin/main: Bitstamp reseed fix, PriceLevelBook perf fix, Gemini/Crypto.com/Bullish
  adapters, Crypto.com depth-50 trim, WS keepalive/watchdog). `import kbtc` confirmed resolving to
  `/opt/kbtc/src` @ 7277c5a before running.
- **Goal (directional):** with all recorded spot constituents, (a) does adding venues move held-out p95
  toward $5, (b) is the multi-constituent machinery sound, (c) sanity. Same frozen methodology as
  EXP-002 (`tracking.evaluate_oos`: tune config×endpoint on TRAIN only, FREEZE, report on disjoint
  TEST; `split_by_date` raises on overlap; per window_type).
- **Data:** re-normalized fresh into `/opt/kbtc/data/clean_prelim6` (bitstamp live_orders_btcusd,
  kraken level3, coinbase_advanced level2, gemini l2_btcusd, cryptocom book_btcusd, bullish l2_btcusd)
  + Kalshi settlement ground truth. Window: markets settling **≥ 2026-10-21 18:00 UTC** (safe margin
  after all 6 live+stable) through latest. In scope: **198** KXBTC15M (10-21: 24 post-cutoff, 10-22:
  87 — reflects the ~2 h Kalshi market-closed gap ~07:00–09:00 UTC, 10-23: 87 through 21:30 UTC).
- **Walk-forward split:** **TRAIN = {2026-10-21 PM, 2026-10-22}**, **held-out TEST = {2026-10-23}**
  (latest day). Only ~2–2.5 days exist → low independent-day power → **point estimate**. n per subset
  below.

## Bitstamp EXCLUSION (the headline data-quality finding)

Bitstamp was **dropped from the index and the ablation**: it is **self-crossed in ~76% of in-window
ticks** (usable only **23.5%**; `self_crossed=9072` of 11,880 close-window ticks). This is **not** the
residual ~13% reseed-resurrection crossing from EXP-002 — it is a **gross book inversion**: measured
median `best_bid − best_ask ≈ +$600 to +$757` (i.e. best bid sits **hundreds of $ above** best ask;
coordinator reports phantom orders ~$1e9 off-touch). Onset probe (synced snaps, best_bid≥best_ask):

| window | crossed % | note |
|---|---|---|
| v2 10-20 20:00 (EXP-002 clean) | 2.9% | healthy, spread +$0.01 |
| p6 10-21 20:00 | 3.0% | healthy, spread +$0.01 |
| p6 10-21 22:00 / 23:00 | 12.7% / **100%** | onset ~10-21 23:00 UTC |
| p6 10-22 (00/01/02/04/08/12h) | 18.6 / 6.1 / **100** / 5.3 / 5.1 / **100** % | **intermittent** 100%-inverted windows |
| p6 10-22 20:00, 10-23 20:00 | **100% / 100%** | inverted by ~−$600 / −$757 |

Healthy through ~10-21 22:00, then **intermittent 100%-inverted** windows (worsening around the
10-22 ~08:00 fix-restart) through 10-23. **This is a raw-feed/reconstruction incident, NOT my
normalize** — it reproduces in EXP-002's full-history `clean_22study_v2` normalize, and my 10-21
staging output is healthy (3.0%). It likely affects the **live feed**; a separate **data-engineer is
root-causing it — cross-reference that ticket**. Consequence here: a heavy USD constituent is missing,
which **confounds** every number below.

## Gemini coverage anomaly (asked) — RESOLVED, was a normalize artifact

Gemini first normalized to only the 10-21 partition (10-22/10-23 emitted 0 rows). Root cause: Gemini's
L2 **baseline snapshot is sent once at connection start (10-21)**, so a standalone **per-date** pass of
a later day never achieves `synced` → emits nothing. Re-normalizing Gemini **continuously** (so the
10-21 baseline propagates) fixed it: Gemini is now **100% synced on all 3 days** (10-22: 5.06M snaps,
10-23: 4.77M), **usable 99.9%**, present 198/198 windows. So Gemini is **fully covered**, not a 1-day
venue; its ablation marginal is a genuine (small) measurement. (Same per-date artifact had also left
**Bitstamp 10-23 at 0% synced** — also fixed by continuous normalize before the run; the crossing issue
above is separate and unfixable here.)

## Ablation A — per-subset OWN full-coverage (held-out TEST = 2026-10-23)

Frozen on TRAIN for every subset: config `(trimmed, all)`, endpoint `T-60..T-1`. 2-of base is
`{kraken, coinbase}` (Bitstamp unavailable, so the EXP-001/002 `{bitstamp,kraken}` base is not
reproducible this window).

| subset | TRAIN med/p95 (in-samp) | **TEST med** | **TEST p95** | TEST max | TEST bias | n_test |
|---|---|---|---|---|---|---|
| 2of {kraken,coinbase} | 3.165 / 9.017 | **4.570** | **11.931** | 14.590 | −4.193 | 72 |
| 3of +gemini | 3.695 / 13.148 | 4.525 | 11.715 | 13.840 | −4.093 | 72 |
| 4of +cryptocom | 3.420 / 11.838 | 3.330 | 10.952 | 13.360 | −2.959 | 69 |
| 5of +bullish | 3.340 / 11.624 | **3.300** | **10.924** | 13.330 | −2.944 | 69 |

All subsets **MISS p95 < $5** (best p95 = $10.92). Adding venues **monotonically** lowers held-out
median ($4.57 → $3.30) and p95 ($11.93 → $10.92) — directionally "more venues help" — but the error is
**dominated by a large structural negative bias**, not dispersion.

## Ablation B — COMMON-SUPPORT (same fixed 69 windows, 5-of full-cov) + marginal deltas

| subset | TEST med (Δ) | TEST p95 (Δ) | bias |
|---|---|---|---|
| 2of {kraken,coinbase} | 4.560 (baseline) | 11.974 (baseline) | −4.109 |
| 3of +gemini | 4.500 (−0.060) | 11.746 (−0.228) | −4.034 |
| 4of +cryptocom | 3.330 (−1.170) | 10.952 (−0.794) | −2.959 |
| 5of +bullish | 3.300 (−0.030) | 10.924 (−0.028) | −2.944 |

On this single day the marginal gain is almost entirely from **Crypto.com** (−$1.17 median, −$0.79
p95); **Gemini and Bullish are marginal** (≤$0.06). Do not over-read: n=69, one atypical day.

## Structural negative bias (the dominant error)

Bias is **−$2.5 to −$4.2** across subsets — the replica sits systematically **below** published, and
|bias| ≈ median (nearly all errors negative; see `notes/figures/EXP-004/exp004_5of_close.png`). This is
**much larger than EXP-002's −$1.29** (3-of **with** Bitstamp). Removing Bitstamp — a heavy USD venue
the real BRTI includes — plausibly drives most of the extra downward bias; the rest is the atypical
10-23 tape (a few large −$25…−$33 outliers at ~11:00 UTC / high vol). Worst at low-vol quintile 0
(bias −$5.55) and US-hours 07–15. O-4/O-9 grid again **empirically inert** (all 4 configs byte-identical
to ~$0.001; only endpoint O-5 moved, froze T-60..T-1) — consistent with EXP-002.

## Sanity (all pass)

- **5-of replica range $81,002.56–$84,775.71** vs published **$81,008.07–$84,776.32** — tracks (the
  ~$3 bias is within-window, not a range error; BTC rallied to ~$84.7k, above EXP-002's $81–83k).
- **60×1 s averaging invariant:** 0 windows with `n_used+n_gap ≠ 60`.
- **Per-venue usable % (close T-59..T, in-window):** bitstamp **23.5% (BROKEN)**, kraken 82.5%,
  coinbase 88.9%, gemini 99.9%, cryptocom 83.8%, bullish 98.8%. Non-crossing drops are
  `no_book`/`unsynced`/`stale` (venue gaps/resyncs), NOT crossing.
- **Crypto.com:** **no `self_crossed` drops** → the depth-50 ghost-trim (REV-029 F1) is clean.
- **Kraken CRC:** 10-22 = 114 mismatch / 4.51M (0.003%), 10-23 = 227 / 4.34M (0.005%), all
  med/p95/max deviation = 0 → reconstruction exact; 10-21's 0.81% is per-date-normalize warmup resync.
- **5-of full-cov windows/day:** 10-21 24/24, 10-22 68/87, 10-23 69–72/87 (good TEST coverage).

## Interpretation

Directionally: the multi-constituent machinery is sound (CRC-clean Kraken, exact 60×1 s invariant,
replica tracks the published range, Crypto.com trim clean, Gemini fully covered) and **adding venues
monotonically helps**. BUT this run is **CONFOUNDED** — Bitstamp (a heavy USD constituent) is
**excluded** due to a live-feed inversion incident, and the single held-out day (Oct 23) is atypical
(BTC rally, large tails). The absolute numbers (p95 ~$11, bias ~−$3) are **worse than EXP-002's 3-of
($2.69 / $7.02 / −$1.29) but NOT comparable** and **do NOT overturn EXP-002**. **The real read needs
(1) Bitstamp fixed (data-engineer ticket), (2) ≥3–5 independent held-out days.** Until then this is a
plumbing/coverage check, not an accuracy verdict.

## Red-team / ROADMAP
Light red-team only. **Does not count; ROADMAP not ticked.** Figures:
`notes/figures/EXP-004/exp004_5of_close.png`. Scripts (uncommitted): `notes/_exp004_scripts/`.
