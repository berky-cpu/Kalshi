"""ROADMAP 4.2 maker-economic gate run (ADR-003). RUN ON THE BOX (clean+raw data live there).

Pre-registered HEADLINE config (NOT chosen post-hoc):
  * policy   = RestAtTouchPolicy (rest 1 lot at the current best bid on each side; join the touch),
               pull rules: no-quote when tau<180s AND |z|<1 (ADR-003 §5, P0a-widened).
  * fills    = back-of-queue, CONSERVATIVE cancels (the immune policy), submit/cancel latency 100ms.
  * index    = published CF BRTI feed (primary); sigma = committed 3.1 EWMA estimator (causal,
               kappa calibrated on each market's pre-open warmup). FV only gates the pull region.
  * metric   = net markout per fill at 5s / 30s / settlement, DAY-LEVEL block-bootstrap 95% CI.
  * benchmark= FRICTIONLESS fills (always-passive-at-touch, P0d limit) -> capture fraction.

This is the 4.2 RESULT. No ROADMAP tick; goes to red-team. No order path (offline).
"""
from __future__ import annotations

import argparse
import gc
import glob
import json
import math
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np

sys.path.insert(0, "/var/tmp/kbtc_src_day7/src")

import polars as pl

from kbtc.backtest import events as ev
from kbtc.backtest.engine import BacktestConfig, run_backtest
from kbtc.backtest.loader import (
    load_cf_index_ticks, load_market_records, market_from_settlement_row,
)
from kbtc.backtest.markout import markouts
from kbtc.backtest.policy import MarketState, RestAtTouchPolicy
from kbtc.backtest.queue import CancelPolicy, FillMode
from kbtc.backtest.report import day_block_bootstrap_mean
from kbtc.recorders.kalshi import KalshiProcessor
from kbtc.signals.fairvalue import (
    DEFAULT_WINDOW, IndexSeries, close_window_state, fair_value_up,
)
from kbtc.signals.vol import EwmaVolConfig, calibrate_scale_kappa, estimate_vol_path

NS = 1_000_000_000


def _date(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS, tz=UTC).strftime("%Y-%m-%d")


def make_fair_value_fn(idx: IndexSeries, sigma_at, strike: float, close_ns: int):
    """fair_value_fn(state)->(prob_up, delta_x, sigma); memoized to the integer second (CF is 1Hz)."""
    win = DEFAULT_WINDOW
    cache: dict[int, tuple] = {}

    def fn(state: MarketState):
        t = state.recv_ts_ns
        sec = t // NS
        hit = cache.get(sec)
        if hit is not None:
            return hit
        y = idx.price_at(t)
        sigma = sigma_at(t)
        res = (None, None, None)
        if math.isfinite(y) and y > 0 and math.isfinite(sigma) and sigma > 0:
            tau_s = max((close_ns - t) / NS, 0.0)
            try:
                st = None
                if tau_s < win.length_s:
                    st = close_window_state(idx.log_price_at, close_ns, t, win)
                dv = fair_value_up(y, strike, tau_s, sigma, state=st, window=win)
                res = (dv.prob_up, dv.delta_x, sigma)
            except Exception:
                res = (None, None, None)
        cache[sec] = res
        return res

    return fn


def _is_pulled(idx, sigma_at, strike, close_ns, t, pull_tau=180.0, pull_z=1.0):
    """Same pull rule the policy uses (tau<180 & |z|<1), evaluated standalone for the frictionless
    benchmark. Conservatively PULLED when FV can't be evaluated (matches RestAtTouchPolicy)."""
    tau = (close_ns - t) / NS
    if tau >= pull_tau:
        return False
    y = idx.price_at(t)
    s = sigma_at(t)
    if not (math.isfinite(y) and y > 0 and math.isfinite(s) and s > 0):
        return True
    try:
        st = close_window_state(idx.log_price_at, close_ns, t, DEFAULT_WINDOW) if tau < DEFAULT_WINDOW.length_s else None
        dv = fair_value_up(y, strike, tau, s, state=st, window=DEFAULT_WINDOW)
        from statistics import NormalDist
        z = NormalDist().inv_cdf(min(max(dv.prob_up, 1e-12), 1 - 1e-12))
    except Exception:
        return True
    return abs(z) < pull_z


