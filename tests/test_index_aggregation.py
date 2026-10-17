"""BRTI replica — per-stage + end-to-end tests against HAND-COMPUTED examples (ROADMAP 2.2 A).

Every expected number here is computed by hand in the docstring of its test, from a tiny book
small enough to work the brti-spec §3 algorithm by pen. These are the acceptance fixtures for the
replica's correctness.
"""
from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from kbtc.index import BrtiParams, DeviationFilter, compute_index
from kbtc.index.book import ASK, BID, AsOfBook, Level, SnapshotSeries
from kbtc.index.consolidate import _winsorize, consolidate, order_size_cap, uncross
from kbtc.index.curves import index_value, price_volume_curve, utilized_depth
from kbtc.index.filters import (
    R_DEVIATION,
    R_EMPTY_SIDE,
    R_NO_BOOK,
    R_SELF_CROSSED,
    R_STALE,
    R_UNSYNCED,
    median,
    prefilter_books,
)
from kbtc.index.params import NS_PER_S, active_constituents
from kbtc.index.spacing import dynamic_spacing, kde_spacing, level_spacing

NO_STALE = BrtiParams(stale_from_last_update=False)


def L(p: str, s: float) -> Level:
    return Level(Decimal(p), s)


# ---------------------------------------------------------------------------------------
# Consolidation + cap + uncross (§3.2, §3.7).
# ---------------------------------------------------------------------------------------
def test_consolidate_sums_sizes_across_venues_by_price():
    """Venue A ask (101,3) + Venue B ask (101,2) ⇒ consolidated ask (101,5); bids likewise."""
    a = AsOfBook("a", 1, True, (L("100", 3),), (L("101", 3),))
    b = AsOfBook("b", 1, True, (L("100", 2),), (L("101", 2),))
    bids, asks = consolidate([a, b])
    assert [(str(x.price), x.size) for x in bids] == [("100", 5.0)]
    assert [(str(x.price), x.size) for x in asks] == [("101", 5.0)]


def test_consolidate_caps_each_level_at_C_T():
    """A single consolidated level of 1005 BTC is capped to C_T=50 when a cap is passed."""
    a = AsOfBook("a", 1, True, (L("100", 1000),), (L("101", 1005),))
    bids, asks = consolidate([a], cap=50.0)
    assert asks[0].size == 50.0
    assert bids[0].size == 50.0


def test_order_size_cap_hand_trimmed_mean_plus_5sigma():
    """asks sizes [1,2,3] (prices 100/101/102, all within 5% of 100) + bids [1,2,3] →
    pool sorted [1,1,2,2,3,3], n=6, k=0 ⇒ trimmed mean=2.0, winsorized std(ddof=1)=sqrt(0.8).
    C_T = 2 + 5·0.894427 = 6.472136 (brti-spec §3.7 Eq 5)."""
    asks = [L("100", 1), L("101", 2), L("102", 3)]
    bids = [L("100", 1), L("99", 2), L("98", 3)]
    ct = order_size_cap(bids, asks, BrtiParams())
    assert ct == pytest.approx(2.0 + 5.0 * np.sqrt(0.8))


def test_winsorize_replaces_tails_not_trims():
    """_winsorize([1,2,3,4,5], k=1) replaces the extreme 1→2 and 5→4 ⇒ [2,2,3,4,4]."""
    out = _winsorize(np.asarray([1.0, 2, 3, 4, 5]), 1)
    assert list(out) == [2.0, 2.0, 3.0, 4.0, 4.0]


def test_order_size_cap_o4_lever_trimmed_vs_winsorized_differ():
    """O-4: with a right-skewed 100-level pool, the TRIMMED-mean cap (literal Eq 4e, default) is
    strictly below the WINSORIZED-mean cap — so the replica's cap reading is a real, switchable
    lever to close empirically against published BRTI, not a cosmetic one."""
    prices = [Decimal("100") + Decimal("0.01") * i for i in range(100)]  # all within 5%
    sizes = [1.0] * 98 + [500.0, 1000.0]
    asks = [Level(p, s) for p, s in zip(prices, sizes, strict=True)]
    ct = order_size_cap([], asks, BrtiParams(cap_mean="trimmed"))
    cw = order_size_cap([], asks, BrtiParams(cap_mean="winsorized"))
    assert ct is not None and cw is not None
    assert ct < cw


