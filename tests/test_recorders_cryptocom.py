"""Replay test for the Crypto.com Exchange v1 ``book`` (L2) adapter (ROADMAP 1.1-series).

Drives the pure processor from a fixture built to the primary-doc schema (the v1
``book.{instrument_name}.{depth}`` channel page, read 2026-10-21) and CONFIRMED on the live wire
(``wss://stream.crypto.com/exchange/v1/market`` ``book.BTC_USD.50`` SNAPSHOT_AND_UPDATE, probed
from both the Ashburn-US and EU boxes 2026-10-21): a top-level-``asks``/``bids`` SNAPSHOT (full
depth-capped book), ``update``-bearing DELTAs applied in strict ``pu == prev.u`` order (incl. a
``"0"`` level removal), a ``public/heartbeat`` the processor must answer with
``public/respond-heartbeat``, a ``pu`` chain break (the VERIFIED gap signal) that forces an in-band
resubscribe resync, a dropped stale delta while awaiting the reseed, a fresh snapshot that rebuilds
the book, and the end-to-end normalizer completeness (with the ``t``-derived ``exch_ts_ns``).
"""
from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.base import ASK, BID
from kbtc.recorders.cryptocom import (
    CryptoComL2Processor,
    io_config,
    resubscribe_messages,
    subscribe_messages,
)
from kbtc.recorders.health import data_classifier, make_processor
from kbtc.normalize import pipeline

T0 = 1_760_000_000_000_000_000


def test_io_config_and_subscribe_envelope():
    cfg = io_config()
    assert cfg.urls == ["wss://stream.crypto.com/exchange/v1/market"]
    assert cfg.venue == "cryptocom" and cfg.channel == "book_btcusd"
    assert cfg.ping_interval_s is None  # server heartbeat (answered) + library ping keep it alive

    sub = subscribe_messages(nonce=111)
    assert sub == [{
        "id": 1, "method": "subscribe",
        "params": {"channels": ["book.BTC_USD.50"],
                   "book_subscription_type": "SNAPSHOT_AND_UPDATE",
                   "book_update_frequency": 100},
        "nonce": 111,
    }]
    # Resync forces a fresh snapshot by unsubscribe-then-subscribe (in-band, no REST).
    resub = resubscribe_messages(nonce=222)
    assert resub[0]["method"] == "unsubscribe"
    assert resub[0]["params"]["channels"] == ["book.BTC_USD.50"]
    assert resub[1]["method"] == "subscribe"
    # Fresh nonce per (re)connect via subscribe_factory (public — no credential involved).
    assert cfg.subscribe_factory is not None
    f1 = cfg.subscribe_factory()
    assert f1[0]["method"] == "subscribe" and f1[0]["params"]["channels"] == ["book.BTC_USD.50"]


def test_snapshot_seed_delta_apply_removal_and_best_bid_ask():
    msgs = load_fixture("cryptocom", "book.jsonl")
    p = CryptoComL2Processor()

    # msgs[0] SNAPSHOT (top-level asks/bids, no ``update``) → COMPLETE book, anchors ``u``.
    out = p.process(msgs[0], T0)
    assert out == [{"kind": "snapshot"}]
    assert p.synced and not p.gaps and p.last_u == 1000
    assert p.book.levels(BID) == [(Decimal("100.00"), Decimal("1.0")),
                                  (Decimal("99.00"), Decimal("2.0"))]
    assert p.book.levels(ASK) == [(Decimal("101.00"), Decimal("1.5")),
                                  (Decimal("102.00"), Decimal("2.0"))]
    assert p.book.best_bid() == Decimal("100.00")
    assert p.book.best_ask() == Decimal("101.00")

    # msgs[1] DELTA (pu==1000): adds bid 100.50, and qty "0" REMOVES bid 99.
    assert p.process(msgs[1], T0 + 1) == [{"kind": "book", "u": 1001}]
    assert p.book.bids[Decimal("100.50")] == Decimal("0.3")
    assert Decimal("99.00") not in p.book.bids
    assert p.book.best_bid() == Decimal("100.50")
    assert p.last_u == 1001 and not p.gaps