def replay_qa(records, ticker, idx, sigma_at, strike, close_ns):
    """ONE bare-proc replay giving (a) trade->level QA (present-at-instant / within-2s — the
    90.5%/99.98% stability check) and (b) the FRICTIONLESS benchmark: lots we would capture if
    ALWAYS passive at the touch (P0d limit) = sum over our-ticker trades AT the current touch, not
    pulled, of min(size, 1) — matching the engine FRICTIONLESS 1-lot-per-event semantics."""
    proc = KalshiProcessor(product=ticker)
    total = inst = recent = 0
    fr_fills = 0
    fr_lots = 0.0
    last_pos = {}
    for ts, raw in records:
        proc.process(raw, ts)
        for e in ev.parse_kalshi_record(ts, raw, "\x00none"):
            if isinstance(e, ev.BookDelta) and e.ticker == ticker:
                if proc.books.get(ticker, {}).get(e.side, {}).get(e.price, 0) > 0:
                    last_pos[(e.side, e.price)] = ts
            elif isinstance(e, ev.Trade) and e.ticker == ticker:
                total += 1
                book = proc.books.get(ticker, {})
                size = book.get(e.consumed_side, {}).get(e.consumed_price, 0)
                if size > 0:
                    inst += 1
                    last_pos[(e.consumed_side, e.consumed_price)] = ts
                lp = last_pos.get((e.consumed_side, e.consumed_price))
                if size > 0 or (lp is not None and ts - lp <= 2 * NS):
                    recent += 1
                # frictionless: were WE at the touch on the consumed side (best bid) and not pulled?
                side_levels = book.get(e.consumed_side, {})
                best = max(side_levels) if side_levels else None
                if best is not None and e.consumed_price == best and not _is_pulled(
                        idx, sigma_at, strike, close_ns, ts):
                    fr_fills += 1
                    fr_lots += min(float(e.size), 1.0)
    return total, inst, recent, fr_fills, fr_lots


