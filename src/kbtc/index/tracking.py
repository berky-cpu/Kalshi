"""Tracking-error harness: replica 60 s average vs Kalshi settlement ground truth (2.2 Part B).

Ground truth (settlement.py / schema.py ``settlements``): ``close_value`` == Kalshi
``expiration_value`` == the CLOSE-window 60 s BRTI average; ``open_value`` == ``floor_strike`` ==
the OPEN-window 60 s average / strike. Both are 60 BRTI prints at 1 s spacing, top-of-second
(facts.md 0.1), rounded to $0.01.

For each settled KXBTC15M market we reconstruct the replica index at 1 Hz over a window, take the
simple 60-print mean, and compare to the published value. The window ENDPOINT (``[T-59,T]`` vs
``[T-60,T-1]``, O-5) is unresolved by the CF methodology, so we compute BOTH and report which
better matches — that regression is the recommended closer for O-5.

Tracking error is reported as median / p95 / max absolute error in $ (facts.md 2.2 target:
p95 < $5), split by window (close/open) and endpoint, with breakdowns vs time-of-day and vs
realized volatility, and the worst markets. Expect worse-than-target with only 2/3 constituents
recorded — reported honestly (the missing-venue gap is surfaced by loader.load_constituent_series).
"""
from __future__ import annotations

import math
from collections.abc import Hashable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from .book import SnapshotSeries
from .brti import compute_index
from .filters import DeviationFilter
from .loader import books_at
from .params import CENT, NS_PER_S, WINDOW_ENDPOINTS, BrtiParams


