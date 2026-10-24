"""Brier-vs-Kalshi-mid evaluation harness for the KXBTC15M fair value (ROADMAP 3.2 acceptance tool).

The ROADMAP 3.2 gate is: *does the model beat Kalshi's own mid out of sample?* This module stands
up that test. It has two layers:

1. **Pure scoring core** (data-source-agnostic, fully unit-tested on synthetic data):
   Brier + log-loss for the model and the Kalshi mid, scored against the realized YES/NO outcome,
   bucketed by (tau-to-close, |moneyness z|), with per-day block-bootstrap CIs and a head-to-head
   per-day-paired Brier-difference CI (the actual gate), plus a calibration table (predicted vs
   realized frequency).

2. **Wiring adapter** (the "run on the box" path): reconstructs the **Kalshi mid** from the
   recorded ``kalshi_books`` (yes/no) as-of each origin, pulls Y_t and sigma from a supplied index
   series / sigma sampler, builds the fair value via ``kbtc.signals.fairvalue``, and emits scored
   :class:`Prediction` rows. The index is the BRTI **replica** for the historical backtest (feed
   history < 1 day per ADR-002); it swaps to the published ``cfbenchmarks_value`` feed once enough
   of that accumulates.

Walk-forward discipline. Every forecast is causal: sigma comes from the 3.1 estimator (causal by
construction), Y_t / the forming-window partial average use only ``recv_ts_ns <= t`` data, and the
Kalshi mid is the as-of book at t. Scoring is **by calendar day**; ``test_dates`` restricts the
report to held-out days so the headline number is OOS.

**Key gate (stated plainly, per ROADMAP 3.2):** if the model's OOS Brier is NOT below the Kalshi
mid's (CI of the per-day-paired difference not clearly < 0), that is a STOP-AND-RETHINK, reported
as such — not hidden. The first pass on our few replica days is labelled PRELIMINARY; the credible
run is >= 3-5 independent held-out days and ideally the published feed.

Floats throughout (probabilities, not money). Kalshi book prices are read as dollars (0..1) and
used directly as probabilities; no fee/P&L math crosses this module.
"""
from __future__ import annotations

import bisect
import datetime as _dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from kbtc.pricing import DEFAULT_WINDOW, SettlementWindow
from kbtc.signals.fairvalue import IndexSeries, close_window_state, fair_value_up

NS_PER_S = 1_000_000_000
_EPS = 1e-15


# =========================================================================================
# Scored prediction row.
# =========================================================================================
@dataclass(frozen=True)
class Prediction:
    """One scored origin: model P(up) and Kalshi mid P(up) vs the realized YES/NO outcome."""

    date: str          # UTC calendar day of the origin (walk-forward unit)
    market_ticker: str
    origin_ns: int
    tau_s: float       # seconds to close at the origin
    z: float           # model standardized moneyness (for |z| bucketing; may be +-inf near expiry)
    outcome: int       # realized: 1 = YES (up), 0 = NO
    model_p: float     # model P(up)
    mid_p: float       # Kalshi mid P(up)


# =========================================================================================
# Loss functions.
# =========================================================================================
def brier_loss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per-observation Brier score ``(p - y)^2`` (y in {0,1})."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    return (p - y) ** 2


