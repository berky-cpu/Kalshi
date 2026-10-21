"""Replay test for the Gemini v2 ``marketdata`` ``l2`` adapter (ROADMAP 1.1-series).

Drives the pure processor from a fixture built to the primary-doc schema
(``developer.gemini.com/websocket/archived/v2``, read 2026-10-21) and CONFIRMED on the live wire
(``wss://api.gemini.com/v2/marketdata`` BTCUSD, 2026-10-20): a ``trades``-bearing snapshot (full
book), incremental deltas (incl. a ``"0.0"`` level removal), a heartbeat and a separate ``trade``
frame (both ignored by the book), a CROSSED-book gap (the only in-band integrity signal — v2 has
NO sequence number), and a fresh snapshot that rebuilds the book and clears the flags. Also
exercises the awaiting-snapshot stale-delta drop and the end-to-end normalizer completeness.
"""
from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.base import ASK, BID
from kbtc.recorders.gemini import GeminiL2Processor, io_config, resubscribe_messages, subscribe_messages
from kbtc.normalize import pipeline

T0 = 1_760_000_000_000_000_000


def test_io_config_and_subscribe_envelope():
    cfg = io_config(product="BTCUSD")
    assert cfg.urls == ["wss://api.gemini.com/v2/marketdata"]
    assert cfg.venue == "gemini" and cfg.channel == "l2_btcusd"
    assert cfg.max_size is None            # full-book snapshot frame must not be size-rejected
    assert cfg.ping_interval_s is None     # server heartbeats + library ping keep the socket alive
    assert subscribe_messages("BTCUSD") == [
        {"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": ["BTCUSD"]}]}
    ]
    # Resync forces a fresh snapshot by unsubscribe-then-subscribe (in-band, no REST).
    assert resubscribe_messages("BTCUSD") == [
        {"type": "unsubscribe", "subscriptions": [{"name": "l2", "symbols": ["BTCUSD"]}]},
        {"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": ["BTCUSD"]}]},
    ]


def test_snapshot_seed_delta_apply_removal_and_best_bid_ask():
    msgs = load_fixture("gemini", "l2.jsonl")
    p = GeminiL2Processor(product="BTCUSD")

    # msgs[0] snapshot (identified by the ``trades``/``auction_events`` signature) → COMPLETE book.
    out = p.process(msgs[0], T0)
    assert out == [{"kind": "snapshot"}]
    assert p.synced and not p.gaps
    assert p.book.levels(BID) == [(Decimal("27000.00"), Decimal("1.0")),
                                  (Decimal("26990.00"), Decimal("2.0"))]
    assert p.book.levels(ASK) == [(Decimal("27010.00"), Decimal("1.5")),
                                  (Decimal("27020.00"), Decimal("2.0"))]
    assert p.book.best_bid() == Decimal("27000.00")
    assert p.book.best_ask() == Decimal("27010.00")

    # msgs[1] delta: absolute SET of a new bid level 27005@0.3.
    assert p.process(msgs[1], T0 + 1) == [{"kind": "book"}]
    assert p.book.bids[Decimal("27005.00")] == Decimal("0.3")
    assert p.book.best_bid() == Decimal("27005.00")

    # msgs[2] delta: qty "0.0" REMOVES bid level 26990.
    p.process(msgs[2], T0 + 2)
    assert Decimal("26990.00") not in p.book.bids
    assert not p.gaps  # no gap through the delta/removal


def test_heartbeat_and_trade_frames_ignored_no_seq_dependence():
    """v2 l2 carries NO sequence number; non-l2 frames (heartbeat, standalone trade) are ignored by
    the book and never affect state — even one carrying a ``socket_sequence`` field."""
    msgs = load_fixture("gemini", "l2.jsonl")
    p = GeminiL2Processor(product="BTCUSD")
    p.process(msgs[0], T0)  # snapshot
    best_before = (p.book.best_bid(), p.book.best_ask())
    assert p.process(msgs[3], T0 + 3) == []   # heartbeat (has socket_sequence): ignored
    assert p.process(msgs[4], T0 + 4) == []   # standalone trade frame: ignored by the book
    assert (p.book.best_bid(), p.book.best_ask()) == best_before
    assert not p.gaps


def test_crossed_book_is_the_gap_signal_then_in_band_reseed():
    msgs = load_fixture("gemini", "l2.jsonl")
    p = GeminiL2Processor(product="BTCUSD")
    for i in range(5):            # snapshot + deltas + heartbeat + trade
        p.process(msgs[i], T0 + i)
    assert p.synced and not p.gaps
    assert p.book.best_bid() == Decimal("27005.00") and p.book.best_ask() == Decimal("27010.00")

    # msgs[5]: a bid at 27015 above the 27010 ask ⇒ CROSSED (a missed ask-removal). The only in-band
    # gap signal v2 affords: logged (never silent), forces resync.
    out = p.process(msgs[5], T0 + 5)
    assert out == [{"kind": "book"}]          # applied, but flagged
    assert len(p.gaps) == 1 and p.gaps[0].reason == "crossed book"
    assert p.needs_resnapshot is True

    # msgs[6] fresh snapshot (the resync result) rebuilds the book and clears every flag — silently
    # (the crossed gap was already logged; a snapshot is the RECOVERY, not a new gap).
    out = p.process(msgs[6], T0 + 6)
    assert out == [{"kind": "snapshot"}]
    assert p.synced and p.needs_resnapshot is False and p.awaiting_snapshot is False
    assert p.book.bids == {Decimal("27001.00"): Decimal("4.0")}
    assert p.book.asks == {Decimal("27011.00"): Decimal("5.0")}
    assert len(p.gaps) == 1                   # the snapshot itself emits no gap

    # msgs[7] delta after the reseed applies normally.
    p.process(msgs[7], T0 + 7)
    assert p.book.bids == {Decimal("27001.00"): Decimal("4.5")}


