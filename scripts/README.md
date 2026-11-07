# scripts

Experiment drivers referenced from `notes/experiments/`. Each is offline, read-only, and takes the
normalized parquet (`data/clean`) or a downloaded public dataset as input.

| Script | Experiment | Purpose |
|---|---|---|
| `experiments/exp009_gate_run.py` | EXP-009 | Queue-aware, latency-aware maker markout gate on recorded KXBTC15M books |
| `experiments/exp010_download.py` | EXP-010 | Download public Kalshi trades for single-name/mention series |
| `experiments/exp010_study.py` | EXP-010 | Frictionless vs. back-of-queue test of the resting-NO premium |

The EXP-006 volatility drivers (`run_vol_qlike_onbox.py`, `replica_qlike.py`, `run_vol_qlike_cached.py`)
were run on the recording server and were not committed; EXP-006 records their protocol and results.
Core analysis code lives in `src/kbtc/` (`index/tracking.py`, `signals/`, `backtest/`).