def as_price_decimal(x: object) -> Decimal | None:
    """Coerce a published settlement value to exact Decimal WITHOUT ever crossing a float.

    Kalshi exposes ``floor_strike`` as a JSON float and ``expiration_value`` as a string; the
    settlements parquet stores both as exact ``Decimal`` (schema.py ``DEC``). This never does
    ``Decimal(a_float)`` — a Decimal passes through untouched, anything else goes via
    ``Decimal(str(x))`` — so the <$5 tracking comparison carries no binary-float noise.
    """
    if x is None:
        return None
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def snap_to_second_ns(ts_ns: int) -> int:
    """Snap a ns timestamp down to its integer UTC second (BRTI prints are top-of-second)."""
    return (ts_ns // NS_PER_S) * NS_PER_S


def window_sample_times_ns(reference_time_ns: int, endpoint: str) -> list[int]:
    """The 60 integer-second sample times for a window ending at ``reference_time_ns``.

    ``endpoint`` selects the placement (params.WINDOW_ENDPOINTS): ``"T-59..T"`` includes the
    reference-second print; ``"T-60..T-1"`` is the 60 s strictly before it. 60 points, 1 s apart.
    """
    lo, hi = WINDOW_ENDPOINTS[endpoint]
    ref = snap_to_second_ns(reference_time_ns)
    return [ref + k * NS_PER_S for k in range(lo, hi + 1)]


@dataclass(frozen=True)
class WindowAverage:
    """A replica 60 s average over one window, with per-print diagnostics."""

    reference_time_ns: int
    endpoint: str
    average: Decimal | None          # rounded to $0.01 (None if no prints at all)
    average_raw: Decimal | None      # unrounded mean of the prints
    n_used: int                      # prints that produced a value
    n_gap: int                       # sample seconds that gapped (no print)
    prints: tuple[tuple[int, Decimal | None], ...]  # (t_ns, value|None) per sample second
    venues_used: tuple[str, ...]     # union of constituents that contributed across the window
    realized_vol: float | None       # std of 1 s log-returns of the used prints (diagnostic)


def replica_window_average(
    series_by_venue: dict[str, SnapshotSeries],
    reference_time_ns: int,
    endpoint: str,
    params: BrtiParams | None = None,
    dev_filter: DeviationFilter | None = None,
) -> WindowAverage:
    """Reconstruct the replica index at each of the 60 window seconds and average the prints.

    The §5.3 deviation-filter hysteresis is carried across the window's 60 ticks in chronological
    order (a fresh filter per window unless one is supplied). Gapped seconds (calc-failure prints)
    are excluded from the mean; ``n_gap`` records them. The mean is rounded to $0.01 to match
    Kalshi's published precision; ``average_raw`` keeps the unrounded value.
    """
    params = params or BrtiParams()
    dev_filter = dev_filter or DeviationFilter(params)
    times = window_sample_times_ns(reference_time_ns, endpoint)

    prints: list[tuple[int, Decimal | None]] = []
    vals: list[Decimal] = []
    venues: set[str] = set()
    for t in times:
        res = compute_index(books_at(series_by_venue, t), t, params, dev_filter)
        if res.gap or res.value is None:
            prints.append((t, None))
        else:
            prints.append((t, res.value))
            vals.append(res.value)
            venues.update(res.venues_used)

    if not vals:
        return WindowAverage(reference_time_ns, endpoint, None, None, 0, len(times),
                             tuple(prints), (), None)

    raw = sum(vals) / Decimal(len(vals))
    avg = raw.quantize(CENT, rounding=ROUND_HALF_UP)
    rvol = _realized_vol([float(v) for v in vals])
    return WindowAverage(reference_time_ns, endpoint, avg, raw, len(vals),
                         len(times) - len(vals), tuple(prints), tuple(sorted(venues)), rvol)


def _realized_vol(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    rets = [math.log(values[i] / values[i - 1]) for i in range(1, len(values))
            if values[i] > 0 and values[i - 1] > 0]
    if len(rets) < 2:
        return None
    return float(np.std(rets, ddof=1))


@dataclass(frozen=True)
class WindowResult:
    """A replica average vs its published settlement value for one window + endpoint."""

    market_ticker: str
    reference_time_ns: int
    window_type: str            # "close" | "open"
    endpoint: str
    replica_avg: Decimal | None
    published: Decimal | None
    error: Decimal | None       # replica_avg − published (signed, $)
    n_used: int
    n_gap: int
    venues_used: tuple[str, ...]
    realized_vol: float | None

    @property
    def abs_error(self) -> Decimal | None:
        return None if self.error is None else abs(self.error)


def evaluate_market(
    series_by_venue: dict[str, SnapshotSeries],
    *,
    market_ticker: str,
    close_time_ns: int | None,
    close_value: Decimal | None,
    open_time_ns: int | None,
    open_value: Decimal | None,
    params: BrtiParams | None = None,
    endpoints: list[str] | None = None,
) -> list[WindowResult]:
    """Evaluate one settled market's close and open windows against the published values.

    Returns a WindowResult per (window_type, endpoint). ``close_value`` (expiration_value) is the
    close-window ground truth referenced to ``close_time_ns``; ``open_value`` (floor_strike) is the
    open-window ground truth referenced to ``open_time_ns``. A window with no reference time or no
    published value is skipped.
    """
    params = params or BrtiParams()
    endpoints = endpoints or list(WINDOW_ENDPOINTS.keys())
    out: list[WindowResult] = []
    # Coerce the published ground truth to exact Decimal without ever touching float.
    windows = [
        ("close", close_time_ns, as_price_decimal(close_value)),
        ("open", open_time_ns, as_price_decimal(open_value)),
    ]
    for wtype, ref_ns, published in windows:
        if ref_ns is None:
            continue
        for ep in endpoints:
            wa = replica_window_average(series_by_venue, ref_ns, ep, params)
            err = None
            if wa.average is not None and published is not None:
                err = wa.average - published
            out.append(WindowResult(
                market_ticker=market_ticker,
                reference_time_ns=ref_ns,
                window_type=wtype,
                endpoint=ep,
                replica_avg=wa.average,
                published=published,
                error=err,
                n_used=wa.n_used,
                n_gap=wa.n_gap,
                venues_used=wa.venues_used,
                realized_vol=wa.realized_vol,
            ))
    return out


# ---------------------------------------------------------------------------------------
# Aggregation / reporting.
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ErrorStats:
    n: int                    # results with a computable error
    n_full: int               # of those, how many used all 60 prints (no gaps)
    median_abs: float | None  # $
    p95_abs: float | None     # $
    max_abs: float | None     # $
    mean_signed: float | None  # $ (bias)
    # F4: the constituents that actually fed these numbers, PHYSICALLY attached so a headline p95
    # can never be read without its coverage caveat (e.g. ('bitstamp','kraken') = 2/7, NOT the
    # full index's accuracy). Union over the contributing windows.
    venues_used: tuple[str, ...] = ()


def _abs_errors(results: list[WindowResult]) -> list[float]:
    return [float(r.abs_error) for r in results if r.abs_error is not None]


def error_stats(results: list[WindowResult]) -> ErrorStats:
    """median / p95 / max absolute error ($) + signed-mean bias + coverage over ``results``."""
    errs = _abs_errors(results)
    signed = [float(r.error) for r in results if r.error is not None]
    n_full = sum(1 for r in results if r.error is not None and r.n_gap == 0)
    venues = tuple(sorted({v for r in results if r.error is not None for v in r.venues_used}))
    if not errs:
        return ErrorStats(0, n_full, None, None, None, None, venues)
    a = np.asarray(errs)
    return ErrorStats(
        n=len(errs),
        n_full=n_full,
        median_abs=float(np.median(a)),
        p95_abs=float(np.percentile(a, 95)),
        max_abs=float(np.max(a)),
        mean_signed=float(np.mean(signed)) if signed else None,
        venues_used=venues,
    )


def summarize(results: list[WindowResult]) -> dict[tuple[str, str], ErrorStats]:
    """ErrorStats grouped by (window_type, endpoint) — IN-SAMPLE EXPLORATION ONLY.

    This shows every endpoint's error over whatever ``results`` you pass. It is for inspecting
    the candidates; it is NOT the acceptance number. Because it reports over the same windows you
    would pick the best endpoint from, using it as the headline overfits (O-5 tuning + reporting
    on the same dates). The p95<$5 acceptance number MUST come from :func:`evaluate_oos`, which
    tunes on a TRAIN date split and reports only on a disjoint TEST split.
    """
    groups: dict[tuple[str, str], list[WindowResult]] = {}
    for r in results:
        groups.setdefault((r.window_type, r.endpoint), []).append(r)
    return {k: error_stats(v) for k, v in groups.items()}


def best_endpoint(results: list[WindowResult]) -> dict[str, str]:
    """For each window_type, the endpoint with the lowest median absolute error (O-5 tuning).

    This is the empirical test of the window-placement open item: whichever off-by-one-second
    endpoint reproduces Kalshi's published averages better is the one Kalshi uses. It is a TUNING
    step — call it on TRAIN data only and report accuracy on a disjoint TEST split (see
    :func:`evaluate_oos`). Picking the best endpoint AND reporting its error on the same windows
    overstates accuracy.
    """
    stats = summarize(results)
    out: dict[str, str] = {}
    by_wtype: dict[str, list[tuple[str, float]]] = {}
    for (wtype, ep), st in stats.items():
        if st.median_abs is not None:
            by_wtype.setdefault(wtype, []).append((ep, st.median_abs))
    for wtype, cand in by_wtype.items():
        out[wtype] = min(cand, key=lambda t: t[1])[0]
    return out


def worst_markets(results: list[WindowResult], k: int = 10) -> list[WindowResult]:
    """The ``k`` results with the largest absolute error (the worst days to inspect)."""
    scored = [r for r in results if r.abs_error is not None]
    scored.sort(key=lambda r: float(r.abs_error), reverse=True)
    return scored[:k]


def _hour_of_day(ns: int) -> int:
    return datetime.fromtimestamp(ns // NS_PER_S, tz=UTC).hour


def by_time_of_day(results: list[WindowResult]) -> dict[int, ErrorStats]:
    """ErrorStats bucketed by UTC hour-of-day of the reference time (error-vs-time-of-day)."""
    groups: dict[int, list[WindowResult]] = {}
    for r in results:
        groups.setdefault(_hour_of_day(r.reference_time_ns), []).append(r)
    return {h: error_stats(v) for h, v in sorted(groups.items())}


def by_volatility(results: list[WindowResult], n_buckets: int = 5) -> dict[int, ErrorStats]:
    """ErrorStats bucketed into ``n_buckets`` quantiles of window realized vol (error-vs-vol).

    Bucket 0 = lowest vol. Results without a realized_vol are skipped.
    """
    scored = [(r.realized_vol, r) for r in results if r.realized_vol is not None]
    if not scored:
        return {}
    vols = np.asarray([v for v, _ in scored])
    edges = np.quantile(vols, np.linspace(0, 1, n_buckets + 1))
    groups: dict[int, list[WindowResult]] = {}
    for v, r in scored:
        b = int(np.clip(np.searchsorted(edges, v, side="right") - 1, 0, n_buckets - 1))
        groups.setdefault(b, []).append(r)
    return {b: error_stats(v) for b, v in sorted(groups.items())}


# ---------------------------------------------------------------------------------------
# Out-of-sample discipline (ROADMAP: "never tune the replica on the same days you report as
# out-of-sample"). The tunable choices are the window endpoint (O-5) and the replica param knobs
# cap_mean (O-4) / median_base (O-9). evaluate_oos tunes them on a TRAIN date split, FREEZES the
# choice, and reports error_stats ONLY on a disjoint TEST date split — so the headline p95<$5 can
# never be the over-fitted best-on-the-same-days number.
# ---------------------------------------------------------------------------------------
def result_date(r: WindowResult) -> str:
    """UTC date (YYYY-MM-DD) of a result's reference time — the unit of the train/test split."""
    return datetime.fromtimestamp(r.reference_time_ns // NS_PER_S, tz=UTC).strftime("%Y-%m-%d")


def all_dates(results: list[WindowResult]) -> list[str]:
    """Sorted distinct UTC dates present in ``results`` (chronological)."""
    return sorted({result_date(r) for r in results})


def split_by_date(
    results: list[WindowResult], train_dates: set[str], test_dates: set[str]
) -> tuple[list[WindowResult], list[WindowResult]]:
    """Partition ``results`` into (train, test) by the UTC date of each window.

    Raises ValueError if the two date sets OVERLAP — this is the hard guarantee that a date used
    for tuning is never also used for reporting.
    """
    overlap = set(train_dates) & set(test_dates)
    if overlap:
        raise ValueError(f"train/test dates overlap (would leak): {sorted(overlap)}")
    train = [r for r in results if result_date(r) in train_dates]
    test = [r for r in results if result_date(r) in test_dates]
    return train, test


def chronological_split(
    results: list[WindowResult], train_frac: float = 0.5
) -> tuple[set[str], set[str]]:
    """Split the DISTINCT dates chronologically: earliest ``train_frac`` → train, rest → test.

    Splitting on whole dates (not individual windows) means train and test never share a day, and
    the chronological order avoids using the future to tune the past. Returns (train_dates,
    test_dates); with <2 distinct dates the test set is empty (reported honestly by evaluate_oos).
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError("train_frac must be in (0, 1)")
    dates = all_dates(results)
    cut = int(round(len(dates) * train_frac))
    cut = min(max(cut, 1), len(dates))  # ≥1 train date when any exist
    return set(dates[:cut]), set(dates[cut:])


@dataclass(frozen=True)
class OOSReport:
    """Out-of-sample tracking-error report: WHAT was tuned, on WHAT, and the held-out accuracy.

    ``test_stats`` is the headline (p95<$5 acceptance) — computed ONLY over TEST-split windows of
    the FROZEN (config, endpoint). ``train_stats`` is the same frozen choice's TRAIN error, kept
    for transparency (expect test ≥ train if there was any overfit). ``frozen_config`` /
    ``frozen_endpoint`` and the date sets make the report self-documenting; ``test_stats.venues_used``
    carries the constituent-coverage caveat.
    """

    frozen_config: Hashable
    frozen_endpoint: str
    window_type: str
    train_dates: tuple[str, ...]
    test_dates: tuple[str, ...]
    n_train: int
    n_test: int
    train_stats: ErrorStats
    test_stats: ErrorStats


def _median_or_inf(st: ErrorStats) -> float:
    return st.median_abs if st.median_abs is not None else float("inf")


def tune_endpoint_and_config(
    results_by_config: dict[Hashable, list[WindowResult]],
    window_type: str,
) -> tuple[Hashable, str, ErrorStats]:
    """On the GIVEN (train) results, pick the (config, endpoint) with the lowest median |error|.

    ``results_by_config`` maps a config label (e.g. the BrtiParams knobs that were varied — a
    single entry if only the endpoint is tuned) to its windows (both endpoints present). Returns
    (best_config, best_endpoint, its ErrorStats on these results).
    """
    best: tuple[Hashable, str, ErrorStats] | None = None
    for cfg, res in results_by_config.items():
        groups: dict[str, list[WindowResult]] = {}
        for r in res:
            if r.window_type == window_type:
                groups.setdefault(r.endpoint, []).append(r)
        for ep, rs in groups.items():
            st = error_stats(rs)
            if st.median_abs is None:
                continue
            if best is None or _median_or_inf(st) < _median_or_inf(best[2]):
                best = (cfg, ep, st)
    if best is None:
        raise ValueError("no tunable (config, endpoint) with a computable error on the train set")
    return best


def evaluate_oos(
    results_by_config: dict[Hashable, list[WindowResult]],
    train_dates: set[str],
    test_dates: set[str],
    window_type: str = "close",
) -> OOSReport:
    """Tune (endpoint + config) on TRAIN, FREEZE, report error_stats on the disjoint TEST split.

    ``results_by_config`` is produced by running :func:`evaluate_market` under each candidate
    BrtiParams variant (one entry if only the window endpoint is tuned). This function:
      1. splits each config's windows into train/test by DATE (raises on any date overlap),
      2. tunes (config, endpoint) on the TRAIN windows only,
      3. reports ``error_stats`` on the TEST windows only, for the frozen (config, endpoint).
    The returned :class:`OOSReport` records the frozen choice and both date sets so the headline
    number is self-documenting and reproducible. THIS is the function the lead uses for the
    p95<$5 acceptance — never ``summarize``/``best_endpoint`` on the full set.
    """
    overlap = set(train_dates) & set(test_dates)
    if overlap:
        raise ValueError(f"train/test dates overlap (would leak): {sorted(overlap)}")

    train_by_cfg: dict[Hashable, list[WindowResult]] = {}
    test_by_cfg: dict[Hashable, list[WindowResult]] = {}
    for cfg, res in results_by_config.items():
        tr, te = split_by_date(res, train_dates, test_dates)
        train_by_cfg[cfg] = tr
        test_by_cfg[cfg] = te

    frozen_cfg, frozen_ep, train_stats = tune_endpoint_and_config(train_by_cfg, window_type)

    test_sel = [
        r for r in test_by_cfg.get(frozen_cfg, [])
        if r.window_type == window_type and r.endpoint == frozen_ep
    ]
    test_stats = error_stats(test_sel)
    n_train = sum(
        1 for r in train_by_cfg.get(frozen_cfg, [])
        if r.window_type == window_type and r.endpoint == frozen_ep
    )
    return OOSReport(
        frozen_config=frozen_cfg,
        frozen_endpoint=frozen_ep,
        window_type=window_type,
        train_dates=tuple(sorted(train_dates)),
        test_dates=tuple(sorted(test_dates)),
        n_train=n_train,
        n_test=len(test_sel),
        train_stats=train_stats,
        test_stats=test_stats,
    )


# ---------------------------------------------------------------------------------------
# Plotting (run by the lead on real results; matplotlib is NOT a hard dependency).
# ---------------------------------------------------------------------------------------
def plot_tracking_error(results: list[WindowResult], out_path: str, *,
                        window_type: str = "close", endpoint: str | None = None) -> str:
    """Write the 4-panel tracking-error figure (abs-error hist, error vs hour, error vs vol,
    error-vs-published scatter) for the tracking-error note. Imports matplotlib lazily so the
    package has no plotting dependency; raises ImportError with guidance if it is missing.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:  # pragma: no cover - plotting is a lead-run convenience
        raise ImportError("plot_tracking_error needs matplotlib (pip install matplotlib)") from e

    sel = [r for r in results if r.window_type == window_type
           and (endpoint is None or r.endpoint == endpoint) and r.abs_error is not None]
    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    abs_e = [float(r.abs_error) for r in sel]
    axes[0, 0].hist(abs_e, bins=40)
    axes[0, 0].set(title=f"|error| ({window_type}/{endpoint})", xlabel="$", ylabel="count")
    axes[0, 1].scatter([_hour_of_day(r.reference_time_ns) for r in sel],
                       [float(r.error) for r in sel], s=8)
    axes[0, 1].set(title="error vs UTC hour", xlabel="hour", ylabel="$")
    axes[1, 0].scatter([r.realized_vol for r in sel if r.realized_vol is not None],
                       [float(r.error) for r in sel if r.realized_vol is not None], s=8)
    axes[1, 0].set(title="error vs realized vol", xlabel="rvol", ylabel="$")
    axes[1, 1].scatter([float(r.published) for r in sel if r.published is not None],
                       [float(r.error) for r in sel if r.published is not None], s=8)
    axes[1, 1].set(title="error vs published level", xlabel="published $", ylabel="$")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path