def test_awaiting_snapshot_drops_stale_deltas_and_sustained_cross_logs_once():
    """After a gap, ``begin_resync`` (the IO layer) arms awaiting-snapshot; stale pre-resubscribe
    deltas are DROPPED (never applied, never re-arm resync) and a sustained cross logs only ONE gap.
    A stale delta can never be mistaken for the rebuild snapshot (it lacks ``trades``)."""
    p = GeminiL2Processor(product="BTCUSD")

    def snap(bid_px, bid_q, ask_px, ask_q):
        return {"type": "l2_updates", "symbol": "BTCUSD",
                "changes": [["buy", bid_px, bid_q], ["sell", ask_px, ask_q]], "trades": []}

    def delta(side, px, q):
        return {"type": "l2_updates", "symbol": "BTCUSD", "changes": [[side, px, q]]}

    p.process(snap("100.0", "1.0", "101.0", "1.0"), T0)
    assert p.synced and not p.gaps

    # A cross: bid 102 > ask 101 ⇒ one "crossed book" gap.
    p.process(delta("buy", "102.0", "1.0"), T0 + 1)
    assert len(p.gaps) == 1 and p.needs_resnapshot is True

    # A SECOND crossing delta before the resync reseed must NOT log again (no gap storm).
    p.process(delta("buy", "103.0", "1.0"), T0 + 2)
    assert len(p.gaps) == 1

    # IO layer resyncs: begin_resync clears the book and arms awaiting-snapshot.
    p.begin_resync()
    assert p.awaiting_snapshot is True and not p.book.bids and not p.book.asks

    # Stale in-flight deltas during the wait (no ``trades`` key) are dropped, not applied.
    assert p.process(delta("sell", "99.0", "1.0"), T0 + 3) == []
    assert not p.book.asks
    assert len(p.gaps) == 1 and p.awaiting_snapshot is True  # still waiting; not thrashing

    # The fresh reseed snapshot (has ``trades``) rebuilds and clears everything.
    p.process(snap("200.0", "7.0", "201.0", "7.0"), T0 + 4)
    assert p.synced and p.awaiting_snapshot is False and p.needs_resnapshot is False
    assert p.book.best_bid() == Decimal("200.0") and p.book.best_ask() == Decimal("201.0")
    assert len(p.gaps) == 1


def test_reconnect_reseed_snapshot_rebuilds_without_begin_resync():
    """On a plain reconnect the processor is reused (still ``synced`` with a stale book). The fresh
    snapshot's ``trades`` signature resets and rebuilds it — silently (the disconnect is logged by
    the IO layer), so no spurious gap is emitted on reconnect."""
    p = GeminiL2Processor(product="BTCUSD")
    p.process({"type": "l2_updates", "symbol": "BTCUSD",
               "changes": [["buy", "100.0", "1.0"], ["sell", "101.0", "1.0"]], "trades": []}, T0)
    assert p.synced and p.book.best_bid() == Decimal("100.0")
    # Reconnect: a fresh snapshot arrives while still synced → silent reset + rebuild, no gap.
    p.process({"type": "l2_updates", "symbol": "BTCUSD",
               "changes": [["buy", "500.0", "2.0"], ["sell", "501.0", "2.0"]],
               "auction_events": []}, T0 + 1)
    assert p.synced and not p.gaps
    assert p.book.bids == {Decimal("500.0"): Decimal("2.0")}
    assert p.book.asks == {Decimal("501.0"): Decimal("2.0")}


def test_normalizer_completeness_and_trade_tape():
    """End-to-end replay: the normalizer emits a COMPLETE (synced=True) book that diverges across
    the crossed-book gap (synced=False) and re-anchors on the reseed snapshot, plus the standalone
    trade frame as a trade row (the snapshot's embedded last-50 ``trades`` are NOT re-emitted)."""
    msgs = load_fixture("gemini", "l2.jsonl")
    records = [(T0 + i, m) for i, m in enumerate(msgs)]
    res = pipeline.normalize_crypto("gemini", "l2_btcusd", records, levels=10)

    assert res.books, "expected emitted book snapshots"
    assert res.books[0]["synced"] is True                 # first snapshot is a complete baseline
    assert res.books[-1]["synced"] is True                # re-anchored after the reseed
    assert any(r["synced"] is False for r in res.books)   # the crossed-book window is honest
    assert res.books[-1]["exch_ts_ns"] is None            # v2 l2 frames carry no event time

    # Exactly one trade row (the standalone type:"trade" frame; the snapshot's embedded trades are
    # a historical backfill and are not re-emitted).
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["venue"] == "gemini" and t["side"] == "buy"
    assert t["price"] == Decimal("27010.00") and t["size"] == Decimal("0.25")
    assert t["trade_id"] == "2840140800042678"
    assert t["exch_ts_ns"] == 1560976400500 * 1_000_000   # ms → ns
