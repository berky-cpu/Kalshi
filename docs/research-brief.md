# Research brief (October 2026)

Condensed from a literature review. Most 2025–26 prediction-market papers are unrefereed
preprints; treat magnitudes as provisional.

## Where the edge is (and isn't)

- **Makers beat takers.** Kalshi (Bürgi, Deng & Whelan, CEPR DP20631) and Polymarket
  (Akey, Grégoire, Harvie & Martineau, CEPR DP21615): profits flow from liquidity takers to
  liquidity providers; longshot buyers lose heavily.
- **Crypto-price markets are among the most efficient categories** — edge must come from a
  better fair value, not from harvesting bias.
- **Taker/latency arbitrage vs. CEX: null result.** Young, *OpenMarket* (arXiv 2607.26245):
  a 43-feature model on Binance flow did not beat Polymarket's own mid out of sample; lost
  after fees. Quotes reacted to big Binance moves in ~347 ms median.
- **Settlement-window manipulation** on 5-minute contracts (Dai, Jia & Yu, arXiv 2606.31675);
  largely absent at 15 minutes. Still: be careful in the final seconds near the strike.
- **Option-implied vs. prediction-market gaps** on longer-dated BTC threshold contracts
  (arXiv 2606.19517): 5–11 pp. Small sample. A possible second strategy later.

## Signals worth building

- Order-flow imbalance (Cont, Kukanov & Stoikov 2014), multi-level OFI (Xu, Gould & Howison 2019),
  deep OFI (Kolm, Turiel & Westray 2023).
- Trade-flow imbalance beats OFI in thin crypto books (Silantyev 2019).
- Feature engineering > architecture on crypto LOBs (Wang, arXiv 2506.05764);
  DeepLOB results ignore costs (Briola et al. 2025 temper them).
- L3 cancellation/spoofing filters (Fabre & Challet, arXiv 2504.15908; arXiv 2507.22712).
- Perps/futures lead spot (Alexander et al. 2020; Plazuelo Pascual et al., arXiv 2506.08718).

## Quoting

- Avellaneda & Stoikov (2008): inventory-skewed reservation price + spread from risk
  aversion and fill intensity. Must be adapted to binary payoff (ATM-peaked delta).

## Fees (verify in Phase 0)

- Kalshi taker: `ceil_to_cent(0.07 · C · P · (1−P))`; maker: 0 by default, `0.0175 · C · P · (1−P)`
  on flagged series. Retail crypto spot fees (40–80 bps/side) make direct crypto trading
  on these signals uneconomic — the crypto data is an *input*, not a venue.

## Data

- Free live: Coinbase level3/full (auth), Kraken v2 level3 (token), Bitstamp live_orders,
  Binance/Bybit L2+trades, Kalshi WS (`orderbook_delta`, `trade`).
- Kalshi has no bulk historical order-book archive → our recordings are the backtest dataset.
- Historical crypto: Tardis.dev (ask about academic pricing). Open prediction-market data:
  Becker's Kalshi/Polymarket dataset, Akey et al. on Hugging Face, OpenMarket corpus.
