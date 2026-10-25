"""Replay tests for the day-7 Kalshi TOP-OF-BOOK extractor (pipeline.stream_kalshi_tob).

The extractor tracks each side's BEST price INCREMENTALLY and emits slim level-0 ``kalshi_books``
rows. These tests gate correctness (NOT timing):

* the emitted top-of-book matches a BRUTE-FORCE best-price reference (fresh ``max`` every frame),
  including a delta that beats the best, a delete of the current best that forces a recompute, and
  a non-best delta that must NOT move the top;
* the output rows read back through ``KalshiBookSeries.from_rows`` reproduce the SAME mid as the
  slow full-depth ``stream_kalshi`` path (the harness must read the new output unchanged);
* ``synced`` is flagged False across a seq gap until the next snapshot re-anchors.

All prices are DOLLAR strings parsed to exact Decimal (never float), channel is ``market``.
"""
from __future__ import annotations

from decimal import Decimal

from kbtc.normalize import pipeline
from kbtc.recorders.kalshi import _dec
from kbtc.signals.brier import KalshiBookSeries

T0 = 1_760_000_000_000_000_000
TICKER = "KXBTC15M-26OCT16"
MKT_ID = "a1b2c3d4-0000-4000-8000-000000000001"


def _snap(sid, seq, yes=None, no=None):
    m = {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
         "market_ticker": TICKER, "market_id": MKT_ID}
    if yes is not None:
        m["yes_dollars_fp"] = [[p, s] for p, s in yes]
    if no is not None:
        m["no_dollars_fp"] = [[p, s] for p, s in no]
    return m


def _delta(sid, seq, price, delta_fp, side, ts_ms=0):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq, "market_ticker": TICKER,
            "market_id": MKT_ID, "price_dollars": price, "delta_fp": delta_fp, "side": side,
            "ts_ms": ts_ms}


def _records(msgs):
    for i, m in enumerate(msgs):
        yield T0 + i, m


# --------------------------------------------------------------------------------------
# Brute-force reference: apply deltas to plain dicts, recompute max fresh each frame, dedupe on
# the (yes,no) top-of-book, emit both sides. Independent of the incremental implementation.
# --------------------------------------------------------------------------------------
def _brute_force_tob(msgs):
    book = {"yes": {}, "no": {}}
    out = []  # (recv, yes_best_or_None, no_best_or_None) as (price, size) tuples
    last = None
    for i, m in enumerate(msgs):
        recv = T0 + i
        typ = m.get("type")
        if typ == "orderbook_snapshot":
            book = {"yes": {}, "no": {}}
            for key, side in (("yes_dollars_fp", "yes"), ("no_dollars_fp", "no")):
                for p, s in m.get(key, []):
                    book[side][_dec(p)] = _dec(s)
        elif typ == "orderbook_delta":
            side = m["side"]
            p = _dec(m["price_dollars"])
            new = book[side].get(p, Decimal(0)) + _dec(m["delta_fp"])
            if new <= 0:
                book[side].pop(p, None)
            else:
                book[side][p] = new
        else:
            continue
        best = {}
        for side in ("yes", "no"):
            if book[side]:
                bp = max(book[side])
                best[side] = (bp, book[side][bp])
            else:
                best[side] = None
        fp = (best["yes"], best["no"])
        if fp != last:
            last = fp
            out.append((recv, best["yes"], best["no"]))
    return out


def _tob_by_snap(rows):
    """Group emitted rows by snap_id → {snap_id: {"yes": (price,size)|None, "no": ...}}."""
    out: dict[int, dict] = {}
    for r in rows:
        g = out.setdefault(r["snap_id"], {"yes": None, "no": None, "recv": r["recv_ts_ns"]})
        assert r["level"] == 0  # TOB extractor only ever emits level 0
        g[r["side"]] = (r["price"], r["size"])
    return out


# The shared scenario exercising every incremental branch.
SCENARIO = [
    _snap(7, 1, yes=[["0.40", "100.00"], ["0.41", "250.00"]], no=[["0.58", "120.00"]]),
    _delta(7, 2, "0.40", "50.00", "yes"),    # below best 0.41 → NO top change, NO recompute
    _delta(7, 3, "0.42", "300.00", "yes"),   # BEATS: new best yes 0.42
    _delta(7, 4, "0.42", "-300.00", "yes"),  # REMOVES current best 0.42 → recompute → back to 0.41
    _delta(7, 5, "0.59", "10.00", "no"),     # new best no 0.59
    _delta(7, 6, "0.58", "-120.00", "no"),   # removes 0.58 (a NON-best no) → best no stays 0.59
]