def log_loss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per-observation log loss ``-[y ln p + (1-y) ln(1-p)]`` with ``p`` clipped to [eps, 1-eps]."""
    p = np.clip(np.asarray(p, dtype=np.float64), _EPS, 1.0 - _EPS)
    y = np.asarray(y, dtype=np.float64)
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))


# =========================================================================================
# Per-day block bootstrap.
# =========================================================================================
def _block_bootstrap_ci(
    per_day_values: list[float], n_boot: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """95% CI for the mean of a per-day metric by resampling WHOLE days (within-day autocorr)."""
    vals = np.asarray([v for v in per_day_values if np.isfinite(v)], dtype=np.float64)
    if vals.size == 0:
        return (float("nan"), float("nan"))
    if vals.size == 1:
        return (float(vals[0]), float(vals[0]))
    rng = np.random.default_rng(seed)
    means = np.array([np.mean(rng.choice(vals, size=vals.size, replace=True)) for _ in range(n_boot)])
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


# =========================================================================================
# Report structures.
# =========================================================================================
@dataclass(frozen=True)
class ModelScore:
    n: int
    brier: float
    log_loss: float
    brier_ci: tuple[float, float]      # per-day block-bootstrap 95% CI of mean Brier
    log_loss_ci: tuple[float, float]


@dataclass(frozen=True)
class HeadToHead:
    """Model-minus-mid, paired by day. ``brier_diff`` < 0 means the model BEATS the mid."""

    n: int
    brier_diff: float                  # mean(model Brier) - mean(mid Brier), pooled
    brier_diff_ci: tuple[float, float]  # per-day-paired block-bootstrap CI of the difference
    log_loss_diff: float
    log_loss_diff_ci: tuple[float, float]
    model_beats_mid: bool              # brier_diff_ci upper bound < 0 (clear OOS win)


@dataclass(frozen=True)
class CalibrationBin:
    lo: float
    hi: float
    n: int
    mean_pred: float
    realized_freq: float


@dataclass(frozen=True)
class BucketScore:
    """One (tau, |z|) bucket. ``head_to_head.model_beats_mid`` here is **DIAGNOSTIC ONLY** — there
    are ~25 buckets with NO multiplicity control (REV-036 F5). The headline is the single pooled
    tau>=floor paired gate (``HarnessReport.head_to_head`` / ``gate_statement``), not any bucket."""

    tau_bucket: str
    z_bucket: str
    n: int
    model: ModelScore
    mid: ModelScore
    head_to_head: HeadToHead


@dataclass(frozen=True)
class HarnessReport:
    """Walk-forward Brier-vs-mid report.

    HEADLINE (``n``, ``model``, ``mid``, ``head_to_head``, ``gate_statement``, calibrations) is
    computed on **tau >= ``tau_floor_s`` origins only** (default 60 s). In-window origins
    (0 < tau < floor) are scored SEPARATELY as a DIAGNOSTIC (``in_window_*``) and are also the
    ``[0,60)`` tau bucket in ``by_bucket``: there the model mechanically OBSERVES the forming
    settlement average and converges to the realized 0/1, which would FLATTER a pooled gate without
    being skill (REV-036 F1). tau == 0 origins are dropped entirely (degenerate FP tie — REV-036 F6).
    ``by_bucket`` head-to-heads are diagnostic (see :class:`BucketScore`)."""

    test_dates: tuple[str, ...]
    tau_floor_s: float
    n: int                                   # HEADLINE count (tau >= tau_floor_s)
    model: ModelScore                        # HEADLINE
    mid: ModelScore                          # HEADLINE
    head_to_head: HeadToHead                 # HEADLINE gate (day-gated beats-mid)
    n_in_window: int                         # DIAGNOSTIC count (0 < tau < tau_floor_s)
    in_window_model: ModelScore              # DIAGNOSTIC
    in_window_mid: ModelScore                # DIAGNOSTIC
    in_window_head_to_head: HeadToHead       # DIAGNOSTIC (NOT the gate)
    by_bucket: tuple[BucketScore, ...]
    model_calibration: tuple[CalibrationBin, ...]
    mid_calibration: tuple[CalibrationBin, ...]
    gate_statement: str


# Default bucket edges (pre-registered before looking at results).
DEFAULT_TAU_EDGES_S: tuple[float, ...] = (0.0, 60.0, 180.0, 300.0, 600.0, 900.1)
DEFAULT_ABS_Z_EDGES: tuple[float, ...] = (0.0, 0.25, 0.5, 1.0, 2.0, float("inf"))


def _bucket_label(value: float, edges: tuple[float, ...]) -> str | None:
    """Half-open [lo, hi) bucket label for ``value`` over sorted ``edges``; None if out of range."""
    for lo, hi in zip(edges[:-1], edges[1:]):
        if lo <= value < hi:
            hi_txt = "inf" if hi == float("inf") else f"{hi:g}"
            return f"[{lo:g},{hi_txt})"
    return None


def _score_one(preds: list[Prediction], which: str, seed: int) -> ModelScore:
    if not preds:
        return ModelScore(0, float("nan"), float("nan"), (float("nan"),) * 2, (float("nan"),) * 2)
    p = np.array([pr.model_p if which == "model" else pr.mid_p for pr in preds])
    y = np.array([pr.outcome for pr in preds])
    bl = brier_loss(p, y)
    ll = log_loss(p, y)
    per_day_b: dict[str, list[float]] = {}
    per_day_l: dict[str, list[float]] = {}
    for pr, b, l in zip(preds, bl, ll):
        per_day_b.setdefault(pr.date, []).append(float(b))
        per_day_l.setdefault(pr.date, []).append(float(l))
    day_b = [float(np.mean(v)) for v in per_day_b.values()]
    day_l = [float(np.mean(v)) for v in per_day_l.values()]
    return ModelScore(
        n=len(preds),
        brier=float(np.mean(bl)),
        log_loss=float(np.mean(ll)),
        brier_ci=_block_bootstrap_ci(day_b, seed=seed),
        log_loss_ci=_block_bootstrap_ci(day_l, seed=seed + 1),
    )


def _head_to_head(preds: list[Prediction], seed: int, min_days: int = 1) -> HeadToHead:
    """Per-day-paired model-minus-mid head-to-head.

    ``model_beats_mid`` requires BOTH the per-day block-bootstrap Brier-diff CI upper bound < 0 AND
    at least ``min_days`` distinct held-out days (REV-036 F3: a single degenerate day must not
    auto-green-light a win). Bucket-level and in-window diagnostic calls use ``min_days=1`` (no day
    gate — diagnostic); the HEADLINE uses ``min_days>=3``.
    """
    if not preds:
        return HeadToHead(0, float("nan"), (float("nan"),) * 2, float("nan"), (float("nan"),) * 2, False)
    y = np.array([pr.outcome for pr in preds])
    mp = np.array([pr.model_p for pr in preds])
    xp = np.array([pr.mid_p for pr in preds])
    db = brier_loss(mp, y) - brier_loss(xp, y)   # model - mid (negative => model better)
    dl = log_loss(mp, y) - log_loss(xp, y)
    per_day_b: dict[str, list[float]] = {}
    per_day_l: dict[str, list[float]] = {}
    for pr, b, l in zip(preds, db, dl):
        per_day_b.setdefault(pr.date, []).append(float(b))
        per_day_l.setdefault(pr.date, []).append(float(l))
    day_b = [float(np.mean(v)) for v in per_day_b.values()]
    day_l = [float(np.mean(v)) for v in per_day_l.values()]
    b_ci = _block_bootstrap_ci(day_b, seed=seed)
    l_ci = _block_bootstrap_ci(day_l, seed=seed + 1)
    n_days = len(per_day_b)
    return HeadToHead(
        n=len(preds),
        brier_diff=float(np.mean(db)),
        brier_diff_ci=b_ci,
        log_loss_diff=float(np.mean(dl)),
        log_loss_diff_ci=l_ci,
        model_beats_mid=bool(np.isfinite(b_ci[1]) and b_ci[1] < 0.0 and n_days >= min_days),
    )


def calibration_table(
    preds: list[Prediction], which: str = "model", n_bins: int = 10
) -> tuple[CalibrationBin, ...]:
    """Reliability table: equal-width predicted-probability bins -> (mean pred, realized freq)."""
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    p = np.array([pr.model_p if which == "model" else pr.mid_p for pr in preds]) if preds else np.array([])
    y = np.array([pr.outcome for pr in preds]) if preds else np.array([])
    out: list[CalibrationBin] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        if hi >= 1.0:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        n = int(np.sum(mask))
        if n:
            out.append(CalibrationBin(float(lo), float(hi), n, float(np.mean(p[mask])),
                                      float(np.mean(y[mask]))))
        else:
            out.append(CalibrationBin(float(lo), float(hi), 0, float("nan"), float("nan")))
    return tuple(out)


def score_predictions(
    predictions: Iterable[Prediction],
    *,
    test_dates: Iterable[str] | None = None,
    tau_floor_s: float = 60.0,
    min_days_for_win: int = 3,
    tau_edges_s: tuple[float, ...] = DEFAULT_TAU_EDGES_S,
    abs_z_edges: tuple[float, ...] = DEFAULT_ABS_Z_EDGES,
    n_cal_bins: int = 10,
    seed: int = 0,
) -> HarnessReport:
    """Aggregate scored predictions into the walk-forward Brier-vs-mid report.

    ``test_dates`` restricts scoring to held-out days (default: all days present).

    REV-036 F1/F6: the HEADLINE (model/mid Brier + log-loss, the paired head-to-head GATE, and the
    calibration tables) is computed on **tau >= ``tau_floor_s`` origins only** (default 60 s).
    In-window origins (0 < tau < floor), where the model mechanically observes the forming
    settlement average and converges to the realized 0/1 (flattering, not skill), are scored
    separately as a DIAGNOSTIC and appear as the ``[0,60)`` tau bucket. tau == 0 origins are dropped.
    The headline ``model_beats_mid`` additionally requires ``>= min_days_for_win`` held-out days (F3).
    """
    preds = list(predictions)
    if test_dates is not None:
        keep = set(test_dates)
        preds = [p for p in preds if p.date in keep]
    preds = [p for p in preds if p.tau_s > 0.0]  # F6: drop degenerate tau==0

    headline = [p for p in preds if p.tau_s >= tau_floor_s]
    in_window = [p for p in preds if 0.0 < p.tau_s < tau_floor_s]
    dates = tuple(sorted({p.date for p in headline}))

    model = _score_one(headline, "model", seed)
    mid = _score_one(headline, "mid", seed + 10)
    h2h = _head_to_head(headline, seed + 20, min_days=min_days_for_win)

    iw_model = _score_one(in_window, "model", seed + 30)
    iw_mid = _score_one(in_window, "mid", seed + 40)
    iw_h2h = _head_to_head(in_window, seed + 50, min_days=1)

    # buckets over ALL origins (so [0,60) shows the in-window diagnostic); head-to-heads DIAGNOSTIC.
    buckets: dict[tuple[str, str], list[Prediction]] = {}
    for p in preds:
        tb = _bucket_label(p.tau_s, tau_edges_s)
        zb = _bucket_label(abs(p.z), abs_z_edges)
        if tb is None or zb is None:
            continue
        buckets.setdefault((tb, zb), []).append(p)
    by_bucket = tuple(
        BucketScore(
            tau_bucket=tb, z_bucket=zb, n=len(bp),
            model=_score_one(bp, "model", seed), mid=_score_one(bp, "mid", seed + 10),
            head_to_head=_head_to_head(bp, seed + 20, min_days=1),  # diagnostic (F5)
        )
        for (tb, zb), bp in sorted(buckets.items())
    )

    gate = _gate_statement(h2h, len(dates), min_days_for_win, tau_floor_s)
    return HarnessReport(
        test_dates=dates, tau_floor_s=tau_floor_s, n=len(headline),
        model=model, mid=mid, head_to_head=h2h,
        n_in_window=len(in_window), in_window_model=iw_model, in_window_mid=iw_mid,
        in_window_head_to_head=iw_h2h,
        by_bucket=by_bucket,
        model_calibration=calibration_table(headline, "model", n_cal_bins),
        mid_calibration=calibration_table(headline, "mid", n_cal_bins),
        gate_statement=gate,
    )


def _gate_statement(h2h: HeadToHead, n_days: int, min_days: int, tau_floor_s: float) -> str:
    where = f"(HEADLINE: tau >= {tau_floor_s:g} s origins only — in-window excluded, REV-036 F1)"
    if h2h.n == 0:
        return f"NO DATA: no scored tau>={tau_floor_s:g}s origins; cannot evaluate the beats-mid gate. {where}"
    cred = "" if n_days >= min_days else (
        f" PRELIMINARY: only {n_days} held-out day(s) — NOT a credible run "
        f"(need >={min_days}-5 independent days, ideally the published feed)."
    )
    lo, hi = h2h.brier_diff_ci
    raw_win = bool(np.isfinite(hi) and hi < 0.0)
    if h2h.model_beats_mid:
        return (f"Model BEATS the Kalshi mid OOS {where}: Brier diff (model-mid) = {h2h.brier_diff:.5f}, "
                f"95% CI [{lo:.5f}, {hi:.5f}] (upper bound < 0, >={min_days} days).{cred}")
    if raw_win and n_days < min_days:
        return (f"NOT GREEN-LIT {where}: the Brier-diff CI favors the model (diff {h2h.brier_diff:.5f}, "
                f"95% CI [{lo:.5f}, {hi:.5f}]) but only {n_days} held-out day(s) — need >={min_days}.{cred}")
    if np.isfinite(lo) and lo > 0:
        return (f"STOP-AND-RETHINK {where}: model is WORSE than the Kalshi mid OOS (Brier diff "
                f"{h2h.brier_diff:.5f}, 95% CI [{lo:.5f}, {hi:.5f}], lower bound > 0).{cred}")
    return (f"STOP-AND-RETHINK {where}: model does NOT clearly beat the Kalshi mid OOS (Brier diff "
            f"{h2h.brier_diff:.5f}, 95% CI [{lo:.5f}, {hi:.5f}] straddles 0).{cred}")


# =========================================================================================
# Kalshi mid reconstruction from normalized kalshi_books (yes/no, prices in DOLLARS 0..1).
# =========================================================================================
def _to_float_price(x: object) -> float | None:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return float(x)
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class KalshiBookSeries:
    """Causal as-of best yes/no bid for one KXBTC15M market, from normalized ``kalshi_books`` rows.

    Normalized ``kalshi_books`` (schema.py / recorders/kalshi.py): ``side`` in {"yes","no"},
    ``price`` in DOLLARS (0..1, e.g. "0.0800"), ``size`` = contract count. A "yes" level is a bid to
    BUY YES at that price; a "no" level a bid to BUY NO. Buying NO at q == selling YES at (1-q), so
    the best offer to SELL YES is ``1 - best_no_bid``. The Kalshi YES mid is therefore
    ``(best_yes_bid + (1 - best_no_bid)) / 2`` — this is the benchmark fair value to beat.

    Rows sharing ``snap_id`` are one book state; ordered by ``(recv_ts_ns, snap_id)``. ``as_of``
    uses the greatest state with ``recv_ts_ns <= t``. Only ``synced`` snapshots are kept (a mid off
    an incomplete/mid-resync book is not a trustworthy benchmark).
    """

    keys: list[tuple[int, int]]                 # (recv_ts_ns, snap_id) sorted
    best_yes: list[float | None]
    best_no: list[float | None]

    @classmethod
    def from_rows(cls, rows: Iterable[dict], *, require_synced: bool = True) -> "KalshiBookSeries":
        by_snap: dict[int, dict] = {}
        for r in rows:
            if require_synced and not bool(r.get("synced", False)):
                continue
            sid = int(r["snap_id"])
            g = by_snap.get(sid)
            if g is None:
                g = {"recv_ts_ns": int(r["recv_ts_ns"]), "yes": None, "no": None}
                by_snap[sid] = g
            price = _to_float_price(r.get("price"))
            size = _to_float_price(r.get("size"))
            if price is None or size is None or size <= 0 or price <= 0:
                continue
            side = r.get("side")
            if side == "yes":
                if g["yes"] is None or price > g["yes"]:
                    g["yes"] = price
            elif side == "no":
                if g["no"] is None or price > g["no"]:
                    g["no"] = price
        items = sorted(((g["recv_ts_ns"], sid, g["yes"], g["no"]) for sid, g in by_snap.items()))
        keys = [(it[0], it[1]) for it in items]
        return cls(keys=keys, best_yes=[it[2] for it in items], best_no=[it[3] for it in items])

    def _idx_at(self, t_ns: int) -> int:
        return bisect.bisect_right(self.keys, (t_ns, float("inf"))) - 1

    def yes_mid_at(self, t_ns: int, *, skip_crossed: bool = True, cross_tol: float = 1e-9) -> float | None:
        """YES mid P(up) in [0,1] as-of ``t_ns``; ``None`` if either side is absent at ``t``.

        YES mid = ``(best_yes_bid + (1 - best_no_bid)) / 2``. The book is CROSSED when the YES bid is
        strictly above the YES ask, i.e. ``best_yes_bid > 1 - best_no_bid`` (equivalently
        ``best_yes_bid + best_no_bid > 1``); LOCKED when equal. REV-036 F4: a crossed book's "mid" is
        not a meaningful benchmark, so by default (``skip_crossed=True``) a crossed state returns
        ``None`` (disclosed to the caller as a missing quote, like an absent side). A LOCKED book
        (zero spread) is kept — its mid is well defined. Set ``skip_crossed=False`` to keep the raw
        (possibly out-of-[0,1]) crossed mid and disclose it instead.
        """
        i = self._idx_at(t_ns)
        if i < 0:
            return None
        by, bn = self.best_yes[i], self.best_no[i]
        if by is None or bn is None:
            return None
        if skip_crossed and (by + bn) > 1.0 + cross_tol:  # yes_bid > yes_ask => crossed
            return None
        return 0.5 * (by + (1.0 - bn))


# =========================================================================================
# Wiring: build scored predictions for one market.
# =========================================================================================
@dataclass(frozen=True)
class MarketSpec:
    """One settled KXBTC15M market's ground truth (from the ``settlements`` dataset)."""

    market_ticker: str
    strike: float            # open_value / floor_strike (open-window 60 s average)
    outcome: int             # 1 = YES (result=="yes"), 0 = NO
    open_time_ns: int
    close_time_ns: int


