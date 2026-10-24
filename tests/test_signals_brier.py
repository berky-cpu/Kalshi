"""Tests for the Brier-vs-Kalshi-mid harness (ROADMAP 3.2 acceptance tool).

Covers: loss functions, (tau,|z|) bucketing, the per-day-paired head-to-head gate (beats-mid vs
stop-and-rethink), the calibration table, Kalshi yes/no mid reconstruction, settlement->MarketSpec,
and the end-to-end per-market wiring on synthetic data."""
from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pytest

from kbtc.signals.brier import (
    DEFAULT_ABS_Z_EDGES,
    KalshiBookSeries,
    MarketSpec,
    Prediction,
    _bucket_label,
    brier_loss,
    build_market_predictions,
    calibration_table,
    log_loss,
    market_spec_from_settlement,
    score_predictions,
)
from kbtc.signals.fairvalue import IndexSeries

NS = 1_000_000_000
SIG = 0.60 / math.sqrt(365.0 * 24 * 3600)


# ----------------------------------------------------------------- loss functions
def test_brier_and_log_loss_values():
    p = np.array([1.0, 0.0, 0.5])
    y = np.array([1, 0, 1])
    assert brier_loss(p, y).tolist() == [0.0, 0.0, 0.25]
    ll = log_loss(p, y)
    assert ll[0] == pytest.approx(0.0, abs=1e-12)
    assert ll[2] == pytest.approx(-math.log(0.5))
    # clipping keeps a confident-wrong prediction finite (not inf)
    assert np.isfinite(log_loss(np.array([1.0]), np.array([0]))[0])


def test_bucket_label():
    assert _bucket_label(0.0, DEFAULT_ABS_Z_EDGES) == "[0,0.25)"
    assert _bucket_label(0.3, DEFAULT_ABS_Z_EDGES) == "[0.25,0.5)"
    assert _bucket_label(5.0, DEFAULT_ABS_Z_EDGES) == "[2,inf)"
    assert _bucket_label(-1.0, DEFAULT_ABS_Z_EDGES) is None  # |z| passed in, never negative


# ----------------------------------------------------------------- head-to-head gate
def _preds(model_ps, mid_ps, outcomes, dates):
    return [
        Prediction(date=d, market_ticker="M", origin_ns=i * NS, tau_s=300.0, z=0.3,
                   outcome=o, model_p=mp, mid_p=xp)
        for i, (mp, xp, o, d) in enumerate(zip(model_ps, mid_ps, outcomes, dates))
    ]


def test_gate_model_beats_mid():
    # model nails every outcome (Brier 0), mid is a coin flip -> model clearly wins OOS
    rng = np.random.default_rng(1)
    n = 200
    outcomes = rng.integers(0, 2, n).tolist()
    model_ps = [float(o) for o in outcomes]
    mid_ps = [0.5] * n
    dates = [f"2026-10-{18 + (i % 4):02d}" for i in range(n)]
    rep = score_predictions(_preds(model_ps, mid_ps, outcomes, dates))
    assert rep.head_to_head.brier_diff < 0
    assert rep.head_to_head.model_beats_mid
    assert "BEATS" in rep.gate_statement


def test_gate_stop_and_rethink_when_model_worse():
    rng = np.random.default_rng(2)
    n = 200
    outcomes = rng.integers(0, 2, n).tolist()
    model_ps = [0.5] * n            # model is a coin flip
    mid_ps = [float(o) for o in outcomes]  # mid nails it
    dates = [f"2026-10-{18 + (i % 4):02d}" for i in range(n)]
    rep = score_predictions(_preds(model_ps, mid_ps, outcomes, dates))
    assert rep.head_to_head.brier_diff > 0
    assert not rep.head_to_head.model_beats_mid
    assert "STOP-AND-RETHINK" in rep.gate_statement


def test_headline_excludes_in_window_f1():
    # F1: headline is tau>=60 origins only; in-window (0<tau<60) scored separately as a diagnostic
    preds = []
    for i in range(30):
        d = f"2026-10-{18 + (i % 3):02d}"
        preds.append(Prediction(d, "M", i * NS, tau_s=300.0, z=0.3, outcome=i % 2, model_p=0.6, mid_p=0.5))
        preds.append(Prediction(d, "M", (i + 1000) * NS, tau_s=30.0, z=0.3, outcome=i % 2,
                                model_p=0.6, mid_p=0.5))
    rep = score_predictions(preds)
    assert rep.tau_floor_s == 60.0
    assert rep.n == 30 and rep.head_to_head.n == 30            # headline = tau>=60 only
    assert rep.n_in_window == 30 and rep.in_window_head_to_head.n == 30  # in-window separate
    assert "tau >= 60" in rep.gate_statement


