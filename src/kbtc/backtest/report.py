"""Thin aggregation/reporting layer over per-market backtest results (feeds the 4.2 gate).

A full study runs ``run_backtest`` once per 15-min KXBTC15M market and collects the results here.
This module produces: net P&L per market/day, fill counts, inventory distribution, markout
summaries at 5 s/30 s/settlement, and a MARKET-BLOCK BOOTSTRAP confidence interval (resample whole
15-min markets with replacement, STRATIFIED by day — the 15-min market is the independent unit, and
days are the strata). Sensitivity tables over latency and queue assumptions are a convenience
wrapper that re-runs the engine across a parameter grid.

NO result/edge number is asserted here; this is plumbing. 4.2 acceptance (fees + back-of-queue +
latency, >= 7 OOS days, red-team sign-off) runs on top of it later.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import fmean
from typing import Callable, Iterable, Sequence

from .engine import BacktestConfig, BacktestMarket, BacktestResult, run_backtest
from .markout import markouts, net_settlement_pnl

ZERO = Decimal(0)


@dataclass(frozen=True)
class MarketOutcome:
    """One market's headline numbers, tagged with its day (``YYYY-MM-DD``) for stratification."""

    ticker: str
    day: str
    fill_count: int
    net_pnl: float | None  # settlement P&L net of fees (None if unsettled)
    total_fee: float
    final_inventory: int
    mean_markout_5s: float | None
    mean_markout_30s: float | None


def summarize_market(result: BacktestResult, day: str) -> MarketOutcome:
    mos = markouts(result)
    m5 = [float(m.markout_5s) for m in mos if m.markout_5s is not None]
    m30 = [float(m.markout_30s) for m in mos if m.markout_30s is not None]
    npnl = net_settlement_pnl(result)
    return MarketOutcome(
        ticker=result.market.ticker, day=day, fill_count=result.fill_count,
        net_pnl=(float(npnl) if npnl is not None else None),
        total_fee=float(result.total_fee), final_inventory=result.final_inventory,
        mean_markout_5s=(fmean(m5) if m5 else None),
        mean_markout_30s=(fmean(m30) if m30 else None),
    )


@dataclass
class StudyReport:
    outcomes: list[MarketOutcome] = field(default_factory=list)

    @property
    def n_markets(self) -> int:
        return len(self.outcomes)

    @property
    def total_fills(self) -> int:
        return sum(o.fill_count for o in self.outcomes)

    def total_net_pnl(self) -> float:
        return sum(o.net_pnl for o in self.outcomes if o.net_pnl is not None)

    def per_day_pnl(self) -> dict[str, float]:
        d: dict[str, float] = defaultdict(float)
        for o in self.outcomes:
            if o.net_pnl is not None:
                d[o.day] += o.net_pnl
        return dict(sorted(d.items()))

    def inventory_distribution(self) -> dict[int, int]:
        d: dict[int, int] = defaultdict(int)
        for o in self.outcomes:
            d[o.final_inventory] += 1
        return dict(sorted(d.items()))

    def mean_markout(self, horizon: str = "5s") -> float | None:
        key = "mean_markout_5s" if horizon == "5s" else "mean_markout_30s"
        vals = [getattr(o, key) for o in self.outcomes if getattr(o, key) is not None]
        return fmean(vals) if vals else None


def daily_pnl_totals(outcomes: Sequence[MarketOutcome]) -> list[tuple[str, float]]:
    """Per-day total net P&L (settled markets only), sorted by day — the GATE's independent unit."""
    by_day: dict[str, float] = defaultdict(float)
    for o in outcomes:
        if o.net_pnl is not None:
            by_day[o.day] += o.net_pnl
    return sorted(by_day.items())


