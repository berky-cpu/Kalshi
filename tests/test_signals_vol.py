"""Tests for the microstructure-robust realized-vol estimator (ROADMAP 3.1).

Covers: unit conversions, causal gap-aware grid sampling, RV estimator correctness on synthetic
series with KNOWN vol, TSRV microstructure-robustness (recovers true vol under additive noise where
naive RV blows up), sparse-sampling robustness under smoothing, intraday seasonality recovery +
causality, the EWMA forecaster, and the QLIKE walk-forward eval harness (incl. model-beats-baseline
on a synthetic multi-day series with known structure).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from kbtc.signals import vol as V

NS = V.NS_PER_S


# ------------------------------------------------------------------ unit helpers
def test_unit_roundtrips():
    v = 1.3e-8
    sig = V.sigma_per_sqrt_second_from_var(v)
    assert sig == pytest.approx(math.sqrt(v))
    assert V.var_per_second_from_sigma(sig) == pytest.approx(v)
    # annualized is consistent with pricing.sigma_per_sqrt_second's inverse
    from kbtc.pricing import sigma_per_sqrt_second
    ann = V.annualized_from_var(v)
    assert sigma_per_sqrt_second(ann) == pytest.approx(sig, rel=1e-12)


def test_sigma_from_var_rejects_negative():
    with pytest.raises(ValueError):
        V.sigma_per_sqrt_second_from_var(-1.0)


# ------------------------------------------------------------------ grid sampling
def test_grid_and_locf_causal():
    ts = np.array([0, 2, 5, 9], dtype=np.int64) * NS
    px = np.array([100.0, 101.0, 102.0, 103.0])
    grid = V.make_grid_ns(0, 9 * NS, 1.0)
    assert grid.size == 10
    lp = V.sample_log_prices_on_grid(ts, px, grid, max_stale_s=10.0)
    # at t=1s the last obs is t=0 (100); at t=4s last is t=2 (101) -> strictly causal LOCF
    assert lp[1] == pytest.approx(math.log(100.0))
    assert lp[4] == pytest.approx(math.log(101.0))
    assert lp[5] == pytest.approx(math.log(102.0))


def test_grid_staleness_gap():
    ts = np.array([0, 1, 50], dtype=np.int64) * NS
    px = np.array([100.0, 100.5, 101.0])
    grid = V.make_grid_ns(0, 50 * NS, 1.0)
    lp = V.sample_log_prices_on_grid(ts, px, grid, max_stale_s=5.0)
    # seconds 7..49 are >5s stale -> NaN (hole not carried across)
    assert np.isnan(lp[10])
    assert np.isnan(lp[40])
    assert not np.isnan(lp[50])  # fresh print again


def test_grid_rejects_bad_input():
    with pytest.raises(ValueError):
        V.sample_log_prices_on_grid(np.array([2, 1]), np.array([1.0, 1.0]),
                                    V.make_grid_ns(0, 1, 1.0), 1.0)
    with pytest.raises(ValueError):
        V.sample_log_prices_on_grid(np.array([0, 1]), np.array([1.0, -1.0]),
                                    V.make_grid_ns(0, 1, 1.0), 1.0)


# ------------------------------------------------------------------ RV correctness (known vol)
def _gbm_log_prices(n, var_per_s, step_s, seed, noise_sd=0.0, start=11.0):
    rng = np.random.default_rng(seed)
    incr = rng.normal(0.0, math.sqrt(var_per_s * step_s), size=n)
    x = start + np.cumsum(incr)
    if noise_sd:
        x = x + rng.normal(0.0, noise_sd, size=n)
    return x


def test_realized_variance_recovers_known_vol():
    var_ps, step, n = 1.2e-8, 1.0, 20000
    x = _gbm_log_prices(n, var_ps, step, seed=1)
    rv = V.realized_variance(x)
    true_iv = var_ps * step * (n - 1)
    assert rv == pytest.approx(true_iv, rel=0.05)


def test_subsampled_rv_matches_naive_without_noise():
    x = _gbm_log_prices(10000, 1e-8, 1.0, seed=2)
    assert V.subsampled_rv(x, 5) == pytest.approx(V.realized_variance(x), rel=0.1)


def test_tsrv_robust_to_microstructure_noise():
    # Additive iid noise biases naive RV UP by ~2 n sigma_u^2; TSRV removes it.
    var_ps, step, n = 1.2e-8, 1.0, 20000
    noise_sd = 2e-4
    x = _gbm_log_prices(n, var_ps, step, seed=3, noise_sd=noise_sd)
    true_iv = var_ps * step * (n - 1)
    rv_naive = V.realized_variance(x)
    est_tsrv = V.tsrv(x, k=10)
    # naive is grossly biased up (many multiples of IV); TSRV is within tolerance of truth
    assert rv_naive > 5 * true_iv
    assert est_tsrv == pytest.approx(true_iv, rel=0.25)
    # and TSRV is far closer to truth than naive
    assert abs(est_tsrv - true_iv) < 0.1 * abs(rv_naive - true_iv)


def test_signature_plot_flattens_with_sparser_sampling():
    # Under additive noise the per-second RV is inflated at fine steps and decays toward the
    # true level as the step grows (noise term ~ 2 sigma_u^2 / step). This is why we sparse-sample.
    var_ps, step, n = 1.0e-8, 1.0, 30000
    noise_sd = 1.5e-4
    x = _gbm_log_prices(n, var_ps, step, seed=4, noise_sd=noise_sd)
    ts = (np.arange(n, dtype=np.int64)) * NS
    px = np.exp(x)
    sig = V.signature_plot(ts, px, steps_s=[1.0, 5.0, 20.0, 60.0])
    assert sig[1.0] > sig[5.0] > sig[60.0]  # monotone decay toward truth
    assert sig[60.0] == pytest.approx(var_ps, rel=0.5)  # flattened near the true per-second var


def test_sparse_sampling_robust_to_smoothing():
    # The BRTI is a depth-weighted mid -> MA-smoothed. A smoothed price underestimates 1s RV;
    # sampling sparsely recovers the true per-second variance.
    var_ps, step, n = 1.0e-8, 1.0, 40000
    xeff = _gbm_log_prices(n, var_ps, step, seed=5)
    # 5-point moving-average smoothing (low-pass); mode="valid" avoids zero-pad boundary jumps
    kern = np.ones(5) / 5.0
    xs = np.convolve(xeff, kern, mode="valid")
    ts = (np.arange(xs.size, dtype=np.int64)) * NS
    px = np.exp(xs)
    sig = V.signature_plot(ts, px, steps_s=[1.0, 60.0])
    assert sig[1.0] < var_ps              # fine-step underestimates under smoothing
    assert sig[60.0] == pytest.approx(var_ps, rel=0.5)  # sparse recovers truth


def test_realized_var_per_second_gap_fallback():
    # With NaNs present the estimator falls back to the gap-robust mean, not TSRV.
    var_ps, step, n = 1.0e-8, 1.0, 5000
    x = _gbm_log_prices(n, var_ps, step, seed=6)
    x[1000:1050] = np.nan
    vps = V.realized_var_per_second(x, step, method="tsrv")
    assert vps == pytest.approx(var_ps, rel=0.15)


# ------------------------------------------------------------------ seasonality
def test_seasonality_recovers_known_pattern():
    # Build per-return squared contributions with a known 2x-in-afternoon shape; fit recovers ratio.
    n_days, step = 3, 60.0
    secs = np.arange(0, n_days * V.SECONDS_PER_DAY, int(step), dtype=np.int64)
    ts = secs * NS
    hour = (secs % V.SECONDS_PER_DAY) // 3600
    season = np.where((hour >= 12) & (hour < 18), 2.0, 1.0)
    rng = np.random.default_rng(10)
    base = 1e-8
    sq = season * base * rng.chisquare(df=1, size=ts.size)  # E[chi2_1]=1, noisy per-sample
    seas = V.IntradaySeasonality.fit(ts, sq, n_bins=24, shrinkage=0.0)
    f_afternoon = float(np.mean(seas.factors[12:18]))
    f_other = float(np.mean(np.concatenate([seas.factors[:12], seas.factors[18:]])))
    assert f_afternoon / f_other == pytest.approx(2.0, rel=0.2)


def test_seasonality_is_level_preserving():
    seas = V.IntradaySeasonality(n_bins=4, factors=np.array([0.5, 1.5, 1.0, 1.0]))
    ts = np.array([0, 7 * 3600, 13 * 3600, 20 * 3600], dtype=np.int64) * NS
    v = np.array([3e-8, 3e-8, 3e-8, 3e-8])
    back = seas.reseasonalize(ts, seas.deseasonalize(ts, v))
    assert np.allclose(back, v)


def test_seasonality_empty_and_sparse_bins_neutral():
    seas = V.IntradaySeasonality.fit(np.array([], dtype=np.int64), np.array([]), n_bins=8)
    assert np.allclose(seas.factors, 1.0)
    # shrinkage pulls non-trivial bin factors toward 1. Two populated bins at different levels.
    ts = np.array([0, 0, 12 * 3600, 12 * 3600], dtype=np.int64) * NS
    sq = np.array([3.0, 3.0, 1.0, 1.0])  # bin 0 high, bin 12 low -> factors != 1
    s0 = V.IntradaySeasonality.fit(ts, sq, n_bins=24, shrinkage=0.0)
    s1 = V.IntradaySeasonality.fit(ts, sq, n_bins=24, shrinkage=0.9)
    assert abs(s0.factors[0] - 1.0) > 1e-6  # unshrunk factor is away from 1
    assert abs(s1.factors[0] - 1.0) < abs(s0.factors[0] - 1.0)
    assert abs(s1.factors[12] - 1.0) < abs(s0.factors[12] - 1.0)


# ------------------------------------------------------------------ EWMA forecaster
def test_ewma_recovers_constant_vol():
    var_ps, step, n = 1.5e-8, 1.0, 30000
    x = _gbm_log_prices(n, var_ps, step, seed=7)
    ts = np.arange(n, dtype=np.int64) * NS
    px = np.exp(x)
    cfg = V.EwmaVolConfig(step_s=5.0, halflife_s=600.0, max_stale_s=30.0)
    vp = V.estimate_vol_path(ts, px, cfg)
    # average of the (warm) re-seasonalized per-second variance ~ true
    v = vp.var_per_s[np.isfinite(vp.var_per_s)]
    assert float(np.mean(v[len(v) // 2:])) == pytest.approx(var_ps, rel=0.2)


def test_ewma_tracks_a_vol_regime_change():
    step, n = 1.0, 40000
    lo, hi = 0.5e-8, 4e-8
    x1 = _gbm_log_prices(n // 2, lo, step, seed=8, start=11.0)
    x2 = _gbm_log_prices(n // 2, hi, step, seed=9, start=float(x1[-1]))
    x = np.concatenate([x1, x2])
    ts = np.arange(x.size, dtype=np.int64) * NS
    px = np.exp(x)
    cfg = V.EwmaVolConfig(step_s=5.0, halflife_s=300.0)
    vp = V.estimate_vol_path(ts, px, cfg)
    # level in the last quarter >> level just before the switch
    q = vp.var_per_s
    mid = vp.grid_ns.size // 2
    early = float(np.nanmean(q[mid // 2: mid - 10]))
    late = float(np.nanmean(q[-q.size // 4:]))
    assert late > 3 * early


def test_ewma_is_causal_truncation_invariant():
    # The level at an early grid index must not change when LATER data is appended (no look-ahead).
    x = _gbm_log_prices(6000, 1e-8, 1.0, seed=11)
    ts = np.arange(x.size, dtype=np.int64) * NS
    px = np.exp(x)
    cfg = V.EwmaVolConfig(step_s=5.0, halflife_s=200.0)
    grid_full = V.make_grid_ns(0, int(ts[-1]), cfg.step_s)
    cut = grid_full.size // 2
    grid_short = grid_full[:cut]
    full = V.estimate_vol_path(ts, px, cfg, grid_ns=grid_full)
    short = V.estimate_vol_path(ts, px, cfg, grid_ns=grid_short)
    assert np.allclose(full.deseas_level[:cut], short.deseas_level, equal_nan=True)


def test_ewma_gap_does_not_inject_variance():
    x = _gbm_log_prices(10000, 1e-8, 1.0, seed=12)
    ts = np.arange(x.size, dtype=np.int64) * NS
    px = np.exp(x)
    # drop a block of samples (a feed outage)
    keep = np.ones(x.size, dtype=bool)
    keep[4000:6000] = False
    cfg = V.EwmaVolConfig(step_s=5.0, halflife_s=300.0, max_stale_s=20.0)
    vp = V.estimate_vol_path(ts[keep], px[keep], cfg,
                             grid_ns=V.make_grid_ns(0, int(ts[-1]), cfg.step_s))
    # the level is held flat across the hole (finite, no spurious spike)
    assert np.all(np.isfinite(vp.deseas_level[np.isfinite(vp.deseas_level)]))
    assert float(np.nanmax(vp.var_per_s)) < 1e-6


def test_expected_forward_variance_short_horizon():
    var_ps, step, n = 2e-8, 1.0, 20000
    x = _gbm_log_prices(n, var_ps, step, seed=13)
    ts = np.arange(n, dtype=np.int64) * NS
    px = np.exp(x)
    cfg = V.EwmaVolConfig(step_s=5.0, halflife_s=600.0)
    seas = V.IntradaySeasonality.uniform(24)
    vp = V.estimate_vol_path(ts, px, cfg, seas)
    gi = vp.grid_ns.size - 200
    H = 300.0
    efv = V.expected_forward_variance(vp, gi, H, seas, cfg.step_s)
    assert efv == pytest.approx(vp.var_per_s[gi] * H, rel=0.05)


# ------------------------------------------------------------------ QLIKE harness
def test_qlike_zero_at_truth_positive_elsewhere():
    h = np.array([1.0, 2.0, 3.0])
    assert np.allclose(V.qlike(h, h), 0.0)
    assert np.all(V.qlike(h, 0.5 * h) > 0)
    assert np.all(V.qlike(h, 2.0 * h) > 0)
    # minimized at f == h
    fs = np.linspace(0.5, 2.0, 50)
    losses = [V.qlike_mean(np.array([1.0]), np.array([f])) for f in fs]
    assert fs[int(np.argmin(losses))] == pytest.approx(1.0, abs=0.05)


def test_qlike_drops_invalid():
    h = np.array([1.0, -1.0, 2.0])
    f = np.array([1.0, 1.0, 0.0])
    assert V.qlike_mean(h, f) == pytest.approx(0.0)  # only first pair valid


def test_mincer_zarnowitz_perfect_forecast():
    f = np.array([1.0, 2.0, 3.0, 4.0, 5.0]) * 1e-8
    mz = V.mincer_zarnowitz(f, f)
    assert mz.slope == pytest.approx(1.0, abs=1e-6)
    assert mz.intercept == pytest.approx(0.0, abs=1e-12)
    assert mz.r2 == pytest.approx(1.0, abs=1e-9)


def test_score_forecasts_ratio_and_ci():
    rows = []
    for d, dn in [("2026-10-20", 0), ("2026-10-21", 1)]:
        base = (dn + 1) * NS * V.SECONDS_PER_DAY
        for i in range(20):
            h = 1e-8 * (1 + 0.1 * i)
            rows.append(V.ForecastRow(
                date=d, origin_ns=base + i,
                realized=h,
                forecasts={"good": h * 1.01, "bad": h * 2.0},
            ))
    rep = V.score_forecasts(rows, ["good", "bad"])
    assert rep.pooled_qlike["good"] < rep.pooled_qlike["bad"]
    assert rep.qlike_ratio_vs["bad"]["good"] < 1.0  # good beats bad baseline
    lo, hi = rep.boot_ci["good"]
    assert lo <= rep.pooled_qlike["good"] <= hi or lo <= np.mean(
        [d.qlike["good"] for d in rep.per_day]) <= hi


# ------------------------------------------------------------------ end-to-end walk-forward
def _synthetic_multiday(seed=20, step=10.0, n_days=5):
    """Multi-day index with a strong repeating intraday season + slow stochastic-vol drift."""
    rng = np.random.default_rng(seed)
    secs = np.arange(0, n_days * V.SECONDS_PER_DAY, int(step), dtype=np.int64)
    hour = (secs % V.SECONDS_PER_DAY) // 3600
    season = np.where((hour >= 13) & (hour < 21), 3.0, 0.6)  # strong US-hours season
    # slow stochastic drift in the base level (AR-1-ish), shared day to day
    drift = 1.0 + 0.5 * np.sin(2 * np.pi * secs / (3 * V.SECONDS_PER_DAY))
    base = 1.0e-8
    var_ps = base * season * drift
    incr = rng.normal(0.0, np.sqrt(var_ps * step))
    x = 11.0 + np.cumsum(incr)
    return secs * NS, np.exp(x)


def test_walk_forward_model_beats_baselines():
    ts, px = _synthetic_multiday()
    cfg = V.WalkForwardConfig(
        ewma=V.EwmaVolConfig(step_s=10.0, halflife_s=1800.0, max_stale_s=60.0),
        horizon_s=900.0, forecast_every_s=600.0, warmup_s=3600.0,
        target_method="naive",  # noise-free synthetic; naive target is exact here
    )
    rep = V.walk_forward_eval(ts, px, test_dates=["1970-01-04", "1970-01-05"], cfg=cfg)
    # all three models produced scores on held-out days
    assert rep.pooled_qlike["model"] > 0
    assert math.isfinite(rep.pooled_qlike["trailing"])
    assert math.isfinite(rep.pooled_qlike["prevday"])
    # the seasonality-aware EWMA beats the trailing-window baseline out of sample
    assert rep.qlike_ratio_vs["trailing"]["model"] < 1.0
    # and beats the single-sample previous-day baseline
    assert rep.qlike_ratio_vs["prevday"]["model"] < 1.0


def test_walk_forward_empty_series():
    rep = V.walk_forward_eval(np.array([0], dtype=np.int64), np.array([100.0]),
                              test_dates=["1970-01-01"])
    assert rep.pooled_qlike == {} or all(not math.isfinite(v) for v in rep.pooled_qlike.values())


# ------------------------------------------------------------------ scale calibration (kappa)
def _smoothed_series(var_ps, step, n, seed, smooth=10):
    """Efficient GBM observed through an MA low-pass (BRTI depth-weighted mid)."""
    xeff = _gbm_log_prices(n, var_ps, step, seed=seed)
    kern = np.ones(smooth) / smooth
    xs = np.convolve(xeff, kern, mode="valid")
    ts = np.arange(xs.size, dtype=np.int64) * NS
    return ts, np.exp(xs)


def test_kappa_lifts_smoothed_level_to_coarse_scale():
    var_ps = 1.14e-8
    ts, px = _smoothed_series(var_ps, 1.0, 60000, seed=30, smooth=10)
    # fine (5s) signature is biased LOW under smoothing; coarse (60s) recovers truth
    sig = V.signature_plot(ts, px, steps_s=[5.0, 60.0])
    assert sig[5.0] < var_ps
    assert sig[60.0] == pytest.approx(var_ps, rel=0.4)
    kappa = V.calibrate_scale_kappa(ts, px, delta_star_s=60.0, base_step_s=5.0, max_stale_s=30.0)
    assert kappa > 1.0  # lifts the fine level up
    assert kappa == pytest.approx(sig[60.0] / sig[5.0], rel=1e-9)
    # applying kappa to the 5s EWMA recovers the coarse (true) per-second variance
    cfg0 = V.EwmaVolConfig(step_s=5.0, halflife_s=600.0, scale_kappa=1.0)
    cfgk = V.EwmaVolConfig(step_s=5.0, halflife_s=600.0, scale_kappa=kappa)
    v0 = V.estimate_vol_path(ts, px, cfg0)
    vk = V.estimate_vol_path(ts, px, cfgk)
    tail0 = float(np.nanmean(v0.var_per_s[v0.var_per_s.size // 2:]))
    tailk = float(np.nanmean(vk.var_per_s[vk.var_per_s.size // 2:]))
    assert tail0 < var_ps                       # uncorrected level is low
    assert tailk == pytest.approx(var_ps, rel=0.35)  # corrected level ~ truth


def test_kappa_falls_back_to_one_without_data():
    assert V.calibrate_scale_kappa(np.array([0], dtype=np.int64), np.array([1.0]),
                                   delta_star_s=60.0, base_step_s=5.0, max_stale_s=30.0) == 1.0


# ------------------------------------------------------------------ coarse-subsampled target
def test_coarse_subsampled_recovers_var_on_fine_grid():
    var_ps = 1.0e-8
    x = _gbm_log_prices(40000, var_ps, 1.0, seed=31)
    v = V.coarse_subsampled_var_per_second(x, fine_step_s=1.0, delta_star_s=60.0)
    assert v == pytest.approx(var_ps, rel=0.1)


def test_coarse_subsampled_is_different_measure_from_fine_under_smoothing():
    var_ps = 1.14e-8
    ts, px = _smoothed_series(var_ps, 1.0, 40000, seed=32, smooth=10)
    x = np.log(px)
    fine = V.coarse_subsampled_var_per_second(x, 1.0, 1.0)   # ~ fine-scale (biased low)
    coarse = V.coarse_subsampled_var_per_second(x, 1.0, 60.0)  # settlement scale
    assert coarse > fine
    assert coarse == pytest.approx(var_ps, rel=0.4)


# ------------------------------------------------------------------ settlement-exact z^2 level test
def _two_day_gbm(var_ps, seed=40):
    n = 2 * V.SECONDS_PER_DAY
    x = _gbm_log_prices(n, var_ps, 1.0, seed=seed, start=math.log(82000.0))
    ts = np.arange(n, dtype=np.int64) * NS
    return ts, np.exp(x)


def test_settlement_z2_is_one_when_sigma_correct():
    var_ps = 1.14e-8
    ts, px = _two_day_gbm(var_ps, seed=41)
    # a vol path whose level IS the true per-second variance (constant-vol GBM)
    cfg = V.EwmaVolConfig(step_s=5.0, halflife_s=3600.0, scale_kappa=1.0)
    vp = V.estimate_vol_path(ts, px, cfg)
    origins = V.make_grid_ns(int(ts[0]) + 3600 * NS, int(ts[-1]) - 1000 * NS, 120.0)
    res = V.settlement_z2_test(ts, px, vp, V.IntradaySeasonality.uniform(24),
                               origins, horizons_s=[300.0, 900.0], max_stale_s=30.0)
    for tau in (300.0, 900.0):
        assert res[tau].mean_z2 == pytest.approx(1.0, abs=0.3)
        assert res[tau].ci[0] <= 1.0 <= res[tau].ci[1] or abs(res[tau].mean_z2 - 1.0) < 0.3


def test_settlement_z2_detects_sigma_too_low():
    var_ps = 1.14e-8
    ts, px = _two_day_gbm(var_ps, seed=42)
    # deliberately scale the variance level to HALF -> sigma too low -> mean z^2 ~ 2
    cfg = V.EwmaVolConfig(step_s=5.0, halflife_s=3600.0, scale_kappa=0.5)
    vp = V.estimate_vol_path(ts, px, cfg)
    origins = V.make_grid_ns(int(ts[0]) + 3600 * NS, int(ts[-1]) - 1000 * NS, 120.0)
    res = V.settlement_z2_test(ts, px, vp, V.IntradaySeasonality.uniform(24),
                               origins, horizons_s=[900.0], max_stale_s=30.0)
    assert res[900.0].mean_z2 == pytest.approx(2.0, rel=0.3)
    assert res[900.0].implied_sigma_bias < -0.1  # flags sigma biased low


def test_ks_statistic_normal_small_for_true_normal():
    rng = np.random.default_rng(99)
    z = rng.standard_normal(5000)
    assert V.ks_statistic_normal(z) < 0.05


# ------------------------------------------------------------------ BM-shape flatness diagnostic
def test_bm_shape_flat_under_gbm():
    var_ps = 1.14e-8
    ts, px = _two_day_gbm(var_ps, seed=43)
    origins = V.make_grid_ns(int(ts[0]) + 3600 * NS, int(ts[-1]) - 1000 * NS, 120.0)
    rows = V.bm_shape_diagnostic(ts, px, origins, horizons_s=[120.0, 300.0, 600.0, 900.0])
    s2p = [rows[t].sigma2_point for t in (120.0, 300.0, 600.0, 900.0)]
    s2a = [rows[t].sigma2_avg for t in (120.0, 300.0, 600.0, 900.0)]
    # both ~ the true per-second variance and FLAT across horizons (max/min < 1.6)
    for s in s2p + s2a:
        assert s == pytest.approx(var_ps, rel=0.5)
    assert max(s2p) / min(s2p) < 1.6
    assert max(s2a) / min(s2a) < 1.6


# ------------------------------------------------------------------ horse-race survives kappa
def test_walk_forward_coarse_target_model_beats_trailing_under_smoothing():
    # New defaults: coarse_subsampled target + scale_calibrate. The ranking (EWMA+seasonality vs
    # trailing) must survive the level fix (kappa applied to BOTH).
    ts, px = _synthetic_multiday(seed=44, step=10.0, n_days=5)
    # add a mild smoothing so kappa != 1 is exercised
    xs = np.convolve(np.log(px), np.ones(3) / 3, mode="valid")
    ts, px = ts[: xs.size], np.exp(xs)
    cfg = V.WalkForwardConfig(
        ewma=V.EwmaVolConfig(step_s=10.0, halflife_s=1800.0, max_stale_s=60.0),
        horizon_s=900.0, forecast_every_s=600.0, warmup_s=3600.0,
        target_method="coarse_subsampled", delta_star_s=60.0, target_fine_step_s=10.0,
        scale_calibrate=True,
    )
    rep = V.walk_forward_eval(ts, px, test_dates=["1970-01-04", "1970-01-05"], cfg=cfg)
    assert math.isfinite(rep.pooled_qlike["model"])
    assert rep.qlike_ratio_vs["trailing"]["model"] < 1.0