def test_day_gate_f3():
    # model perfect, mid a coin flip, CI clearly favours the model — but only 2 held-out days
    outcomes = [0, 1, 0, 1]
    preds = _preds([0.0, 1.0, 0.0, 1.0], [0.5] * 4, outcomes,
                   ["2026-10-18", "2026-10-18", "2026-10-19", "2026-10-19"])
    rep = score_predictions(preds)  # default min_days_for_win=3
    assert rep.head_to_head.brier_diff_ci[1] < 0       # CI upper bound < 0 (favours model)
    assert not rep.head_to_head.model_beats_mid        # F3: < 3 days -> vetoed, not auto-green-lit
    assert "NOT GREEN-LIT" in rep.gate_statement
    rep2 = score_predictions(preds, min_days_for_win=2)
    assert rep2.head_to_head.model_beats_mid           # bar lowered to 2 -> now a win


def test_gate_preliminary_label_for_few_days():
    outcomes = [0, 1, 0, 1]
    rep = score_predictions(_preds([0.0, 1.0, 0.0, 1.0], [0.5] * 4, outcomes,
                                   ["2026-10-18", "2026-10-18", "2026-10-19", "2026-10-19"]))
    assert "PRELIMINARY" in rep.gate_statement  # only 2 held-out days


def test_test_dates_restricts_scoring():
    preds = _preds([0.0, 1.0, 0.0, 1.0], [0.5] * 4, [0, 1, 0, 1],
                   ["2026-10-18", "2026-10-18", "2026-10-19", "2026-10-19"])
    rep = score_predictions(preds, test_dates=["2026-10-19"])
    assert rep.n == 2
    assert rep.test_dates == ("2026-10-19",)


# ----------------------------------------------------------------- bucketing + calibration
def test_buckets_populated_by_tau_and_z():
    preds = [
        Prediction("2026-10-18", "M", i * NS, tau_s=tau, z=z, outcome=1, model_p=0.6, mid_p=0.5)
        for i, (tau, z) in enumerate([(30.0, 0.1), (400.0, 1.5), (30.0, 0.1)])
    ]
    rep = score_predictions(preds)
    labels = {(b.tau_bucket, b.z_bucket): b.n for b in rep.by_bucket}
    assert labels[("[0,60)", "[0,0.25)")] == 2
    assert labels[("[300,600)", "[1,2)")] == 1


def test_calibration_table_recovers_frequency():
    # all preds at ~0.7; realized freq 0.7 -> that bin is well-calibrated
    rng = np.random.default_rng(3)
    n = 2000
    y = (rng.random(n) < 0.7).astype(int)
    preds = [Prediction("2026-10-18", "M", i * NS, 300.0, 0.3, int(y[i]), 0.7, 0.5) for i in range(n)]
    cal = calibration_table(preds, "model", n_bins=10)
    bin7 = next(c for c in cal if c.lo <= 0.7 < c.hi)
    assert bin7.n == n
    assert bin7.realized_freq == pytest.approx(0.7, abs=0.03)


# ----------------------------------------------------------------- Kalshi mid reconstruction
def _krow(recv_ns, snap_id, side, price, size, synced=True):
    return {"recv_ts_ns": recv_ns, "snap_id": snap_id, "synced": synced,
            "side": side, "price": Decimal(str(price)), "size": Decimal(str(size))}


def test_kalshi_yes_mid():
    rows = [
        _krow(100, 1, "yes", "0.40", "10"),  # best yes bid 0.40
        _krow(100, 1, "yes", "0.38", "5"),
        _krow(100, 1, "no", "0.55", "10"),   # best no bid 0.55 -> yes ask = 0.45
    ]
    ks = KalshiBookSeries.from_rows(rows)
    # mid = (0.40 + (1 - 0.55)) / 2 = (0.40 + 0.45)/2 = 0.425
    assert ks.yes_mid_at(100) == pytest.approx(0.425)
    assert ks.yes_mid_at(50) is None       # before any book


def test_kalshi_mid_as_of_and_missing_side():
    rows = [
        _krow(100, 1, "yes", "0.40", "10"),
        _krow(100, 1, "no", "0.55", "10"),
        _krow(200, 2, "yes", "0.60", "10"),   # later snapshot: only a yes side present
    ]
    ks = KalshiBookSeries.from_rows(rows)
    assert ks.yes_mid_at(150) == pytest.approx(0.425)  # as-of the first state
    assert ks.yes_mid_at(250) is None                  # newest state has no "no" side -> no mid


def test_kalshi_mid_require_synced():
    rows = [
        _krow(100, 1, "yes", "0.40", "10", synced=False),
        _krow(100, 1, "no", "0.55", "10", synced=False),
    ]
    assert KalshiBookSeries.from_rows(rows).yes_mid_at(100) is None  # unsynced dropped
    assert KalshiBookSeries.from_rows(rows, require_synced=False).yes_mid_at(100) == pytest.approx(0.425)


def test_crossed_and_locked_book_f4():
    # crossed: yes_bid 0.60 + no_bid 0.60 = 1.20 > 1 (yes bid above yes ask)
    crossed = [_krow(100, 1, "yes", "0.60", "10"), _krow(100, 1, "no", "0.60", "10")]
    ksc = KalshiBookSeries.from_rows(crossed)
    assert ksc.yes_mid_at(100) is None                                   # skip_crossed default
    assert ksc.yes_mid_at(100, skip_crossed=False) == pytest.approx(0.5)  # raw mid disclosed
    # locked: yes_bid 0.50 + no_bid 0.50 = 1.00 (zero spread) -> well-defined, kept
    locked = [_krow(100, 1, "yes", "0.50", "10"), _krow(100, 1, "no", "0.50", "10")]
    assert KalshiBookSeries.from_rows(locked).yes_mid_at(100) == pytest.approx(0.5)