def test_heartbeat_queues_respond_and_is_not_data():
    """The server ``public/heartbeat`` must be answered with an id-echoing
    ``public/respond-heartbeat`` (queued on ``outbound``, drained+sent by the IO layer); it is not a
    book frame and never touches the book, and the data-classifier does NOT count it as DATA."""
    msgs = load_fixture("cryptocom", "book.jsonl")
    p = CryptoComL2Processor()
    p.process(msgs[0], T0)
    best_before = (p.book.best_bid(), p.book.best_ask())

    hb = msgs[2]
    assert hb["method"] == "public/heartbeat"
    assert p.process(hb, T0 + 2) == []           # not a book frame
    assert p.take_outbound() == [{"id": 42, "method": "public/respond-heartbeat"}]
    assert p.take_outbound() == []               # drained
    assert (p.book.best_bid(), p.book.best_ask()) == best_before and not p.gaps

    clf = data_classifier("cryptocom")
    assert clf(hb) is False                       # heartbeat is liveness, NOT data
    assert clf(msgs[0]) is True                   # a book frame IS data


def test_pu_chain_gap_then_in_band_reseed():
    msgs = load_fixture("cryptocom", "book.jsonl")
    p = CryptoComL2Processor()
    for i in range(4):            # snapshot, delta, heartbeat, delta
        p.process(msgs[i], T0 + i)
        p.take_outbound()
    assert p.synced and not p.gaps and p.last_u == 1002
    assert p.book.best_ask() == Decimal("101.00") and p.book.asks[Decimal("101.00")] == Decimal("2.0")

    # msgs[4]: pu=9999 != last_u=1002 ⇒ a dropped-message GAP (the VERIFIED pu-chain signal).
    # Logged (never silent), forces resync, delta DROPPED (not applied), book frozen.
    asks_before = dict(p.book.asks)
    out = p.process(msgs[4], T0 + 4)
    assert out == []
    assert len(p.gaps) == 1 and p.gaps[0].reason == "pu chain break"
    assert p.gaps[0].expected == 1002 and p.gaps[0].got == 9999
    assert p.needs_resnapshot is True and p.awaiting_snapshot is True
    assert dict(p.book.asks) == asks_before       # the gap delta was NOT applied

    # msgs[5]: a stale in-flight delta while awaiting the reseed is DROPPED, logs nothing more.
    assert p.process(msgs[5], T0 + 5) == []
    assert len(p.gaps) == 1 and p.awaiting_snapshot is True
    assert Decimal("104.00") not in p.book.asks

    # msgs[6]: the fresh reseed SNAPSHOT rebuilds the book and clears every flag, silently.
    out = p.process(msgs[6], T0 + 6)
    assert out == [{"kind": "snapshot"}]
    assert p.synced and p.needs_resnapshot is False and p.awaiting_snapshot is False
    assert p.book.bids == {Decimal("200.00"): Decimal("4.0")}
    assert p.book.asks == {Decimal("201.00"): Decimal("5.0")}
    assert p.last_u == 2000 and len(p.gaps) == 1   # the snapshot itself emits no gap

    # msgs[7]: delta after the reseed (pu==2000) applies normally.
    assert p.process(msgs[7], T0 + 7) == [{"kind": "book", "u": 2001}]
    assert p.book.bids == {Decimal("200.00"): Decimal("4.5")}


