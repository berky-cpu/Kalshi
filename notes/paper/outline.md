# Working-paper outline (draft) — DO adverse-selection premia persist in prediction markets?

**Status (UPDATED 2026-10-29): the decay headline is DEAD — pivot fired.** The hardening run found the
Kalshi "Mentions" universe is **2026-only** (launched ~Jul 2026), so the multi-year time axis the
decay headline needs does not exist in accessible data; the frictionless premium is **flat** over the
observable Q3→Q4 2026; and cross-series variation (−10.5¢…+17.6¢) dwarfs any time change — **selection
dominates, not a time trend.** Bartlett–O'Hara's pre-2026 sample is therefore a *different* universe,
making the comparison cross-sectional, not temporal. **Section 4 (decay) is retired.** Per the
pre-committed pivot, the lead becomes the **queue-aware frictions methodology (Section 5)**: a
replication-then-refutation of the harvestability of the +1.91¢. This is a MODEST methods/practitioner
note, not a novel empirical paper. Awaiting Berk's decision on whether to write it (short note / blog)
or shelve. Decay-run detail: see backtest-engineer handback + `notes/_candidate2/result_byperiod.txt`.

**Working title:** *Fading Edges: Decay of the NO-Side Adverse-Selection Premium in Kalshi
Single-Name Prediction Markets* (alt: "Do Adverse-Selection Premia Persist? …").

**Realistic venue:** SSRN / arXiv q-fin working paper, or a short empirical note. NOT a top journal
(much of the microstructure is confirmatory; the novelty is the decay + the queue-aware methodology).

**One-line contribution:** we show that a documented, very recent (2026) retail-maker edge on Kalshi
single-name markets has *decayed as the market scaled*, and we demonstrate — with a queue-aware,
latency-aware backtest — that the residual edge was never harvestable once realistic fills are
modeled. Two linked messages: premia decay with market maturation; and frictionless markouts
systematically overstate retail passive edge.

---

## Abstract (to write last)
Prediction-market volume on Kalshi grew ~100× over 2024–2026. Bartlett & O'Hara (2026) document a
NO-side adverse-selection premium (~+1.91¢/contract) on single-name "mention" markets driven by
takers over-buying YES. We ask whether that premium persists. Using the full public trade/settlement
history across [N] single-name series, we [replicate the premium on the 2021–2025 era and] show the
YES-settlement rate rose from ~31% to ~[41–46]% into 2026 while takers continued to over-buy YES,
eroding the NO-side premium to [X]. We then show, with a back-of-queue, latency-aware simulator, that
even the pre-decay premium was not harvestable by a retail maker: realistic fill capture is ~7–8% and
concentrated in adversely-selected sweeps, turning a positive frictionless markout negative. [Headline
numbers pending the hardening run.]

## 1. Introduction
- Prediction markets as a maturing asset class; retail participation; the open question of whether
  documented inefficiencies/premia survive scale.
- Bartlett & O'Hara (2026): the NO-side premium. Our two questions: (Q1) does it persist? (Q2) was it
  ever harvestable net of realistic frictions?
- Contributions: (i) first evidence of *temporal decay* of a prediction-market adverse-selection
  premium as volume scaled; (ii) a queue-aware/latency-aware methodology showing frictionless markouts
  overstate retail passive edge, applied to both single-name markets and 15-min crypto binaries.

## 2. Related work
- Bartlett & O'Hara (2026) — the base result we extend. Be explicit we REPLICATE then EXTEND; no
  priority claim on the base premium.
- Bürgi, Deng & Whelan (2026) — favorite-longshot on Kalshi (corroborates behavioral YES-bias).
- Microstructure: Glosten–Milgrom (adverse selection sets the spread); back-of-queue fill theory;
  Budish–Cramton–Shim (speed/sniping) for the "slow side" argument.
- Prediction-market efficiency (incl. weather-beats-forecast, Fed-on-Kalshi-macro) — context that
  these markets are already quite efficient, which motivates Q2.