def run_one(spec, market_root, cf_root, cfg, warmup_s, market_warmup_s, tail_s):
    lo_sig = spec.open_time_ns - int(warmup_s * NS)
    hi = spec.close_time_ns + int(tail_s * NS)
    dates = sorted({_date(lo_sig), _date(spec.open_time_ns), _date(spec.close_time_ns), _date(hi)})

    cf = load_cf_index_ticks(cf_root, dates, lo_sig, spec.close_time_ns + 2 * NS)
    if len(cf) < 120:
        return None
    ts = np.asarray([t.recv_ts_ns for t in cf], dtype=np.int64)
    pxs = np.asarray([t.price for t in cf], dtype=np.float64)
    cfg_v = EwmaVolConfig()
    idx = IndexSeries(ts, pxs, max_stale_s=cfg_v.max_stale_s)
    pre = ts < spec.open_time_ns
    kappa = (calibrate_scale_kappa(ts[pre], pxs[pre], delta_star_s=60.0, base_step_s=cfg_v.step_s,
                                   max_stale_s=cfg_v.max_stale_s) if pre.sum() > 10 else 1.0)
    vp = estimate_vol_path(ts, pxs, replace(cfg_v, scale_kappa=kappa))
    grid = vp.grid_ns

    def sigma_at(t_ns: int) -> float:
        i = int(np.searchsorted(grid, t_ns, side="right")) - 1
        return vp.sigma_at(i) if i >= 0 else float("nan")

    lo_mkt = spec.open_time_ns - int(market_warmup_s * NS)
    records = load_market_records(market_root, dates, lo_mkt, hi)
    if not records:
        return None
    # engine index ticks = CF ticks within the trading window (requote triggers + index_level)
    eng_ticks = [t for t in cf if lo_mkt <= t.recv_ts_ns <= spec.close_time_ns]

    fvfn = make_fair_value_fn(idx, sigma_at, float(spec.strike), spec.close_time_ns)
    policy = RestAtTouchPolicy(size=__import__("decimal").Decimal(1))

    assert cfg.cancel_policy is CancelPolicy.CONSERVATIVE, "accepted path must be CONSERVATIVE"
    qres = run_backtest(spec, policy, records, index_ticks=eng_ticks, config=cfg, fair_value_fn=fvfn)

    mos = markouts(qres)
    m5 = [float(m.markout_5s) for m in mos if m.markout_5s is not None]
    m30 = [float(m.markout_30s) for m in mos if m.markout_30s is not None]
    sp = [float(m.settle_pnl_per_contract) for m in mos if m.settle_pnl_per_contract is not None]
    inv_path = [i for _, i in qres.inventory_path]
    total_t, inst_t, recent_t, fr_fills, fr_lots = replay_qa(
        records, spec.ticker, idx, sigma_at, float(spec.strike), spec.close_time_ns)

    out = {
        "ticker": spec.ticker, "day": _date(spec.close_time_ns),
        "queue_fills": qres.fill_count, "frictionless_fills": fr_fills,
        "queue_contracts": float(sum(float(f.size) for f in qres.fills)),
        "frictionless_contracts": float(fr_lots),
        "total_fee": float(qres.total_fee),
        "final_inventory": qres.final_inventory,
        "max_abs_inventory": max((abs(i) for i in inv_path), default=0),
        "m5": m5, "m30": m30, "settle_pc": sp,
        "map_total": total_t, "map_inst": inst_t, "map_recent": recent_t,
    }
    del records, qres, mos, idx, vp
    gc.collect()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-root", default="/opt/kbtc/data/raw/kalshi/market")
    ap.add_argument("--cf-root", default="/opt/kbtc/data/raw/kalshi/cfbenchmarks")
    ap.add_argument("--settle-raw-root", default="/opt/kbtc/data/raw/kalshi/settlement")
    ap.add_argument("--days", default="", help="comma dates (close day) to gate; default all CF-overlap")
    ap.add_argument("--warmup-s", type=float, default=1800.0)
    ap.add_argument("--market-warmup-s", type=float, default=120.0)
    ap.add_argument("--tail-s", type=float, default=30.0)
    ap.add_argument("--latency-ms", type=float, default=100.0)
    ap.add_argument("--max-markets", type=int, default=0, help="0 = all")
    ap.add_argument("--stride", type=int, default=1,
                    help="systematic 1-in-k market sample (by close time, spreads across days); 1=census")
    ap.add_argument("--out", default="/var/tmp/kbtc_src_day7/gate_result.json")
    ap.add_argument("--summarize", default="", help="summarize an existing result json and exit")
    args = ap.parse_args()

    if args.summarize:
        with open(args.summarize) as f:
            summarize(json.load(f))
        return

    cf_days = {d.split("date=")[-1] for d in glob.glob(f"{args.cf_root}/date=*")}
    mkt_days = {d.split("date=")[-1] for d in glob.glob(f"{args.market_root}/date=*")}
    overlap = sorted(cf_days & mkt_days)
    want = set(d for d in args.days.split(",") if d) or set(overlap)

    # Settlements from RAW envelopes (the clean parquet is stale); keep the last record per ticker.
    from kbtc.recorders.writer import read_records
    envs: dict[str, dict] = {}
    for d in sorted(set(overlap) | want):
        for p in sorted(glob.glob(f"{args.settle_raw_root}/date={d}/*.jsonl.zst")):
            for rec in read_records(p):
                try:
                    env = json.loads(rec["raw"])
                except (KeyError, ValueError):
                    continue
                tk = env.get("market_ticker")
                if isinstance(tk, str) and tk.startswith("KXBTC15M"):
                    envs[tk] = env  # last wins
    specs = [m for m in (market_from_settlement_row(e) for e in envs.values()) if m is not None]
    specs = [m for m in specs if _date(m.close_time_ns) in want
             and _date(m.close_time_ns) in overlap]
    specs.sort(key=lambda m: m.close_time_ns)
    if args.stride > 1:
        specs = specs[::args.stride]
    if args.max_markets:
        specs = specs[:args.max_markets]
    print(f"# 4.2 GATE: {len(specs)} markets over days {sorted({_date(m.close_time_ns) for m in specs})}",
          flush=True)

    lat = int(args.latency_ms * 1e6)
    cfg = BacktestConfig(submit_latency_ns=lat, cancel_latency_ns=lat,
                         fill_mode=FillMode.QUEUE, cancel_policy=CancelPolicy.CONSERVATIVE)

    rows = []
    for i, spec in enumerate(specs):
        try:
            r = run_one(spec, args.market_root, args.cf_root, cfg,
                        args.warmup_s, args.market_warmup_s, args.tail_s)
        except Exception as e:  # noqa: BLE001
            print(f"   !! {spec.ticker}: {type(e).__name__}: {e}", flush=True)
            r = None
        if r is not None:
            rows.append(r)
        if (i + 1) % 10 == 0:
            with open(args.out, "w") as f:  # incremental: pollable partial results
                json.dump(rows, f)
            print(f"   ... {i+1}/{len(specs)} markets, {len(rows)} usable, "
                  f"{sum(x['queue_fills'] for x in rows)} queue fills", flush=True)

    with open(args.out, "w") as f:
        json.dump(rows, f)
    summarize(rows)