def bootstrap_pnl_ci(outcomes: Sequence[MarketOutcome], *, n_boot: int = 5000,
                     alpha: float = 0.05, seed: int = 0,
                     statistic: str = "total") -> tuple[float, float, float]:
    """DAY-level block bootstrap CI for P&L (the 4.2 go/no-go unit).

    SEV-2 fix: markets WITHIN a day are autocorrelated (shared BTC path/vol), so resampling
    individual 15-min markets treats non-iid units as iid and yields a CI that is too NARROW —
    exactly the wrong direction for a go/no-go gate. The INDEPENDENT unit is the DAY. This resamples
    whole DAYS with replacement (each day's total P&L is one block), requiring >=7 days for the gate.
    ``statistic`` is ``"total"`` (sum over days) or ``"mean"`` (mean daily P&L). Returns
    ``(point, lo, hi)`` at the ``1-alpha`` level. Days with no settled market are dropped.

    For a small number of days the day-level block bootstrap is the sole primary interval; a
    normal-approximation t-interval was removed as misleading on few days.
    """
    import numpy as np

    totals = [p for _, p in daily_pnl_totals(outcomes)]
    if not totals:
        return (float("nan"), float("nan"), float("nan"))
    arr = np.asarray(totals, dtype=float)
    rng = np.random.default_rng(seed)

    def stat(vals: np.ndarray) -> float:
        return float(vals.sum()) if statistic == "total" else float(vals.mean())

    point = stat(arr)
    boot = np.empty(n_boot, dtype=float)
    n = len(arr)
    for b in range(n_boot):
        boot[b] = stat(arr[rng.integers(0, n, n)])  # resample whole days
    lo = float(np.quantile(boot, alpha / 2))
    hi = float(np.quantile(boot, 1 - alpha / 2))
    return (point, lo, hi)


def day_block_bootstrap_mean(values_by_day: dict[str, Sequence[float]], *, n_boot: int = 5000,
                             alpha: float = 0.05, seed: int = 0) -> tuple[float, int, float, float]:
    """Day-level block bootstrap of the POOLED MEAN of per-item values (the markout gate statistic).

    ``values_by_day`` maps day -> the per-fill (or per-item) values observed that day. Resamples whole
    DAYS with replacement (the independent block; markouts within a day share the BTC path), pools the
    resampled days' values, and takes the mean. Returns ``(point_mean, n_values, lo, hi)`` at the
    ``1-alpha`` level. The point estimate is the pooled mean over all real days.

    (A per-item/iid bootstrap would under-cover; this is the SEV-2-correct block for the gate.)
    """
    import numpy as np

    days = [d for d, v in values_by_day.items() if len(v) > 0]
    if not days:
        return (float("nan"), 0, float("nan"), float("nan"))
    arrays = {d: np.asarray(values_by_day[d], dtype=float) for d in days}
    all_vals = np.concatenate(list(arrays.values()))
    point = float(all_vals.mean())
    rng = np.random.default_rng(seed)
    keys = list(arrays.keys())
    boot = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        pick = rng.integers(0, len(keys), len(keys))
        pooled = np.concatenate([arrays[keys[k]] for k in pick])
        boot[b] = pooled.mean()
    lo = float(np.quantile(boot, alpha / 2))
    hi = float(np.quantile(boot, 1 - alpha / 2))
    return (point, int(all_vals.size), lo, hi)


# ---------------------------------------------------------------------------------------
# Sensitivity grid over latency / queue assumptions.
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SensitivityCell:
    config: BacktestConfig
    total_net_pnl: float
    total_fills: int
    mean_markout_5s: float | None


def sensitivity_table(
    markets: Iterable[tuple[BacktestMarket, str, Iterable, Iterable]],
    policy_factory: Callable[[], object],
    configs: Sequence[BacktestConfig],
) -> list[SensitivityCell]:
    """Re-run the study across a grid of configs (latency / fill mode / cancel policy).

    ``markets`` is an iterable of ``(market, day, kalshi_records, index_ticks)``. For reproducibility
    the records must be re-iterable (materialize lists before calling). ``policy_factory`` builds a
    fresh policy per run. Returns one :class:`SensitivityCell` per config.

    SEV-2 / PRE-REGISTRATION: this grid is a ROBUSTNESS diagnostic only — report EVERY cell. The 4.2
    gate config (latency, ``FillMode``, ``CancelPolicy``) MUST be fixed in the experiment's ADR/plan
    BEFORE running and the accepted number read from THAT cell. Never pick the best-looking cell
    post hoc, and never accept a PRO_RATA cell (optimistic upper bound — see ``CancelPolicy``).
    """
    mkts = [(m, day, list(kr), list(ix)) for (m, day, kr, ix) in markets]
    cells: list[SensitivityCell] = []
    for cfg in configs:
        report = StudyReport()
        for market, day, kr, ix in mkts:
            res = run_backtest(market, policy_factory(), kr, ix, cfg)
            report.outcomes.append(summarize_market(res, day))
        cells.append(SensitivityCell(
            config=cfg, total_net_pnl=report.total_net_pnl(), total_fills=report.total_fills,
            mean_markout_5s=report.mean_markout("5s"),
        ))
    return cells