## 3. Data
- Kalshi public REST (markets, trade tape with disclosed taker side, settlement `result`). Sample:
  single-name/mention series, [span 2021–2026], [N series / M markets / K trades]. Free, reproducible.
- Definitions: NO-side resting; held-to-settlement P&L = payoff − entry; maker-free (per-series
  `fee_type` verified). Event = speech/earnings-call = independent bootstrap unit.
- **Licensing note:** the 15-min crypto case study (Section 5) uses a relayed CF BRTI feed under
  internal-use terms — we report only aggregate statistics, never redistribute the feed. (Confirm
  before submission.)

## 4. The NO-side premium and its decay  ← HEADLINE (pending hardening)
- Replicate Bartlett–O'Hara on the pre-2026 era (target ≈ +1.9¢; validates method).
- The two time series whose gap is the premium: YES-settlement rate vs taker-YES-volume share,
  by quarter/half-year. Takers keep over-buying YES (~66%) but markets settle YES increasingly often
  (~31% → ~41–46%) → the premium compresses.
- Frictionless NO-side P&L by period, with event-block bootstrap CIs; the decay trend.
- **Time vs selection:** within-series trends (series spanning multiple years) + fixed-universe
  comparison, to argue the decay is a time/efficiency effect, not series-mix. [This is the load-
  bearing robustness the hardening run must deliver; if it fails, re-frame.]

## 5. Why the premium was never harvestable: a queue-aware backtest  ← SUPPORTING methodology
- The gap between a *frictionless* markout (rest everywhere, fill everything, no pick-offs) and a
  *realistic* one (back-of-queue, latency, fill only what sweeps past you — the adverse subset).
- Single-name: frictionless +1.95¢ → honest single-entry −3.2¢; ~7.6%-analogue capture;
  P&L concentrated in top-5% of markets (fat-tailed, not dependable).
- 15-min crypto binaries (KXBTC15M) as a second, cleaner instance: well-calibrated free-data fair
  value *ties* the exchange mid; frictionless passive capture looks positive; back-of-queue + 100ms
  latency → −9¢/fill, every day negative, 7.6% capture. The structural "slow side" point: pricing off
  the published CF index (which lags constituents ~2s) is adverse near ATM/expiry by binary delta.
- Methodological takeaway: retail passive-edge studies that stop at frictionless markouts overstate;
  queue position + latency are decisive and must be modeled.

## 6. Discussion
- Market efficiency increases with scale/volume — a concrete, dated example of a premium decaying.
- Implications for retail makers on event markets: no rebate + tick-wide books + being the slow side
  ⇒ no passive edge; the apparent edges are frictionless artifacts or fat-tailed bets.
- What WOULD create an edge (and didn't exist for us): a genuine speed/lead feed, a maker rebate,
  structurally wider-spread contracts.

## 7. Limitations
- No live/paper-trade validation (backtest only); queue_ahead assumed (swept) for the REST-only
  single-name data; CF-feed licensing constrains crypto-side disclosure; single-operator simulator;
  decay result's time-vs-selection identification is [as strong as the hardening run allows].

## 8. Conclusion
- Premia in these markets are not static; a 2026 retail-maker edge had already decayed, and was not
  net-harvestable even before it did. Both points matter for anyone modeling prediction-market
  microstructure or expecting a durable retail edge.

## Appendix
- The queue-aware simulator (back-of-queue, latency, exact fees; `src/kbtc/backtest/`); the honest
  vs frictionless fill models; event-block bootstrap; fee handling; reproducibility (public REST +
  committed code; commit hashes). Point to EXP-007/008/009/010 and ADR-003/004 as the audit trail.

---

### Open decisions for Berk
- Authorship / whether to post publicly at all (vs. keep as an internal writeup).
- Venue: SSRN vs arXiv q-fin vs a practitioner Substack (lower bar, faster, still citable).
- Whether to invest in a short Phase-5 demo paper-trade for live validation (would materially
  strengthen Section 5; costs a little infra again — your call, not required for a working paper).
- Confirm CF-feed aggregate-statistics disclosure is within the internal-use terms before anything
  goes public.
