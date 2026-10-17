"""Tracking-error harness tests on synthetic settlement windows (ROADMAP 2.2 B).

A constant two-venue book over a 60 s window makes the replica print a known constant, so the
60 s average is exactly computable and the error vs a chosen "published" value is exact. Also
covers the window-endpoint offsets, gap handling, and the aggregation/breakdown functions.
"""
from __future__ import annotations

from decimal import Decimal

from kbtc.index import BrtiParams
from kbtc.index.book import SnapshotSeries
from kbtc.index.params import NS_PER_S
from kbtc.index.tracking import (
    OOSReport,
    WindowResult,
    as_price_decimal,
    best_endpoint,
    by_time_of_day,
    by_volatility,
    chronological_split,
    error_stats,
    evaluate_market,
    evaluate_oos,
    replica_window_average,
    result_date,
    split_by_date,
    summarize,
    window_sample_times_ns,
    worst_markets,
)

T0 = 1_760_000_060 * NS_PER_S  # an integer UTC second
DAY_NS = 86_400 * NS_PER_S


def _constant_series(venue: str, bid: str, ask: str, size: float = 10.0) -> SnapshotSeries:
    """One snapshot per second over [T0-60, T0] with a fixed book (lag=0 at every sample second)."""
    rows = []
    for i in range(61):
        t = T0 - (60 - i) * NS_PER_S
        rows += [
            {"recv_ts_ns": t, "snap_id": i, "synced": True, "side": "bid", "price": Decimal(bid), "size": Decimal(str(size))},
            {"recv_ts_ns": t, "snap_id": i, "synced": True, "side": "ask", "price": Decimal(ask), "size": Decimal(str(size))},
        ]
    return SnapshotSeries.from_rows(venue, rows)


def _two_venue_series() -> dict[str, SnapshotSeries]:
    return {"a": _constant_series("a", "100", "101"), "b": _constant_series("b", "100", "101")}


def test_window_sample_times_both_endpoints():
    """[T-59,T] = 60 prints ending AT T (inclusive); [T-60,T-1] = the 60 s strictly before T."""
    a = window_sample_times_ns(T0, "T-59..T")
    b = window_sample_times_ns(T0, "T-60..T-1")
    assert len(a) == len(b) == 60
    assert (T0 - a[0]) // NS_PER_S == 59 and (T0 - a[-1]) // NS_PER_S == 0
    assert (T0 - b[0]) // NS_PER_S == 60 and (T0 - b[-1]) // NS_PER_S == 1


def test_window_sample_times_snaps_reference_to_integer_second():
    """A reference time mid-second is snapped down to top-of-second before sampling."""
    ref = T0 + 500_000_000  # +0.5 s
    assert window_sample_times_ns(ref, "T-59..T")[-1] == T0


def test_replica_window_average_constant_book():
    """Constant 100/101 book ⇒ every 1 Hz print is 100.50 ⇒ 60 s average = 100.50, no gaps."""
    wa = replica_window_average(_two_venue_series(), T0, "T-59..T", BrtiParams())
    assert wa.average == Decimal("100.50")
    assert wa.n_used == 60 and wa.n_gap == 0
    assert wa.venues_used == ("a", "b")


def test_replica_window_average_gaps_when_no_book():
    """With no snapshots at/before the window seconds, every tick gaps and there is no average."""
    empty = {"a": SnapshotSeries.from_rows("a", [])}
    wa = replica_window_average(empty, T0, "T-59..T", BrtiParams())
    assert wa.average is None and wa.n_used == 0 and wa.n_gap == 60