def test_uncross_consumes_min_size_until_separated():
    """bids [(101,5),(100,3)] vs asks [(100,2),(101,4)] crossing:
    consume 2@101/100 → bid(101,3),ask gone to (101,4); consume 3@101/101 → bid(101) gone,
    ask(101,1); now 100<101 → stop. Result bids [(100,3)], asks [(101,1)] (§3.2-2a)."""
    b = [L("101", 5), L("100", 3)]
    a = [L("100", 2), L("101", 4)]
    nb, na = uncross(b, a)
    assert [(str(x.price), x.size) for x in nb] == [("100", 3.0)]
    assert [(str(x.price), x.size) for x in na] == [("101", 1.0)]


# ---------------------------------------------------------------------------------------
# Dynamic spacing (§3.6).
# ---------------------------------------------------------------------------------------
def test_level_spacing_is_min_side_liquidity_over_tau():
    """s_lev = min(total_ask=200, total_bid=150)/100 = 1.5 (Eq 6a)."""
    assert level_spacing([L("1", 150)], [L("2", 200)], 100) == pytest.approx(1.5)


def test_kde_spacing_small_sample_returns_smallest():
    """n<4 ⇒ s_KDE = smallest sampled size (Eq 6j else-branch)."""
    assert kde_spacing([3.0, 2.0], BrtiParams()) == 2.0


def test_kde_spacing_degenerate_returns_mode_value():
    """A zero-variance sample's KDE mode is that value."""
    assert kde_spacing([4.0] * 5, BrtiParams()) == 4.0


def test_kde_spacing_grid_resolution_is_immaterial():
    """O-3: the argmax is invariant (<1e-3) to doubling the search-grid resolution."""
    samps = [1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0, 5.0, 1.02, 0.98] * 3
    a = kde_spacing(samps, BrtiParams(kde_grid_points=1024))
    b = kde_spacing(samps, BrtiParams(kde_grid_points=8192))
    assert abs(a - b) < 1e-3


def test_dynamic_spacing_is_min_of_candidates():
    """Equal sizes 20 over 1 level/side: s_lev=20/100=0.2, s_KDE=20 (n<4) ⇒ s_T=0.2 (Eq 7)."""
    bids = [L("100", 20)]
    asks = [L("101", 20)]
    assert dynamic_spacing(bids, asks, BrtiParams()) == pytest.approx(0.2)


# ---------------------------------------------------------------------------------------
# PV curves + utilized depth + exponential weighting (§3.3–3.5).
# ---------------------------------------------------------------------------------------
def test_pv_curve_and_utilized_depth_hand():
    """Consolidated asks [(101,2),(102,3)], bids [(100,2),(99,3)], s=1:
    v=1,2 → askPV=101,bidPV=100,mid=100.5, midSV=0.5/100.5=0.49751%≤0.5%;
    v=3 → askPV=102,bidPV=99,mid=100.5, midSV=1.5/100.5=1.4925%>0.5% (breach).
    ⇒ v_T = max(last-ok=2, s=1) = 2 (§3.4 Eq 2)."""
    cb = [L("100", 2), L("99", 3)]
    ca = [L("101", 2), L("102", 3)]
    curve = price_volume_curve(cb, ca, 1.0, BrtiParams())
    assert [p.v for p in curve] == [1.0, 2.0, 3.0]
    assert str(curve[0].ask_px) == "101" and str(curve[0].mid_px) == "100.5"
    assert curve[0].mid_sv == pytest.approx(0.5 / 100.5)
    assert curve[2].mid_sv == pytest.approx(1.5 / 100.5)
    assert utilized_depth(curve, 1.0, BrtiParams()) == 2.0


def test_utilized_depth_floors_to_s_when_first_point_breaches():
    """If even v=s breaches D, v_T floors to s and the index reduces to the best mid (§4.2)."""
    curve = price_volume_curve([L("100", 10)], [L("102", 10)], 0.1, BrtiParams())
    assert curve[0].mid_sv > BrtiParams().deviation_from_mid
    assert utilized_depth(curve, 0.1, BrtiParams()) == 0.1


def test_index_value_lambda_decays_to_3p6pct_at_vT():
    """VERIFIED O-2: λ=1/(0.3·v_T) ⇒ λ·v_T = 1/0.3 ≈ 3.333 and the exponential weight at the
    utilized depth is e^(−3.333) ≈ 3.6% of the top weight — the flat depth-weighting the spec
    pins down (NOT the old e^(−10.3))."""
    cb = [L("100", 2), L("99", 3)]
    ca = [L("101", 2), L("102", 3)]
    curve = price_volume_curve(cb, ca, 1.0, BrtiParams())
    v_T = utilized_depth(curve, 1.0, BrtiParams())
    _value, _n, lam = index_value(curve, v_T, BrtiParams())
    assert lam * v_T == pytest.approx(1.0 / 0.3)
    assert np.exp(-lam * v_T) == pytest.approx(0.0357, abs=1e-4)