def market_spec_from_settlement(row: dict) -> MarketSpec | None:
    """Build a :class:`MarketSpec` from a normalized settlement row; None if not usable.

    Requires a numeric ``open_value`` (strike), open/close times, and a terminal ``result`` in
    {"yes","no"}. ``outcome`` is taken from ``result`` (empirically == ``close_value >= open_value``
    per ROADMAP 1.2); if ``result`` is absent we fall back to that comparison.
    """
    strike = _to_float_price(row.get("open_value"))
    open_ns = row.get("open_time_ns")
    close_ns = row.get("close_time_ns")
    if strike is None or strike <= 0 or open_ns is None or close_ns is None:
        return None
    result = row.get("result")
    if result in ("yes", "no"):
        outcome = 1 if result == "yes" else 0
    else:
        close_v = _to_float_price(row.get("close_value"))
        if close_v is None:
            return None
        outcome = 1 if close_v >= strike else 0
    return MarketSpec(
        market_ticker=str(row.get("market_ticker", "")),
        strike=strike, outcome=outcome,
        open_time_ns=int(open_ns), close_time_ns=int(close_ns),
    )


def _utc_date(ns: int) -> str:
    return _dt.datetime.fromtimestamp(ns // NS_PER_S, tz=_dt.UTC).strftime("%Y-%m-%d")


def build_market_predictions(
    spec: MarketSpec,
    index: IndexSeries,
    sigma_at: Callable[[int], float],
    kalshi: KalshiBookSeries,
    *,
    origin_spacing_s: float = 10.0,
    window: SettlementWindow = DEFAULT_WINDOW,
    drift_per_s: float = 0.0,
    skip_crossed: bool = True,
) -> list[Prediction]:
    """Scored :class:`Prediction` rows for one market over its tradeable life.

    Origins step every ``origin_spacing_s`` from the first instant the strike is fully formed
    (``open_time + window.length_s``) through ``close_time`` but STRICTLY BEFORE close (tau == 0 is
    dropped — degenerate FP tie, REV-036 F6; the headline gate is tau>=60 s anyway, F1). At each
    origin t we need: Y_t (index as-of t), sigma_t (``sigma_at(t)``, from the causal 3.1 estimator),
    the forming-close-window state, and the Kalshi yes/no mid as-of t (``skip_crossed`` drops a
    crossed-book mid, F4). Any origin missing one of these (feed gap, no quote, crossed book,
    non-positive sigma) is skipped, not fudged.
    """
    out: list[Prediction] = []
    start = spec.open_time_ns + int(round(window.length_s * NS_PER_S))
    step_ns = int(round(origin_spacing_s * NS_PER_S))
    t = start
    while t < spec.close_time_ns:  # strictly before close: drop tau == 0 (F6)
        y = index.price_at(t)
        sigma = sigma_at(t)
        mid = kalshi.yes_mid_at(t, skip_crossed=skip_crossed)
        if (np.isfinite(y) and y > 0 and np.isfinite(sigma) and sigma > 0 and mid is not None):
            tau_s = (spec.close_time_ns - t) / NS_PER_S
            try:
                state = close_window_state(index.log_price_at, spec.close_time_ns, t, window)
                dv = fair_value_up(y, spec.strike, tau_s, sigma, state=state, window=window,
                                   drift_per_s=drift_per_s)
            except ValueError:
                t += step_ns
                continue
            out.append(Prediction(
                date=_utc_date(t), market_ticker=spec.market_ticker, origin_ns=t,
                tau_s=tau_s, z=dv.z, outcome=spec.outcome,
                model_p=dv.prob_up, mid_p=float(mid),
            ))
        t += step_ns
    return out


# =========================================================================================
# Calibration plot (lead-run convenience; matplotlib is NOT a hard dependency).
# =========================================================================================
def plot_calibration(report: HarnessReport, out_path: str) -> str:  # pragma: no cover - plotting
    """Write the model-vs-mid reliability diagram (predicted vs realized frequency)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise ImportError("plot_calibration needs matplotlib (pip install matplotlib)") from e

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect")
    for cal, name in ((report.model_calibration, "model"), (report.mid_calibration, "kalshi mid")):
        xs = [c.mean_pred for c in cal if c.n > 0]
        ys = [c.realized_freq for c in cal if c.n > 0]
        ax.plot(xs, ys, "o-", label=name)
    ax.set(xlabel="predicted P(up)", ylabel="realized frequency",
           title="KXBTC15M calibration: model vs Kalshi mid", xlim=(0, 1), ylim=(0, 1))
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path
