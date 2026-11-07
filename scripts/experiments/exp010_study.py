"""Candidate-2 study driver: run the resting-NO held-to-settlement simulation over the downloaded
single-name/mention tapes and produce the adversarial report.

PRE-REGISTERED before looking at final numbers (see module docstring + the task):
  * Buckets: NO-price deciles [0,0.10),[0.10,0.20),...,[0.90,1.00).
  * Policy: passive resting NO at every traded NO level, held to settlement, maker-free.
  * GATE config (back-of-queue): queue_ahead=5, size=5 (the paper's ~5-contract touch depth),
    at-level fills, blocks excluded, fees exact (maker-free => $0 on our side).
  * Frictionless benchmark: queue_ahead=0, size=UNBOUNDED (== the paper's idealized fill).
  * Independent bootstrap unit: EVENT (one speech/earnings call => many word-markets), 2000 resamples.
  * KILL if ANY: gate net < +1c/contract; >70% of P&L in top-5% of markets; 2026 sub-sample <= 0.

Usage: uv run python notes/_candidate2_scripts/study.py KXTRUMPMENTION [KXELON... ]
Reads notes/_candidate2/{markets,trades}/<SERIES>.jsonl. Offline; read-only.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from kbtc.backtest.single_name import (  # noqa: E402
    UNBOUNDED,
    Trade,
    price_decile,
    simulate_resting_no,
    simulate_single_entry,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_candidate2"))
D = Decimal


def _ts_ns(iso: str) -> int:
    s = iso.replace("Z", "+00:00")
    return int(datetime.fromisoformat(s).timestamp() * 1e9)


def _year(iso: str) -> int:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).year


class MarketData:
    __slots__ = ("ticker", "event", "open_ns", "close_ns", "open_iso", "result_no", "trades")

    def __init__(self, m: dict):
        self.ticker = m["ticker"]
        self.event = m.get("event_ticker", self.ticker.rsplit("-", 1)[0])
        self.open_iso = m.get("open_time")
        self.open_ns = _ts_ns(m["open_time"]) if m.get("open_time") else 0
        self.close_ns = _ts_ns(m["close_time"]) if m.get("close_time") else 0
        self.result_no = (m.get("result") or "").strip().lower() == "no"
        self.trades: list[Trade] = []


def load(series_list: list[str]) -> list[MarketData]:
    mkts: dict[str, MarketData] = {}
    for series in series_list:
        mpath = os.path.join(ROOT, "markets", f"{series}.jsonl")
        with open(mpath) as f:
            for ln in f:
                m = json.loads(ln)
                if (m.get("result") or "").strip().lower() not in ("yes", "no"):
                    continue
                if not m.get("open_time") or not m.get("close_time"):
                    continue
                mkts[m["ticker"]] = MarketData(m)
        tpath = os.path.join(ROOT, "trades", f"{series}.jsonl")
        with open(tpath) as f:
            for ln in f:
                t = json.loads(ln)
                md = mkts.get(t["ticker"])
                if md is None:
                    continue
                md.trades.append(Trade(
                    ts_ns=_ts_ns(t["created_time"]),
                    taker_side=t["taker_side"],
                    no_price=D(t["no_price_dollars"]),
                    size=D(t["count_fp"]),
                    is_block=bool(t.get("is_block_trade")),
                ))
    return list(mkts.values())


def pct(x):
    return f"{100*x:.1f}%"


def c(x):  # dollars -> cents string
    return f"{100*float(x):+.3f}c"


# ----------------------------------------------------------------------------- bootstrap
def event_block_bootstrap(sum_by_event, cnt_by_event, *, n_boot=2000, seed=0):
    """Contract-weighted mean per-contract P&L (cents), resampling whole EVENTS (independent unit).
    Each event contributes (sum_pnl_dollars, n_contracts); pooled mean = Σsum/Σcnt. Returns
    (point_c, total_contracts, lo_c, hi_c)."""
    keys = [k for k in sum_by_event if cnt_by_event[k] > 0]
    if not keys:
        return (float("nan"), 0.0, float("nan"), float("nan"))
    sums = np.array([sum_by_event[k] for k in keys], float)
    cnts = np.array([cnt_by_event[k] for k in keys], float)
    point = sums.sum() / cnts.sum() * 100
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot)
    n = len(keys)
    for b in range(n_boot):
        pick = rng.integers(0, n, n)
        boot[b] = sums[pick].sum() / cnts[pick].sum() * 100
    return (point, float(cnts.sum()), float(np.quantile(boot, .025)), float(np.quantile(boot, .975)))


def run_config(mkts, *, queue_ahead, size, label, include_blocks=False, mode="multi"):
    """Run the sim for one (Q,size) config; return a dict of aggregates. Contract-weighted by the
    (fractional) filled size; the EVENT is the bootstrap block. ``mode="multi"`` rests at every
    traded level (at-level only, generous); ``mode="single"`` is the strictly-causal single-entry
    variant (first-trade price, incl. adverse trade-through)."""
    simfn = simulate_resting_no if mode == "multi" else simulate_single_entry
    sum_by_event = defaultdict(float)   # event -> Σ (edge * size)  [dollars]
    cnt_by_event = defaultdict(float)   # event -> Σ size           [contracts]
    dec_sum = defaultdict(float)        # decile -> Σ edge*size
    dec_cnt = defaultdict(float)        # decile -> Σ size
    dollar_per_market = []
    filled_markets = 0
    total_contracts = 0.0
    win_contracts = 0.0
    for md in mkts:
        r = simfn(md.ticker, md.trades, md.result_no,
                  queue_ahead=D(queue_ahead), size=size, include_blocks=include_blocks)
        if not r.fills:
            continue
        filled_markets += 1
        payoff = D(1) if md.result_no else D(0)
        mkt_dollars = 0.0
        for f in r.fills:
            edge = float(payoff - f.no_price)   # per-contract $ (maker-free => net)
            sz = float(f.size)
            contrib = edge * sz
            sum_by_event[md.event] += contrib
            cnt_by_event[md.event] += sz
            dec = price_decile(f.no_price)
            dec_sum[dec] += contrib
            dec_cnt[dec] += sz
            mkt_dollars += contrib
            total_contracts += sz
            if edge > 0:
                win_contracts += sz
        dollar_per_market.append(mkt_dollars)
    dm = np.asarray(dollar_per_market, float)
    total_net = dm.sum()
    k = max(1, int(np.ceil(0.05 * len(dm)))) if dm.size else 0
    sorted_desc = np.sort(dm)[::-1]
    top5_share = (sorted_desc[:k].sum() / total_net) if (dm.size and total_net != 0) else float("nan")
    gross_profit = dm[dm > 0].sum() if dm.size else 0.0
    top5_gross_share = (sorted_desc[:k].sum() / gross_profit) if gross_profit > 0 else float("nan")
    frac_neg_markets = (float((dm < 0).mean()) if dm.size else float("nan"))
    point, ncontr, lo, hi = event_block_bootstrap(sum_by_event, cnt_by_event)
    return {
        "label": label, "queue_ahead": queue_ahead, "size": str(size),
        "filled_markets": filled_markets, "n_contracts": int(round(total_contracts)),
        "total_contracts_frac": total_contracts,
        "mean_c_per_contract": point,
        "win_rate": (win_contracts / total_contracts if total_contracts else float("nan")),
        "boot_mean_c": point, "boot_lo_c": lo, "boot_hi_c": hi,
        "mean_dollar_per_market": (float(dm.mean()) if dm.size else float("nan")),
        "median_dollar_per_market": (float(np.median(dm)) if dm.size else float("nan")),
        "total_net_dollar": float(total_net),
        "top5pct_share": float(top5_share),
        "top5pct_gross_share": float(top5_gross_share),
        "frac_neg_markets": float(frac_neg_markets),
        "per_fill_decile": {d: (dec_sum[d] / dec_cnt[d] * 100, int(round(dec_cnt[d])))
                             for d in sorted(dec_cnt) if dec_cnt[d] > 0},
    }


def stylized_fact(mkts):
    yv = nv = 0.0
    yes_settle = 0
    n = 0
    for md in mkts:
        for t in md.trades:
            if t.is_block:
                continue
            if t.taker_side == "yes":
                yv += float(t.size)
            else:
                nv += float(t.size)
        n += 1
        yes_settle += 0 if md.result_no else 1
    tot = yv + nv
    return {"n_markets": n, "yes_taker_vol_share": (yv / tot if tot else float("nan")),
            "realized_yes_rate": (yes_settle / n if n else float("nan")),
            "yes_vol": yv, "no_vol": nv}


def walk_forward_calibration(mkts):
    """Reliability by NO-price decile using only PRIOR-closing markets (causal). For each market,
    using its VWAP NO price decile, predict NO-settlement from all markets that CLOSED before this
    one OPENED; report realized NO rate per decile (pooled, full sample) + a simple causal count of
    how often the prior-data expected edge was positive and the realized sign matched."""
    ms = sorted([m for m in mkts if m.trades], key=lambda m: m.open_ns)
    # pooled realized NO-rate by decile of each market's volume-weighted NO price
    by_dec = defaultdict(lambda: [0, 0])  # decile -> [no_count, total]
    rows = []
    prior = []  # (close_ns, decile, result_no)
    for m in ms:
        vol = sum((float(t.size) for t in m.trades if not t.is_block), 0.0)
        if vol <= 0:
            continue
        vwap_no = sum(float(t.no_price) * float(t.size) for t in m.trades if not t.is_block) / vol
        dec = price_decile(D(str(round(vwap_no, 4))))
        # causal prediction: NO-rate among prior-closed markets in same decile
        rel = [r for (cns, dd, r) in prior if cns <= m.open_ns and dd == dec]
        exp_no = (sum(rel) / len(rel)) if rel else None
        rows.append((dec, vwap_no, m.result_no, exp_no, len(rel)))
        by_dec[dec][0] += 1 if m.result_no else 0
        by_dec[dec][1] += 1
        prior.append((m.close_ns, dec, 1 if m.result_no else 0))
    # pooled reliability
    reliability = {d: (cnt[0] / cnt[1], cnt[1]) for d, cnt in sorted(by_dec.items())}
    return reliability, rows


def report(series_list, mkts, tag):
    print(f"\n{'='*78}\n{tag}  |  series={series_list}  |  settled markets={len(mkts)}\n{'='*78}")
    sf = stylized_fact(mkts)
    print(f"STYLIZED FACT: YES-taker volume share = {pct(sf['yes_taker_vol_share'])} "
          f"(yes_vol={sf['yes_vol']:.0f} no_vol={sf['no_vol']:.0f}); "
          f"realized YES-settlement rate = {pct(sf['realized_yes_rate'])} over {sf['n_markets']} markets")
    print("  (paper: takers buy YES ~61%, settles YES ~30.9%)\n")

    configs = [
        ("FRICTIONLESS (paper idealized: Q=0, unbounded)", 0, UNBOUNDED),
        ("Q=5 size=5  (GATE: ~5-contract touch depth)", 5, D(5)),
        ("Q=0 size=5  (front-of-queue, capacity 5)", 0, D(5)),
        ("Q=20 size=5 (deeper queue)", 20, D(5)),
        ("Q=5 size=20 (bigger maker)", 5, D(20)),
    ]
    results = {}
    for label, q, s in configs:
        r = run_config(mkts, queue_ahead=q, size=s, label=label)
        results[label] = r
        print(f"-- {label}")
        print(f"   filled markets={r['filled_markets']}  contracts(int)={r['n_contracts']}  "
              f"frac_contracts={r['total_contracts_frac']:.0f}")
        print(f"   net/contract (contract-wtd mean)={r['mean_c_per_contract']:+.3f}c  "
              f"win={pct(r['win_rate'])}")
        print(f"   event-block boot mean={r['boot_mean_c']:+.3f}c  "
              f"95% CI [{r['boot_lo_c']:+.3f}c, {r['boot_hi_c']:+.3f}c]")
        print(f"   $/market: mean=${r['mean_dollar_per_market']:+.2f}  "
              f"median=${r['median_dollar_per_market']:+.2f}  total=${r['total_net_dollar']:+.0f}")
        print(f"   concentration: top-5%-markets share of NET={pct(r['top5pct_share'])}  "
              f"share of GROSS profit={pct(r['top5pct_gross_share'])}  "
              f"markets net-negative={pct(r['frac_neg_markets'])}")
        dstr = "  ".join(f"d{d}:{v[0]:+.2f}c(n={v[1]})" for d, v in r["per_fill_decile"].items())
        print(f"   by NO-price decile (net c/contract): {dstr}")
        print()
    return results


def _month(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return f"{dt.year}-{dt.month:02d}"


def monthly_trend(mkts):
    """Gate-config (Q=5,size=5) net/contract + stylized fact by open-month (erosion check)."""
    by_month = defaultdict(list)
    for m in mkts:
        if m.open_iso:
            by_month[_month(m.open_iso)].append(m)
    print(f"\n{'='*78}\nMONTHLY TREND (gate Q=5/size=5; erosion check)\n{'='*78}")
    print(f"  {'month':8} {'n_mkt':>5} {'YESvolShr':>9} {'YESrate':>8} {'net c/ct':>9} {'median$/mkt':>11}")
    for mo in sorted(by_month):
        ms = by_month[mo]
        sf = stylized_fact(ms)
        r = run_config(ms, queue_ahead=5, size=D(5), label="m")
        print(f"  {mo:8} {len(ms):5d} {pct(sf['yes_taker_vol_share']):>9} "
              f"{pct(sf['realized_yes_rate']):>8} {r['boot_mean_c']:>+8.2f}c "
              f"${r['median_dollar_per_market']:>+9.2f}")


def report_single_entry(mkts, tag):
    """Strictly-causal single-entry (first-trade price, adverse trade-through) robustness check."""
    print(f"\n{'='*78}\nSINGLE-ENTRY CAUSAL ROBUSTNESS (first-trade price, incl. adverse pick-off) | {tag}\n{'='*78}")
    for label, q, s in [("Q=5 size=5 (gate-equiv)", 5, D(5)),
                        ("Q=0 size=5 (front)", 0, D(5)),
                        ("Q=20 size=5 (deeper queue)", 20, D(5))]:
        r = run_config(mkts, queue_ahead=q, size=s, label=label, mode="single")
        print(f"-- {label}: filled_mkts={r['filled_markets']} contracts={r['n_contracts']} "
              f"net/contract={r['boot_mean_c']:+.3f}c 95%CI[{r['boot_lo_c']:+.2f},{r['boot_hi_c']:+.2f}] "
              f"win={pct(r['win_rate'])}")
        print(f"   $/market mean=${r['mean_dollar_per_market']:+.2f} median=${r['median_dollar_per_market']:+.2f} "
              f"| top5%NET={pct(r['top5pct_share'])} mkts-neg={pct(r['frac_neg_markets'])}")
    return r


def main():
    series_list = sys.argv[1:] or ["KXTRUMPMENTION"]
    allm = load(series_list)
    print(f"loaded {len(allm)} settled markets with terminal result")

    full = allm
    dated = sorted([m for m in allm if m.open_ns], key=lambda m: m.open_ns)
    half = len(dated) // 2
    earlier = dated[:half]
    recent = dated[half:]  # most-recent half = the "recent post-growth sub-sample"

    rep_full = report(series_list, full, "FULL SAMPLE (all 2026 for this series)")
    report(series_list, earlier, "EARLIER HALF (by open date)")
    rep_recent = report(series_list, recent, "RECENT HALF (by open date) = recent sub-sample")
    report_single_entry(full, "FULL SAMPLE")
    report_single_entry(recent, "RECENT HALF")
    monthly_trend(full)

    # walk-forward reliability
    print(f"\n{'='*78}\nWALK-FORWARD RELIABILITY (pooled realized NO-rate by market VWAP-NO decile)\n{'='*78}")
    rel, _ = walk_forward_calibration(full)
    for d, (r, n) in rel.items():
        lo, hi = d / 10, (d + 1) / 10
        print(f"  decile {d} NO-price [{lo:.1f},{hi:.1f}): realized NO-rate={pct(r)}  (n={n} markets)")

    # ---- KILL CRITERIA VERDICT (gate config = Q=5 size=5) ----
    gate = "Q=5 size=5  (GATE: ~5-contract touch depth)"
    g = rep_full[gate]
    grec = rep_recent[gate]
    print(f"\n{'='*78}\nPRE-REGISTERED KILL-CRITERIA VERDICT (gate config: {gate})\n{'='*78}")
    k1 = g["boot_mean_c"] < 1.0
    k2 = (not np.isnan(g["top5pct_share"])) and g["top5pct_share"] > 0.70
    k3 = grec["boot_mean_c"] <= 0.0
    print(f"  (1) gate net/contract = {g['boot_mean_c']:+.3f}c  95%CI[{g['boot_lo_c']:+.2f},{g['boot_hi_c']:+.2f}]"
          f"  (point < +1c? {k1}; CI includes <=0? {g['boot_lo_c'] <= 0})")
    print(f"  (2) top-5%-market share of NET P&L (full) = {pct(g['top5pct_share'])}  (> 70%? {k2})")
    print(f"  (3) recent sub-sample net/contract = {grec['boot_mean_c']:+.3f}c "
          f"95%CI[{grec['boot_lo_c']:+.2f},{grec['boot_hi_c']:+.2f}]  (<= 0? {k3})")
    killed = k1 or k2 or k3
    print(f"\n  VERDICT: {'KILL — edge NOT real for us' if killed else 'SURVIVES all three'}  "
          f"(fired: {[i+1 for i,k in enumerate([k1,k2,k3]) if k]})")


if __name__ == "__main__":
    main()