def test_kalshi_mid_skips_nonpositive_levels():
    rows = [
        _krow(100, 1, "yes", "0.40", "0"),   # zero size -> ignored
        _krow(100, 1, "yes", "0.30", "10"),  # real best yes bid 0.30
        _krow(100, 1, "no", "0.55", "10"),
    ]
    ks = KalshiBookSeries.from_rows(rows)
    assert ks.yes_mid_at(100) == pytest.approx((0.30 + 0.45) / 2)


# ----------------------------------------------------------------- settlement -> MarketSpec
def test_market_spec_from_settlement_result():
    row = {"market_ticker": "KXBTC15M-X", "open_value": Decimal("82000.00"),
           "close_value": Decimal("82010.00"), "result": "yes",
           "open_time_ns": 0, "close_time_ns": 900 * NS}
    spec = market_spec_from_settlement(row)
    assert spec.strike == 82000.0 and spec.outcome == 1
    assert spec.open_time_ns == 0 and spec.close_time_ns == 900 * NS


def test_market_spec_fallback_to_close_vs_open():
    row = {"market_ticker": "M", "open_value": Decimal("82000"), "close_value": Decimal("81990"),
           "result": None, "open_time_ns": 0, "close_time_ns": 900 * NS}
    assert market_spec_from_settlement(row).outcome == 0  # close < open -> NO


def test_market_spec_none_when_missing():
    assert market_spec_from_settlement({"open_value": None, "open_time_ns": 0,
                                        "close_time_ns": 1, "result": "yes"}) is None


# ----------------------------------------------------------------- end-to-end per-market wiring
def test_build_market_predictions_end_to_end():
    close = 900 * NS
    spec = MarketSpec("M", strike=82000.0, outcome=1, open_time_ns=0, close_time_ns=close)
    # 1 Hz index, constant at the strike
    ts = np.arange(0, 901, dtype=np.int64) * NS
    px = np.full(ts.shape, 82000.0)
    idx = IndexSeries(ts, px, max_stale_s=5.0)
    rows = [_krow(0, 1, "yes", "0.49", "10"), _krow(0, 1, "no", "0.49", "10")]
    ks = KalshiBookSeries.from_rows(rows)
    preds = build_market_predictions(spec, idx, lambda t: SIG, ks, origin_spacing_s=30.0)
    assert len(preds) > 0
    # F6: tau==0 (the degenerate at-close knife-edge) is dropped entirely
    assert all(p.tau_s > 0 for p in preds)
    # both the headline (tau>=60) and the in-window diagnostic (0<tau<60) regimes are produced
    assert any(p.tau_s >= 60.0 for p in preds)
    assert any(0.0 < p.tau_s < 60.0 for p in preds)
    # constant index at strike -> ATM -> model P ~ 0.5 (tiny FP noise inside the window)
    for p in preds:
        assert p.outcome == 1
        assert p.mid_p == pytest.approx(0.5)
        assert p.model_p == pytest.approx(0.5, abs=1e-3)


def test_build_market_predictions_skips_when_no_mid_or_sigma():
    close = 900 * NS
    spec = MarketSpec("M", 82000.0, 1, 0, close)
    ts = np.arange(0, 901, dtype=np.int64) * NS
    idx = IndexSeries(ts, np.full(ts.shape, 82000.0), max_stale_s=5.0)
    empty = KalshiBookSeries.from_rows([])
    assert build_market_predictions(spec, idx, lambda t: SIG, empty) == []  # no Kalshi quotes
    rows = [_krow(0, 1, "yes", "0.49", "10"), _krow(0, 1, "no", "0.49", "10")]
    ks = KalshiBookSeries.from_rows(rows)
    assert build_market_predictions(spec, idx, lambda t: float("nan"), ks) == []  # no sigma


def test_build_market_predictions_directional_moneyness():
    # index well ABOVE the strike -> model should price YES high and beat a 0.5 mid in Brier
    close = 900 * NS
    spec = MarketSpec("M", strike=82000.0, outcome=1, open_time_ns=0, close_time_ns=close)
    ts = np.arange(0, 901, dtype=np.int64) * NS
    idx = IndexSeries(ts, np.full(ts.shape, 82300.0), max_stale_s=5.0)  # +$300 above strike
    rows = [_krow(0, 1, "yes", "0.50", "10"), _krow(0, 1, "no", "0.50", "10")]
    ks = KalshiBookSeries.from_rows(rows)
    preds = build_market_predictions(spec, idx, lambda t: SIG, ks, origin_spacing_s=30.0)
    assert all(p.model_p > 0.5 for p in preds)
    assert all(p.z > 0 for p in preds)