def test_begin_resync_drops_stale_and_logs_once():
    """``begin_resync`` (the IO layer) clears the book + arms awaiting-snapshot; a stale pre-reseed
    delta is DROPPED and a sustained pu break logs only ONE gap (no gap storm / resubscribe thrash).
    A stale delta can never be mistaken for the rebuild snapshot (it carries ``update``)."""
    p = CryptoComL2Processor()

    def book_frame(data0):
        return {"result": {"channel": "book", "subscription": "book.BTC_USD.50", "data": [data0]}}

    def snap(u, bid, ask):
        return book_frame({"bids": [[bid, "1.0", "1"]], "asks": [[ask, "1.0", "1"]], "u": u, "t": 1})

    def delta(u, pu, side, px, q):
        key = "bids" if side == BID else "asks"
        return book_frame({"update": {key: [[px, q, "1"]]}, "u": u, "pu": pu, "t": 2})

    p.process(snap(1000, "100.0", "101.0"), T0)
    assert p.synced and not p.gaps

    # A pu break: pu=500 != 1000 ⇒ one "pu chain break" gap, drop.
    p.process(delta(1001, 500, ASK, "102.0", "1.0"), T0 + 1)
    assert len(p.gaps) == 1 and p.needs_resnapshot is True and p.awaiting_snapshot is True
    # A SECOND broken delta before the reseed must NOT log again.
    p.process(delta(1002, 501, ASK, "103.0", "1.0"), T0 + 2)
    assert len(p.gaps) == 1

    # IO layer resyncs: begin_resync clears the book, resets the chain, arms awaiting-snapshot.
    p.begin_resync()
    assert p.awaiting_snapshot is True and not p.book.bids and not p.book.asks and p.last_u is None

    # Stale in-flight delta during the wait is dropped, not applied.
    assert p.process(delta(1003, 1002, BID, "99.0", "1.0"), T0 + 3) == []
    assert not p.book.bids and len(p.gaps) == 1

    # Fresh reseed snapshot rebuilds and clears everything.
    p.process(snap(2000, "200.0", "201.0"), T0 + 4)
    assert p.synced and p.awaiting_snapshot is False and p.needs_resnapshot is False
    assert p.book.best_bid() == Decimal("200.0") and p.book.best_ask() == Decimal("201.0")
    assert len(p.gaps) == 1


def test_reconnect_reseed_snapshot_rebuilds_without_begin_resync():
    """On a plain reconnect the processor is reused (still synced with a stale book). A fresh
    snapshot (no ``update``, no ``pu``) resets and rebuilds it silently — no spurious gap."""
    def snap(u, bid, ask):
        return {"result": {"channel": "book", "data": [
            {"bids": [[bid, "1.0", "1"]], "asks": [[ask, "1.0", "1"]], "u": u, "t": 1}]}}
    p = CryptoComL2Processor()
    p.process(snap(1000, "100.0", "101.0"), T0)
    assert p.synced and p.book.best_bid() == Decimal("100.0")
    p.process(snap(5000, "500.0", "501.0"), T0 + 1)   # reconnect reseed
    assert p.synced and not p.gaps and p.last_u == 5000
    assert p.book.bids == {Decimal("500.0"): Decimal("1.0")}
    assert p.book.asks == {Decimal("501.0"): Decimal("1.0")}


def _book_frame(data0):
    return {"result": {"channel": "book", "subscription": "book.BTC_USD.50", "data": [data0]}}


def _snapshot(u, bids, asks):
    return _book_frame({"bids": bids, "asks": asks, "u": u, "t": 1})


def _delta(u, pu, bids=None, asks=None):
    upd = {}
    if bids is not None:
        upd["bids"] = bids
    if asks is not None:
        upd["asks"] = asks
    return _book_frame({"update": upd, "u": u, "pu": pu, "t": 2})


