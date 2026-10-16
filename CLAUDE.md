# kbtc-mm — Kalshi BTC 15-minute market making

You are working on Berk's research project: a **maker-only, model-priced market-making
system for Kalshi's KXBTC15M (15-minute BTC up/down) contracts**, priced from free
order-level crypto data (Coinbase / Kraken / Bitstamp L3, Binance/Bybit perp L2+trades).

Read this file, then `ROADMAP.md`, then `docs/research-brief.md` before starting any task.

## Hard rules (non-negotiable)

1. **Paper trading only.** Orders may only ever be sent to Kalshi's **demo** environment
   (`https://demo-api.kalshi.co/trade-api/v2`). Production Kalshi endpoints may be used
   for *market data reads* only. Every order path must go through
   `kbtc.execution.guard.assert_order_endpoint()`. Never weaken, bypass, or edit the guard,
   `.claude/hooks/`, or `.claude/settings.json`. If a task seems to need that, stop and ask Berk.
2. **Never read or print secrets.** API keys live in `.env` / `secrets/` on the VPS only.
   Code reads them from environment variables. Never commit them.
3. **Verify contract and venue facts against primary sources before building on them**
   (Kalshi contract terms + fee schedule, CF Benchmarks BRTI methodology, exchange WS docs).
   Record what you verified, the source URL and the date in `docs/facts.md`.
   Anything unverified is marked `UNVERIFIED` in code comments and docs.
4. **No result counts until the red-team reviewer has passed it.** Backtests must model
   fees, back-of-queue fills, latency, and use strictly walk-forward / out-of-sample splits.
5. **Show the math.** Every model change ships with a derivation note in
   `docs/derivations/` (mechanics → derivation → practical implication). Berk wants to be
   able to explain and defend every piece himself.

## Architecture

```
recorders (VPS, 24/7)  →  data/raw/{venue}/{channel}/date=YYYY-MM-DD/HH.jsonl.zst
        ↓
normalize → data/clean/*.parquet  (books, trades, kalshi books/trades, settlements)
        ↓
index replica (BRTI-like)  →  fair value (digital w/ 60s settlement averaging)
        ↓                         + short-horizon drift (OFI / TFI / perp lead)
quoting (A–S adapted for binaries)  →  event-driven backtester (queue-aware)
        ↓
paper trader on Kalshi demo  →  markouts / P&L reports
```

Package layout (`src/kbtc/`):

| Module | Purpose |
|---|---|
| `fees.py` | Kalshi taker/maker fee formulas (tested) |
| `pricing.py` | Digital fair value with settlement-window averaging (tested) |
| `quoting.py` | Baseline Avellaneda–Stoikov quotes adapted to binary inventory risk (tested) |
| `execution/guard.py` | Demo-only order endpoint guard (tested, do not modify) |
| `recorders/writer.py` | Hourly-rotating zstd JSONL writer (tested) |
| `recorders/` | Venue adapters — **to be built in Phase 1** |
| `index/`, `signals/`, `backtest/`, `paper/` | Phases 2–5 |

## Key quantitative principles (established)

- Binary price = risk-neutral probability of finishing in the money.
- Settlement is a 60 s average of 1 s BRTI prints (UNVERIFIED — confirm in Phase 0).
  Averaging compresses variance: with τ to expiry and window w,
  `Var = σ²(τ − 2w/3)` for τ ≥ w, and `σ² τ³ / (3w²)` once inside the window
  (see `docs/derivations/settlement-averaging.md`). This correction is load-bearing.
- Binary inventory risk peaks at-the-money (`∂V/∂x = φ(d)/sd`), so A–S needs the
  binary's local delta, not σ²τ.
- Fill modeling must assume **back of queue**; ignoring queue position overstates edge.
- Edge ranking: index reconstruction + averaging correction > vol-forecasting alpha.
- Taker edge on these markets is essentially nil in published work (OpenMarket, 2026);
  the edge, if any, is in *passive* quoting with good fair value and adverse-selection defense.
- Stay shallow / pull quotes in the last 60–90 s when price is near the strike.

## Conventions

- Python 3.12+, `uv` for env management. Run tests with `uv run pytest -q`.
- Timestamps: integer nanoseconds UTC (`recv_ts_ns`, `exch_ts_ns`). Prices in Kalshi are
  handled in **dollars as Decimal or integer cents** at boundaries — never float for fees.
- Every new module gets unit tests. Keep functions pure where possible.
- Experiments are logged in `notes/experiments/EXP-NNN-*.md` using the template.
- Decisions go in `docs/decisions/ADR-NNN-*.md`.
- Commit small, with clear messages. Tick off `ROADMAP.md` items only when the
  acceptance criteria are met and the reviewer has signed off.

## How to work (orchestration)

You are the **lead**. Delegate to subagents in `.claude/agents/`:

| Agent | Use for |
|---|---|
| `data-engineer` | Recorders, storage, normalization, VPS deploy |
| `index-replicator` | Replicating BRTI from constituent books; tracking-error studies |
| `quant-modeler` | Fair value, vol estimation, OFI/TFI signals, quoting logic |
| `backtest-engineer` | Event-driven, queue-aware simulator; paper trader on demo |
| `red-team-reviewer` | Adversarial review of every result and every PR (read-only) |
| `quant-tutor` | Derivation notes and explanations for Berk |

Loop for each roadmap item: plan → implement (specialist) → tests pass →
red-team review → fix → derivation/notes (tutor) → update ROADMAP → commit.
Use `/next` to run this loop on the next unchecked roadmap item.