def test_tob_matches_brute_force_reference():
    res = pipeline.normalize_kalshi_tob("market", _records(SCENARIO))
    emitted = _tob_by_snap(res.books)
    ref = _brute_force_tob(SCENARIO)

    # Same number of distinct TOB states, in the same order, with the same best price+size per side.
    states = [emitted[sid] for sid in sorted(emitted)]
    assert len(states) == len(ref), (len(states), len(ref))
    for st, (recv, y, n) in zip(states, ref):
        assert st["recv"] == recv
        assert st["yes"] == y
        assert st["no"] == n


def test_non_best_delta_emits_no_new_snapshot():
    # seq 2 (yes 0.40 add, below best 0.41) must NOT create a new snapshot; nor must seq 6 (removing
    # the non-best no level 0.58). The emitted snaps are exactly: snapshot, 0.42-beat, 0.42-remove,
    # 0.59-new-no = 4 states.
    res = pipeline.normalize_kalshi_tob("market", _records(SCENARIO))
    snap_ids = {r["snap_id"] for r in res.books}
    assert len(snap_ids) == 4


def test_best_values_through_scenario():
    res = pipeline.normalize_kalshi_tob("market", _records(SCENARIO))
    states = [v for _, v in sorted(_tob_by_snap(res.books).items())]
    # 1) snapshot: best yes 0.41, best no 0.58
    assert states[0]["yes"] == (Decimal("0.41"), Decimal("250.00"))
    assert states[0]["no"] == (Decimal("0.58"), Decimal("120.00"))
    # 2) yes 0.42 beats
    assert states[1]["yes"] == (Decimal("0.42"), Decimal("300.00"))
    # 3) 0.42 removed → recompute back to 0.41
    assert states[2]["yes"] == (Decimal("0.41"), Decimal("250.00"))
    # 4) new best no 0.59
    assert states[3]["no"] == (Decimal("0.59"), Decimal("10.00"))


def test_readback_mid_matches_full_depth_path():
    # The harness must read the slim output UNCHANGED: KalshiBookSeries mids from the TOB rows equal
    # those from the slow full-depth stream_kalshi at every record timestamp.
    tob = pipeline.normalize_kalshi_tob("market", _records(SCENARIO))
    full = pipeline.normalize_kalshi("market", _records(SCENARIO))
    s_tob = KalshiBookSeries.from_rows(tob.books)
    s_full = KalshiBookSeries.from_rows(full.books)
    for t in range(T0, T0 + len(SCENARIO) + 1):
        assert s_tob.yes_mid_at(t) == s_full.yes_mid_at(t)
    # And a concrete mid: after the snapshot, yes_bid=0.41, no_bid=0.58 → mid=(0.41+(1-0.58))/2=0.415
    assert s_tob.yes_mid_at(T0) == 0.5 * (0.41 + (1.0 - 0.58))


def test_synced_false_across_seq_gap_until_resnapshot():
    msgs = [
        _snap(7, 1, yes=[["0.40", "100.00"]], no=[["0.58", "120.00"]]),
        _delta(7, 3, "0.41", "10.00", "yes"),   # seq jumps 1→3 (missing 2): gap → diverged
        _snap(7, 10, yes=[["0.45", "50.00"]], no=[["0.60", "70.00"]]),  # re-anchor
        _delta(7, 11, "0.46", "5.00", "yes"),
    ]
    res = pipeline.normalize_kalshi_tob("market", _records(msgs))
    by_snap = _tob_by_snap(res.books)
    synced = {r["snap_id"]: r["synced"] for r in res.books}
    ids = sorted(by_snap)
    # First snapshot synced True.
    assert synced[ids[0]] is True
    # The post-gap state (if emitted) and everything up to the re-anchoring snapshot is False.
    reanchor = next(sid for sid in ids if by_snap[sid]["yes"] == (Decimal("0.45"), Decimal("50.00")))
    assert synced[reanchor] is True
    assert all(synced[sid] is False for sid in ids if sid < reanchor and sid != ids[0])


def test_sampling_cadence_throttles_but_keeps_as_of_monotonic():
    # With a cadence, at most one emit per window; the harness still reads a valid, monotonic series.
    res = pipeline.normalize_kalshi_tob("market", _records(SCENARIO), sample_every_ns=10)
    # Each emit is >= 10 ns after the previous (first always emits).
    recvs = sorted({r["recv_ts_ns"] for r in res.books})
    assert all(b - a >= 10 for a, b in zip(recvs, recvs[1:]))
    # Still a usable mid at the final time.
    s = KalshiBookSeries.from_rows(res.books)
    assert s.yes_mid_at(T0 + len(SCENARIO)) is not None