# ---------------------------------------------------------------------------------------
# Contingency filters (§4/§5).
# ---------------------------------------------------------------------------------------
def test_prefilter_drops_each_erroneous_class():
    """no-book / unsynced / stale / empty-side / self-crossed each disregarded with its reason."""
    T = 100 * NS_PER_S
    books = {
        "nobook": AsOfBook("nobook", None, False, (), ()),
        "unsynced": AsOfBook("unsynced", T, False, (L("100", 1),), (L("101", 1),)),
        "stale": AsOfBook("stale", T - 11 * NS_PER_S, True, (L("100", 1),), (L("101", 1),)),
        "empty": AsOfBook("empty", T, True, (), (L("101", 1),)),
        "crossed": AsOfBook("crossed", T, True, (L("102", 1),), (L("101", 1),)),
        "ok": AsOfBook("ok", T, True, (L("100", 1),), (L("101", 1),)),
    }
    kept, dropped = prefilter_books(books, T, BrtiParams())
    assert set(kept) == {"ok"}
    assert dropped == {
        "nobook": R_NO_BOOK, "unsynced": R_UNSYNCED, "stale": R_STALE,
        "empty": R_EMPTY_SIDE, "crossed": R_SELF_CROSSED,
    }


def test_median_even_and_odd():
    assert median([Decimal("1"), Decimal("3"), Decimal("2")]) == Decimal("2")
    assert median([Decimal("1"), Decimal("3")]) == Decimal("2")


def test_deviation_filter_hysteresis_5pct_drop_2p5pct_reinstate():
    """§5.3/§5.3.4 with the literal median_base='all' (default): four anchor venues at 100 keep
    the all-venues median at 100 while c swings. c at 200 → dropped (100%); at 104 → STAYS
    dropped (4%, between 2.5–5%); at 102 → reinstated (2%, <2.5%). State persists across ticks."""
    anchors = {"a": Decimal("100"), "b": Decimal("100"), "d": Decimal("100"), "e": Decimal("100")}
    df = DeviationFilter(BrtiParams())  # median_base='all'
    k, _ = df.apply({**anchors, "c": Decimal("200")})
    assert set(k) == {"a", "b", "d", "e"} and df.dropped == {"c"}
    k, _ = df.apply({**anchors, "c": Decimal("104")})
    assert "c" not in k and df.dropped == {"c"}        # 4% ⇒ not yet reinstated
    k, _ = df.apply({**anchors, "c": Decimal("102")})
    assert "c" in k and df.dropped == set()            # 2% ⇒ reinstated


def test_deviation_filter_median_base_all_vs_active_differ():
    """O-9: the two median bases give DIFFERENT reinstatement with a dropped venue present.
    a=100 active, c dropped at 103: base 'all' → median([100,103])=101.5 ⇒ c dev 1.48% <2.5%
    ⇒ REINSTATED; base 'active' → median({a})=100 ⇒ c dev 3% >2.5% ⇒ STAYS dropped."""
    for base, reinstated in (("all", True), ("active", False)):
        df = DeviationFilter(BrtiParams(median_base=base))
        df.dropped.add("c")  # seed c as already dropped
        k, _ = df.apply({"a": Decimal("100"), "c": Decimal("103")})
        assert ("c" in k) is reinstated


# ---------------------------------------------------------------------------------------
# End-to-end compute_index (hand-computed values).
# ---------------------------------------------------------------------------------------
def test_compute_index_flat_book_hand():
    """Two venues, bid 100 / ask 101 (sizes 3 & 2). Consolidated 100×5 / 101×5; the half-spread
    is 0.4975% < 0.5% at every depth so v_T = full depth and, since every mid = 100.5, the
    depth-weighted mean is exactly 100.50 regardless of weights."""
    a = AsOfBook("a", 1_000, True, (L("100", 3),), (L("101", 3),))
    b = AsOfBook("b", 1_000, True, (L("100", 2),), (L("101", 2),))
    res = compute_index({"a": a, "b": b}, 1_000, NO_STALE)
    assert not res.gap
    assert res.value == Decimal("100.50")
    assert set(res.venues_used) == {"a", "b"}
    assert res.lam * res.v_T == pytest.approx(1.0 / 0.3)


