# Passive market making in Kalshi's 15-minute Bitcoin contracts: a negative result

Berk Yilmaz · UC Berkeley · October 2026 · code at github.com/berky-cpu/Kalshi

## 1. Question

Kalshi's KXBTC15M series lists a new binary contract every 15 minutes: YES pays $1 if Bitcoin's
settlement price at the close is at least its reference price at the open. Both prices are 60-second
averages of the CF Benchmarks Bitcoin Real-Time Index (BRTI), published once per second from seven
spot exchanges. Makers pay no fee on this series; takers pay round-up(0.07·C·P·(1−P)) per order.

Published work on Kalshi and Polymarket finds that liquidity providers profit at takers' expense on
average. This project asked a narrower question: **can a small maker, using only free public
data, earn the spread on KXBTC15M by quoting around a model fair value?** The hypothesis had two
parts: (i) a fair value that correctly models the 60-second settlement averaging would be more
accurate than the market's, especially in the final minutes; (ii) posting resting quotes around it
would capture more spread than it lost to adverse selection.

## 2. Data

All data was recorded by this repo's own WebSocket clients, stamped with receive time, on two
Hetzner servers (US-East for Kalshi and the index constituents; EU for Binance and Bybit, which
block US addresses), October 16–29, 2026.

- **Kalshi:** full-depth order-book deltas and trades for every KXBTC15M market (≈485 messages/s,
  zero sequence gaps after the roll-gap fix), settlement values (`floor_strike`, `expiration_value`,
  `result`), and Kalshi's relay of the official BRTI (`cfbenchmarks_value`, ≈1 Hz, with its
  60-second settlement averages).
- **BRTI constituents:** Coinbase (L2), Kraken (L3), Bitstamp (L3); Gemini, Crypto.com and Bullish
  were added late. LMAX was not recorded.
- **Perpetual futures:** Binance and Bybit L2 and trades, as a potential leading signal.

Raw messages are stored as hourly zstd-compressed JSONL, normalized to parquet with checksum-verified
book reconstruction (Kraken: 284,171 of 284,171 CRC32 checkpoints matched on a full day). The
recordings are archived privately in Backblaze B2 (≈2 weeks); Kalshi publishes no historical order
books, so they cannot be re-downloaded, only re-recorded.

## 3. Methods

**Contract facts.** Every rule the code depends on (settlement source, 60-print window, ≥ tie rule,
tick structure, fee schedule, BRTI constituents and aggregation constants) was verified against
primary sources and logged with URL and date in `docs/facts.md` before any code relied on it.

**Index replica (Phase 2).** A full implementation of the CF BRTI methodology v17.0: consolidate
constituent books, cap each level at trimmed-mean + 5σ, uncross, build price–volume curves, take
utilized depth at a 0.5% half-spread, and average the mid curve with weights λe^(−λv),
λ = 1/(0.3·v_T), with the 5%/2.5% hysteresis deviation filter. Scored out of sample against
Kalshi's published settlement values after tuning free levers on earlier days only.

**Fair value (Phase 3).** Log index as a driftless random walk. The settlement average A of 60
one-second prints has exact discrete variance Var[A] = (σ²/n²) Σ_k Σ_l min(u_k, u_l), which equals
σ²(τ − 40 s) before the window and σ²τ³/(3w²) inside it. P(YES) = Φ((E[A] − ln K)/√Var[A]). σ from
a microstructure-robust EWMA of index returns with intraday seasonality, validated by QLIKE out of
sample. Evaluated by paired Brier score against Kalshi's contemporaneous mid, walk-forward by
calendar day, with day-block bootstrap confidence intervals.

**Backtest (Phase 4).** Event-driven replay of the recorded Kalshi book. Orders join the back of the
queue at their price; the queue ahead drains only through observed trades and conservative cancels;
100 ms submit/cancel latency; exact fees; settlement from the authoritative result. The pre-registered
policy was rest-one-lot-at-touch on both sides, pulled when τ < 180 s and |z| < 1. Metric:
net markout per fill at 5 s, 30 s and settlement.

**Review.** Each result had to pass an adversarial review (look-ahead, fees, fill realism, statistics,
unverified facts) before it counted. Thirty-seven such reviews are in `notes/reviews/`.

## 4. Results