def summarize(rows):
    if not rows:
        print("# NO usable markets"); return
    by_day_m5, by_day_m30, by_day_sp = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in rows:
        by_day_m5[r["day"]] += r["m5"]
        by_day_m30[r["day"]] += r["m30"]
        by_day_sp[r["day"]] += r["settle_pc"]
    days = sorted({r["day"] for r in rows})
    qf = sum(r["queue_fills"] for r in rows)
    ff = sum(r["frictionless_fills"] for r in rows)
    qc = sum(r["queue_contracts"] for r in rows)
    fc = sum(r["frictionless_contracts"] for r in rows)
    mt = sum(r["map_total"] for r in rows)
    mi = sum(r["map_inst"] for r in rows)
    mr = sum(r["map_recent"] for r in rows)

    print(f"\n# ===== 4.2 GATE RESULT ({len(rows)} markets, {len(days)} days: {days}) =====")
    print(f"# fees total = ${sum(r['total_fee'] for r in rows):.4f} (expect $0 — maker-free)")
    print(f"\n# FILLS: queue={qf} ({qc:.0f} lots)  frictionless={ff} ({fc:.0f} lots)  "
          f"capture-fraction (lots) = {qc/fc*100 if fc else 0:.1f}%")
    print("\n# NET MARKOUT PER FILL (day-level block bootstrap 95% CI):")
    for name, vbd in (("5s", by_day_m5), ("30s", by_day_m30), ("settlement(/contract)", by_day_sp)):
        pt, n, lo, hi = day_block_bootstrap_mean(vbd)
        verdict = "EDGE>0" if lo > 0 else ("NEG" if hi < 0 else "CI spans 0")
        print(f"   {name:22s} mean={pt*100:+.4f}c  95%CI=[{lo*100:+.4f}, {hi*100:+.4f}]c  n={n}  -> {verdict}")
    print("\n# TRADE->LEVEL QA (per gate, all days pooled):")
    print(f"   our-ticker trades={mt}  present-at-instant={mi/mt*100 if mt else 0:.2f}%  "
          f"present-or-within-2s={mr/mt*100 if mt else 0:.2f}%")
    print("\n# INVENTORY: final per market and max|inv|:")
    fin = defaultdict(int)
    for r in rows:
        fin[r["final_inventory"]] += 1
    print(f"   final-inventory distribution: {dict(sorted(fin.items()))}")
    print(f"   max |inventory| over all markets = {max(r['max_abs_inventory'] for r in rows)}")
    print("\n# per-day fills + mean 5s markout:")
    for d in days:
        df = [r for r in rows if r["day"] == d]
        qfd = sum(r["queue_fills"] for r in df)
        m5d = [x for r in df for x in r["m5"]]
        print(f"   {d}: markets={len(df)} queue_fills={qfd} "
              f"mean5s={np.mean(m5d)*100 if m5d else float('nan'):+.4f}c")
    print("\n# === HEADLINE config pre-registered; CONSERVATIVE/100ms/rest-at-touch. "
          "NOT a ROADMAP tick. ===")


if __name__ == "__main__":
    main()