def test_compute_index_floor_to_best_mid_hand():
    """Single venue bid 100 / ask 102: half-spread 0.990% > 0.5% at the first grid step, so
    v_T floors to s and the index is the best mid = 101.00."""
    a = AsOfBook("a", 1_000, True, (L("100", 10),), (L("102", 10),))
    res = compute_index({"a": a}, 1_000, NO_STALE)
    assert res.value == Decimal("101.00")
    assert res.n_grid == 1


def test_compute_index_subset_runs_on_single_constituent():
    """Constituent-agnostic: with only one recorded venue the replica still prints and records
    exactly which venue was used (the 2/3-constituent reality)."""
    a = AsOfBook("bitstamp", 1_000, True, (L("100", 5),), (L("101", 5),))
    res = compute_index({"bitstamp": a}, 1_000, NO_STALE)
    assert not res.gap and res.venues_used == ("bitstamp",)


def test_compute_index_gap_when_all_dropped():
    """All venues unsynced ⇒ calc failure ⇒ gap (no print), not an interpolated value (§5.5)."""
    a = AsOfBook("a", 1_000, False, (L("100", 1),), (L("101", 1),))
    res = compute_index({"a": a}, 1_000, NO_STALE)
    assert res.gap and res.value is None and res.reason == "all_stale_or_erroneous"


# ---------------------------------------------------------------------------------------
# Constituents + as-of reconstruction.
# ---------------------------------------------------------------------------------------
def test_active_constituents_itbit_july_2026_change():
    """itBit is a constituent strictly before 2026-07-27 and excluded on/after (§1)."""
    from kbtc.index.params import ITBIT_SUSPENDED_NS
    assert "itbit" in active_constituents(ITBIT_SUSPENDED_NS - 1)
    assert "itbit" not in active_constituents(ITBIT_SUSPENDED_NS)
    assert {"bitstamp", "coinbase", "kraken"} <= active_constituents(ITBIT_SUSPENDED_NS)


def test_asof_reconstruction_last_snapshot_tiebreak_on_snap_id():
    """as_of(T) takes the greatest (recv_ts_ns, snap_id) ≤ T; on a recv_ts tie the higher snap_id
    wins (schema.py load-bearing tie-break)."""
    rows = [
        {"recv_ts_ns": 10, "snap_id": 1, "synced": True, "side": BID, "price": Decimal("100"), "size": Decimal("1")},
        {"recv_ts_ns": 10, "snap_id": 1, "synced": True, "side": ASK, "price": Decimal("101"), "size": Decimal("1")},
        # same recv_ts_ns, higher snap_id ⇒ this is the state in effect
        {"recv_ts_ns": 10, "snap_id": 2, "synced": True, "side": BID, "price": Decimal("200"), "size": Decimal("1")},
        {"recv_ts_ns": 10, "snap_id": 2, "synced": True, "side": ASK, "price": Decimal("201"), "size": Decimal("1")},
        {"recv_ts_ns": 30, "snap_id": 3, "synced": True, "side": BID, "price": Decimal("300"), "size": Decimal("1")},
        {"recv_ts_ns": 30, "snap_id": 3, "synced": True, "side": ASK, "price": Decimal("301"), "size": Decimal("1")},
    ]
    s = SnapshotSeries.from_rows("v", rows)
    assert s.as_of(5).recv_ts_ns is None             # nothing at/before 5
    assert str(s.as_of(10).best_bid) == "200"        # tie broken to snap_id 2
    assert str(s.as_of(25).best_bid) == "200"        # still snap 2 until 30
    assert str(s.as_of(100).best_bid) == "300"       # latest


def test_asof_drops_nonpositive_entries_per_level():
    """A non-positive price/size entry is dropped individually (§5.2.2), not the whole book."""
    rows = [
        {"recv_ts_ns": 1, "snap_id": 1, "synced": True, "side": BID, "price": Decimal("100"), "size": Decimal("1")},
        {"recv_ts_ns": 1, "snap_id": 1, "synced": True, "side": BID, "price": Decimal("0"), "size": Decimal("1")},
        {"recv_ts_ns": 1, "snap_id": 1, "synced": True, "side": ASK, "price": Decimal("101"), "size": Decimal("0")},
        {"recv_ts_ns": 1, "snap_id": 1, "synced": True, "side": ASK, "price": Decimal("102"), "size": Decimal("2")},
    ]
    bk = SnapshotSeries.from_rows("v", rows).as_of(1)
    assert [str(x.price) for x in bk.bids] == ["100"]   # the price-0 bid dropped
    assert [str(x.price) for x in bk.asks] == ["102"]   # the size-0 ask dropped