| Gate | Result | Note |
|---|---|---|
| Index replica accuracy (EXP-002) | p95 error $7.02 vs. $5 target, median $2.69, bias −$1.29 (n=80 windows, 3 of 7 constituents) | Missed the target; made moot when Kalshi's relay of the official feed was found and recorded |
| Replica vs. published index (EXP-005) | Replica **leads** the published BRTI by ≈2 s (k≈2.2 train, 1.9 test) | The published index is stale relative to the constituent books it is built from |
| Volatility estimator (EXP-006) | QLIKE ratio model/trailing ≈ 0.93 at the frozen configuration | Small, configuration-sensitive gain |
| Fair value vs. Kalshi mid (EXP-007) | Paired Brier diff **+0.0029**, 95% CI [≈0, +0.0058]; log-loss diff +0.0083 | Well calibrated; ties-to-slightly-worse than the mid. Shortfall concentrated at the money in the last 1–3 min |
| Spread cushion (P0a/P0b) | Book quotes at 1¢, often 0.1¢; fair-value RMS error 3–16¢; where model and mid disagree by >3¢ the model is worse | No regime in which a stand-alone fair value is rewarded |
| Maker economics, back of queue (EXP-009) | Net markout **−9.06¢/fill at 5 s**, CI [−11.21, −6.55]; −9.25¢ at 30 s; −18.4¢ at settlement. All 6 days negative; 82% of fills ≤ 0; capture 7.6% of frictionless fills | The decisive negative |
| Maker economics, frictionless front of queue | −1.34¢ mean (median −0.05¢), driven by 2 of 42 markets; quiet markets +0.05 to +0.33¢ | Best-case ceiling ≈ 0 ± 0.1¢; its sign is unresolved and was not pursued |

**Mechanism.** Three facts combine. (1) The maker prices off the published BRTI, which trails its
own constituents by about 2 seconds; takers who watch Coinbase, Kraken or the perpetuals directly
know where the index is going before the maker's quote updates. (2) Kalshi's maker fee is $0 for
everyone, so there is no rebate to fund a negative markout. (3) Jumping the queue requires improving
the touch by 0.1¢, which is the whole plausible edge. A maker on this contract is therefore the slow
side of an informed flow, and the spread it captures (≈0.5¢) does not cover the adverse selection
(≈9¢).

**Alternatives tested and rejected (EXP-010, facts.md).** Kalshi's liquidity-incentive program pays
about $14/day per market, is pro-rata among many makers, and covers no crypto series. Resting NO
orders on single-name "mention" markets reproduces the published frictionless premium (+1.95¢ vs.
+1.91¢ in Bartlett & O'Hara) but turns to −3.2¢ per contract under back-of-queue fills, with the
profit concentrated in the top 5% of markets.

## 5. Conclusion

**A maker with public-feed information has no viable passive edge on KXBTC15M.** The empirical claim,
which passed review, is that the realistic naive passive policy loses about 9¢ per fill with every
day negative. The stronger claim, that no selective policy could work, is argued from the market's
structure (sub-cent tick, zero-fee symmetric book, stale reference index) rather than measured, and
is recorded as such in ADR-004.

The thesis would reopen only with a genuine sub-second lead feed from the constituent exchanges or
perpetuals, a maker rebate or fee change, or a contract with a structurally wider spread.

## 6. What is reusable

The recorders, normalizer, BRTI replica, settlement-averaging pricer and queue-aware backtester are
general. The ≈2-second lag between the published settlement index and its constituents is itself a
measurable phenomenon in cash-settled binaries, and the recorded Kalshi order books are a rare
dataset for studying it.

## 7. Replication

1. `uv sync && uv run pytest -q` (565 tests).
2. Deploy the recorders per `deploy/README.md` with a read-only Kalshi API key; record for at least
   seven full days so day-block bootstraps have enough blocks.
3. `python -m kbtc.normalize --root data/raw --out data/clean`.
4. Run the experiments in order; each note in `notes/experiments/` states its script, configuration,
   train/test split and the protocol that was fixed before results were seen.
5. Compare against the tables above. Differences in the index-lag estimate and the markout magnitude
   are expected across periods; the sign of EXP-009 is the result to check.

## References

- Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book," *Quantitative Finance*.
- Bartlett & O'Hara (2026), "Adverse Selection in Prediction Markets: Evidence from Kalshi," SSRN 6615739.
- Bürgi, Deng & Whelan (2026), "Makers and Takers: The Economics of the Kalshi Prediction Market," CEPR DP20631.
- CF Benchmarks, *CME CF Cryptocurrency Real Time Indices Methodology Guide*, v17.0.
- Cont, Kukanov & Stoikov (2014), "The price impact of order book events," *J. Financial Econometrics*.
- Dai, Jia & Yu (2026), "Settlement Manipulation in Prediction Markets," arXiv 2606.31675.
- Kalshi, KXBTC15M contract terms and fee schedule (see `docs/facts.md` for URLs and dates).
- Patton (2011), "Volatility forecast comparison using imperfect volatility proxies," *J. Econometrics*.
- Young (2026), "OpenMarket: A Synchronized Polymarket–Binance Dataset," arXiv 2607.26245.
