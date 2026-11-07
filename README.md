# kbtc-mm — Is there a passive maker edge in Kalshi's 15-minute Bitcoin contracts?

A market-microstructure research project on **Kalshi KXBTC15M**, the 15-minute Bitcoin up/down
binary. The question: can a market maker with free public data earn the spread by quoting around a
model fair value? The answer, after six weeks of recording, modelling and backtesting, is **no**.
The full write-up with numbers is in [`docs/REPORT.md`](docs/REPORT.md).

**Author:** Berk Yilmaz (UC Berkeley). October 2026. Paper trading only; no live orders were ever sent.

## Result in one paragraph

KXBTC15M settles on a 60-second average of the CF Benchmarks BRTI index. A settlement-averaging
digital-option fair value, priced off the published index feed, is well calibrated but only **ties**
Kalshi's own mid out of sample (paired Brier difference +0.0029, CI touching zero). The book quotes
at the 1¢ / 0.1¢ tick, so a fair-value error of 3–16¢ is an order of magnitude wider than the spread.
In a queue-aware, latency-aware backtest, resting at the touch nets **−9.06¢ per fill at 5 s**
(day-block bootstrap CI [−11.2, −6.6]), with every day negative and only 7.6% of frictionless fills
actually captured. The mechanism is structural: the published index lags its constituent exchanges by
about 2 seconds, takers watch the constituents directly, the maker fee is zero for everyone (no
rebate), and queue priority costs the entire best-case edge.

## What is in the repo

| Path | What it is |
|---|---|
| `docs/REPORT.md` | Research write-up: question, data, methods, results, what would reopen it |
| `docs/facts.md` | Every contract, fee, index and feed fact the code relies on, with source URL and date |
| `docs/brti-spec.md`, `docs/feeds.md` | Implementable spec of the CF BRTI methodology; venue feed contracts |
| `docs/derivations/` | Math notes: settlement-averaging variance, BRTI replica, fee curve |
| `docs/decisions/` | ADR-001 … ADR-004, the decisions that shaped and closed the project |
| `notes/experiments/` | EXP-001 … EXP-010, each with a pre-registered protocol and the result |
| `notes/reviews/` | REV-001 … REV-037, the adversarial reviews every result had to pass |
| `src/kbtc/recorders/` | 24/7 WebSocket recorders: Kalshi (book, trades, settlements, CF index feed), Coinbase, Kraken, Bitstamp, Gemini, Crypto.com, Bullish, Binance and Bybit perps; gap detection and resync |
| `src/kbtc/normalize/` | Raw messages → parquet books/trades, with checksum-verified book reconstruction |
| `src/kbtc/index/` | Replica of the CF BRTI methodology (consolidate, cap, uncross, PV curves, utilized depth, exponential weighting, deviation filter) and a tracking-error harness |
| `src/kbtc/pricing.py` | Asian-digital fair value with the exact discrete settlement-window variance |
| `src/kbtc/quoting.py` | Avellaneda–Stoikov quoting adapted to a binary's sensitivity |
| `src/kbtc/backtest/` | Event-driven simulator: back-of-queue fills, latency, exact fees, markouts |
| `src/kbtc/fees.py` | Kalshi's quadratic fee schedule, verified against the published table |
| `deploy/` | systemd units, health report, backups and runbook for the recording servers |
| `tests/` | 565 tests |

## Reproducing

Kalshi publishes no historical order books, so the raw recordings are the scarce input. They are
archived privately; anyone can re-record with the code here.

```bash
git clone https://github.com/berky-cpu/Kalshi.git && cd Kalshi
uv sync && uv run pytest -q                      # 565 passed

# 1. Record (any Linux box; see deploy/README.md for the 24/7 setup)
cp deploy/env.example /etc/kbtc/env              # read-only Kalshi key + exchange keys
python -m kbtc.recorders.run kalshi              # one process per feed; see run.py for names

# 2. Normalize
python -m kbtc.normalize --root data/raw --out data/clean

# 3. Analyses — each EXP note lists its exact script, config and split
#    EXP-002  index replica tracking error        (src/kbtc/index/tracking.py)
#    EXP-007  fair value vs Kalshi mid, Brier     (notes/experiments/EXP-007-*.md)
#    EXP-009  queue-aware maker gate              (scripts/experiments/exp009_gate_run.py)
```

Every experiment fixed its evaluation protocol before looking at results, split train/test by
calendar day, and used only data timestamped at receipt. Contract and fee facts were checked against
primary sources before any code depended on them.

## How it was built

The code was developed with a Claude Code multi-agent workflow that I designed and directed: a lead
agent, specialist agents for data, index replication, modelling and backtesting, and a read-only
red-team reviewer whose sign-off was required before any result counted. The configuration is in
`.claude/` (agents, slash commands, and a hook that blocks any order to a production endpoint).
I set the research question and gates, verified the primary sources, made the decisions recorded
in `docs/decisions/`, and chose when to stop.

## Safety

`kbtc.execution.guard` rejects any order endpoint other than Kalshi's demo environment, and the
`.claude/hooks` guard blocks order-like calls to production hosts and reads of secrets. The
recorders are read-only market-data clients.

## License and data terms

Code: MIT. Recorded exchange and Kalshi data are subject to each venue's API terms and are not
redistributed. The CF Benchmarks index values relayed by Kalshi were used for internal research only.