def test_evaluate_market_zero_error_vs_matching_published():
    """Replica 60 s average 100.50 vs a published close value of 100.50 ⇒ error exactly 0 for
    both endpoints (the harness computes both, per O-5)."""
    sv = _two_venue_series()
    res = evaluate_market(
        sv, market_ticker="X", close_time_ns=T0, close_value=Decimal("100.50"),
        open_time_ns=None, open_value=None, params=BrtiParams(),
    )
    close = [r for r in res if r.window_type == "close"]
    assert {r.endpoint for r in close} == {"T-59..T", "T-60..T-1"}
    assert all(r.error == Decimal("0.00") and r.n_gap == 0 for r in close)


def test_evaluate_market_nonzero_error_is_signed_difference():
    """Published 100.40 vs replica 100.50 ⇒ error = +0.10 (replica − published)."""
    sv = _two_venue_series()
    res = evaluate_market(
        sv, market_ticker="X", close_time_ns=T0, close_value=Decimal("100.40"),
        open_time_ns=None, open_value=None, params=BrtiParams(),
    )
    close = [r for r in res if r.window_type == "close"]
    assert all(r.error == Decimal("0.10") for r in close)


def _mk(err: float | None, *, wtype="close", ep="T-59..T", ref=T0, rvol=None, gap=0) -> WindowResult:
    return WindowResult(
        market_ticker="m", reference_time_ns=ref, window_type=wtype, endpoint=ep,
        replica_avg=None if err is None else Decimal(str(100 + err)),
        published=Decimal("100"), error=None if err is None else Decimal(str(err)),
        n_used=60 - gap, n_gap=gap, venues_used=("a",), realized_vol=rvol,
    )


def test_error_stats_median_p95_max_and_coverage():
    """Hand stats over |errors| {1,2,3,4,100}: median 3, max 100; n_full counts the no-gap ones."""
    rs = [_mk(1), _mk(-2), _mk(3), _mk(-4, gap=5), _mk(100)]
    st = error_stats(rs)
    assert st.n == 5 and st.n_full == 4
    assert st.median_abs == 3.0 and st.max_abs == 100.0
    assert st.p95_abs > 4.0  # dominated by the 100 outlier


def test_error_stats_empty_is_none():
    st = error_stats([_mk(None)])
    assert st.n == 0 and st.median_abs is None


def test_summarize_and_best_endpoint_picks_lower_median():
    """best_endpoint returns, per window type, the endpoint with the smaller median |error|."""
    rs = [
        _mk(1, ep="T-59..T"), _mk(1, ep="T-59..T"),
        _mk(10, ep="T-60..T-1"), _mk(12, ep="T-60..T-1"),
    ]
    summ = summarize(rs)
    assert set(summ) == {("close", "T-59..T"), ("close", "T-60..T-1")}
    assert best_endpoint(rs) == {"close": "T-59..T"}


def test_worst_markets_sorts_by_abs_error():
    rs = [_mk(1), _mk(-50), _mk(3)]
    assert [float(r.abs_error) for r in worst_markets(rs, k=2)] == [50.0, 3.0]


def test_error_stats_carries_venues_used_coverage():
    """F4: the coverage (union of contributing constituents) is physically attached to the stats
    so a headline p95 can't be read without its '2/7 constituents' caveat."""
    rs = [
        WindowResult("m", T0, "close", "T-59..T", Decimal("100.5"), Decimal("100"),
                     Decimal("0.5"), 60, 0, ("bitstamp", "kraken"), None),
        WindowResult("m", T0, "close", "T-59..T", Decimal("100.5"), Decimal("100"),
                     Decimal("0.5"), 60, 0, ("bitstamp",), None),
    ]
    assert error_stats(rs).venues_used == ("bitstamp", "kraken")


def test_as_price_decimal_never_touches_float():
    """A Decimal passes through; a float/str goes via str() — never Decimal(float) noise."""
    assert as_price_decimal(Decimal("100.50")) == Decimal("100.50")
    assert as_price_decimal("100.50") == Decimal("100.50")
    # 100.1 as a binary float is 100.099999...; str()-routing keeps the intended decimal.
    assert as_price_decimal(100.1) == Decimal("100.1")
    assert as_price_decimal(None) is None