def test_trim_evicts_depth_window_ghost(monkeypatch):
    """REV-029 F1: a depth-capped feed can push an old level OUT of the top-N window WITHOUT a
    qty-0 remove (it simply stops transmitting it). Absolute-SET + no trim would keep it as a
    ghost that resurfaces as a false best_bid/ask. The shared-path trim must evict it.

    Driven at depth 3 so the window is small and the ghost obvious; the real feed uses 50."""
    p = CryptoComL2Processor(depth=3)

    # Seed a full 3-level book on each side (already at the window cap). Asks sit far above bids so
    # the moves below stay uncrossed and the ghost is unambiguous.
    snap = _snapshot(
        1000,
        bids=[["100.00", "1.0", "1"], ["99.00", "1.0", "1"], ["98.00", "1.0", "1"]],
        asks=[["200.00", "1.0", "1"], ["201.00", "1.0", "1"], ["202.00", "1.0", "1"]],
    )
    assert p.process(snap, T0) == [{"kind": "snapshot"}]
    assert len(p.book.bids) == 3 and len(p.book.asks) == 3

    # BID ghost: market ticks UP. New higher bids 105/104 arrive (as absolute SET), but the venue
    # does NOT send a qty-0 remove for the old low bids 99/98 — they simply fall out of the top-3
    # window (become rank 4/5). Without a trim they would linger as ghosts.
    d1 = _delta(1001, 1000, bids=[["105.00", "1.0", "1"], ["104.00", "1.0", "1"]])
    assert p.process(d1, T0 + 1) == [{"kind": "book", "u": 1001}]
    assert len(p.book.bids) <= 3
    assert Decimal("99.00") not in p.book.bids and Decimal("98.00") not in p.book.bids
    assert p.book.levels(BID) == [(Decimal("105.00"), Decimal("1.0")),
                                  (Decimal("104.00"), Decimal("1.0")),
                                  (Decimal("100.00"), Decimal("1.0"))]

    # ASK ghost: market ticks DOWN. New lower asks 150/151 arrive; old high asks 201/202 fall out
    # of the top-3 window with no qty-0 remove.
    d2 = _delta(1002, 1001, asks=[["150.00", "1.0", "1"], ["151.00", "1.0", "1"]])
    assert p.process(d2, T0 + 2) == [{"kind": "book", "u": 1002}]
    assert len(p.book.asks) <= 3
    assert Decimal("201.00") not in p.book.asks and Decimal("202.00") not in p.book.asks
    assert p.book.levels(ASK) == [(Decimal("150.00"), Decimal("1.0")),
                                  (Decimal("151.00"), Decimal("1.0")),
                                  (Decimal("200.00"), Decimal("1.0"))]

    # Top-of-book is uncrossed and reflects the real window (no ghost resurfacing as a false best).
    assert p.book.best_bid() == Decimal("105.00")
    assert p.book.best_ask() == Decimal("150.00")


def test_trim_does_not_drop_in_window_levels():
    """A normal ≤depth book is left completely untouched by the trim (no real liquidity dropped)."""
    p = CryptoComL2Processor(depth=50)
    snap = _snapshot(
        1000,
        bids=[["100.00", "1.0", "1"], ["99.00", "2.0", "1"]],
        asks=[["101.00", "1.5", "1"], ["102.00", "2.0", "1"]],
    )
    p.process(snap, T0)
    assert p.book.levels(BID) == [(Decimal("100.00"), Decimal("1.0")),
                                  (Decimal("99.00"), Decimal("2.0"))]
    assert p.book.levels(ASK) == [(Decimal("101.00"), Decimal("1.5")),
                                  (Decimal("102.00"), Decimal("2.0"))]
    # A delta that keeps the book at/under the window changes nothing extra.
    p.process(_delta(1001, 1000, bids=[["100.50", "0.3", "1"]]), T0 + 1)
    assert len(p.book.bids) == 3 and len(p.book.asks) == 2
    assert p.book.best_bid() == Decimal("100.50") and p.book.best_ask() == Decimal("101.00")