# ---------------------------------------------------------------------------------------
# Train/test discipline (F1).
# ---------------------------------------------------------------------------------------
def _dated(err: float, ep: str, ref_ns: int) -> WindowResult:
    return WindowResult("m", ref_ns, "close", ep, Decimal(str(100 + err)), Decimal("100"),
                        Decimal(str(err)), 60, 0, ("bitstamp", "kraken"), None)


def test_split_by_date_rejects_overlap():
    import pytest
    with pytest.raises(ValueError, match="overlap"):
        split_by_date([], {"2026-10-01"}, {"2026-10-01"})


def test_chronological_split_partitions_whole_dates_disjoint():
    """4 distinct dates, train_frac=0.5 ⇒ 2 earliest dates train, 2 latest test, disjoint."""
    rs = [_dated(1, "T-59..T", T0 + i * DAY_NS) for i in range(4)]
    train_d, test_d = chronological_split(rs, 0.5)
    assert len(train_d) == 2 and len(test_d) == 2
    assert not (train_d & test_d)
    assert max(train_d) < min(test_d)  # chronological (train is the earlier half)


def test_evaluate_oos_tunes_on_train_reports_on_test_only():
    """F1 acceptance: endpoint tuned on TRAIN (day1), where 'T-59..T' beats 'T-60..T-1'; the
    headline stats are computed ONLY over TEST (day2) windows of the frozen endpoint, and the
    frozen choice + date sets are recorded. The day2 'T-60..T-1' error (60) must NOT leak in."""
    d1 = T0
    d2 = T0 + DAY_NS
    train_d1 = result_date(_dated(0, "x", d1))
    test_d2 = result_date(_dated(0, "x", d2))
    results = [
        # TRAIN (day1): endpoint A small, endpoint B large ⇒ tune picks A.
        _dated(1, "T-59..T", d1), _dated(1, "T-59..T", d1),
        _dated(50, "T-60..T-1", d1),
        # TEST (day2): A errors {2,4}; B error 60 (must be excluded from the headline).
        _dated(2, "T-59..T", d2), _dated(4, "T-59..T", d2),
        _dated(60, "T-60..T-1", d2),
    ]
    report = evaluate_oos({"cfg0": results}, {train_d1}, {test_d2}, window_type="close")
    assert isinstance(report, OOSReport)
    assert report.frozen_endpoint == "T-59..T"           # tuned on TRAIN
    assert report.frozen_config == "cfg0"
    assert report.train_dates == (train_d1,) and report.test_dates == (test_d2,)
    # Headline computed over TEST day2 / frozen endpoint ONLY: errors {2,4}, NOT the 60.
    assert report.test_stats.n == 2
    assert report.test_stats.median_abs == 3.0
    assert report.test_stats.max_abs == 4.0
    assert report.test_stats.venues_used == ("bitstamp", "kraken")  # coverage attached


def test_evaluate_oos_rejects_overlapping_splits():
    import pytest
    with pytest.raises(ValueError, match="overlap"):
        evaluate_oos({"c": [_dated(1, "T-59..T", T0)]}, {"2026-10-01"}, {"2026-10-01"})


def test_breakdowns_by_time_of_day_and_volatility():
    """The time-of-day and volatility breakdowns bucket results and return per-bucket stats."""
    hour_ns = 3600 * NS_PER_S
    rs = [
        _mk(1, ref=0 * hour_ns, rvol=0.001),
        _mk(2, ref=0 * hour_ns, rvol=0.002),
        _mk(3, ref=12 * hour_ns, rvol=0.010),
        _mk(4, ref=12 * hour_ns, rvol=0.020),
    ]
    tod = by_time_of_day(rs)
    assert set(tod) == {0, 12}
    assert tod[0].n == 2
    vol = by_volatility(rs, n_buckets=2)
    assert sum(st.n for st in vol.values()) == 4