def test_trim_shared_path_live_matches_normalize():
    """The trim lives in the processor reconstruction path, which the offline normalize REUSES,
    so a live-reconstructed book and an offline-normalized book agree on top-of-book after a
    window-eviction sequence (the Bitstamp lesson: one reconstruction path)."""
    depth = 3
    snap = _snapshot(
        1000,
        bids=[["100.00", "1.0", "1"], ["99.00", "1.0", "1"], ["98.00", "1.0", "1"]],
        asks=[["200.00", "1.0", "1"], ["201.00", "1.0", "1"], ["202.00", "1.0", "1"]],
    )
    # Up-move: old low bids 99/98 fall out of the window without a qty-0 remove (ghosts).
    d = _delta(1001, 1000, bids=[["105.00", "1.0", "1"], ["104.00", "1.0", "1"]])

    # LIVE processor path.
    live = CryptoComL2Processor(depth=depth)
    live.process(snap, T0)
    live.process(d, T0 + 1)
    live_bids = live.book.levels(BID)
    live_asks = live.book.levels(ASK)
    assert live_bids[0][0] == Decimal("105.00") and live_asks[0][0] == Decimal("200.00")

    # OFFLINE normalize path: same processor class, reused via the pipeline. The FINAL emitted
    # snapshot (highest snap_id) must agree with the live book — one reconstruction path.
    records = [(T0, snap), (T0 + 1, d)]
    res = pipeline.normalize_crypto("cryptocom", "book_btcusd", records, levels=depth)
    final_id = max(r["snap_id"] for r in res.books)
    final = [r for r in res.books if r["snap_id"] == final_id]
    final_bid0 = next(r for r in final if r["side"] == BID and r["level"] == 0)
    final_ask0 = next(r for r in final if r["side"] == ASK and r["level"] == 0)
    assert final_bid0["price"] == live_bids[0][0] == Decimal("105.00")
    assert final_ask0["price"] == live_asks[0][0] == Decimal("200.00")
    # The ghost bids (99/98) are absent from the FINAL normalized snapshot (they only, correctly,
    # appear in the initial snapshot where they were still in-window).
    assert all(r["price"] not in (Decimal("99.00"), Decimal("98.00"))
               for r in final if r["side"] == BID)


def test_price_level_book_trim_unit():
    """PriceLevelBook.trim keeps the best-N per side, slicing the far end; ≤N is a no-op."""
    from kbtc.recorders.base import PriceLevelBook

    b = PriceLevelBook()
    for px in ("100", "99", "98", "97"):
        b.set(BID, px, "1")
    for px in ("101", "102", "103", "104"):
        b.set(ASK, px, "1")
    b.trim(BID, 2)
    b.trim(ASK, 2)
    assert list(b.bids.keys()) == [Decimal("99"), Decimal("100")]   # highest 2 kept
    assert list(b.asks.keys()) == [Decimal("101"), Decimal("102")]  # lowest 2 kept
    assert b.best_bid() == Decimal("100") and b.best_ask() == Decimal("101")
    # No-op when already ≤ n.
    b.trim(BID, 10)
    assert list(b.bids.keys()) == [Decimal("99"), Decimal("100")]


def test_make_processor_wired():
    proc, prime = make_processor("cryptocom", "book_btcusd")
    assert isinstance(proc, CryptoComL2Processor) and prime is None


def test_normalizer_completeness_and_exch_ts():
    """End-to-end replay: the normalizer emits a COMPLETE (synced=True) book that re-anchors on the
    reseed snapshot, carries the ``t``-derived ``exch_ts_ns``, and emits NO trades (the book channel
    has no trade tape — trades would be a separate ``trade`` channel we do not subscribe to)."""
    msgs = load_fixture("cryptocom", "book.jsonl")
    records = [(T0 + i, m) for i, m in enumerate(msgs)]
    res = pipeline.normalize_crypto("cryptocom", "book_btcusd", records, levels=10)

    assert res.books, "expected emitted book snapshots"
    assert res.books[0]["synced"] is True                 # first snapshot is a complete baseline
    assert res.books[-1]["synced"] is True                # re-anchored after the reseed
    assert all(r["venue"] == "cryptocom" and r["product"] == "BTC_USD" for r in res.books)
    # exch_ts_ns comes from the book frame's ``t`` (ms → ns), unlike Gemini (which has none).
    assert res.books[0]["exch_ts_ns"] == 1771761038000 * 1_000_000
    assert res.trades == []                                 # book channel: no trade tape
