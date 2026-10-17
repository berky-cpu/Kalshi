"""Unit tests for recorder resync (ROADMAP 1.1 / F8, red-team round 2). No network, no socket.

Each test drives a pure processor into ``needs_resnapshot`` then runs the resyncer with a FAKE
``fetch`` (returning a fixture snapshot) or a FAKE ``send`` (recording resubscribe frames), and
asserts the book re-anchors, stale messages are discarded by the venue's **post-snapshot
live-path guard** (not a buffer replay), and the flag clears without thrash.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from conftest import load_fixture

from kbtc.recorders import resync as R
from kbtc.recorders.binance import BinanceDepthProcessor
from kbtc.recorders.bitstamp import BitstampProcessor
from kbtc.recorders.bybit import BybitOrderbookProcessor
from kbtc.recorders.coinbase import CoinbaseProcessor
from kbtc.recorders.kalshi import KalshiProcessor
from kbtc.recorders.kraken import KrakenProcessor


def _run(coro):
    return asyncio.run(coro)


class _FakeSend:
    """Records the frames a ResubscribeResyncer pushes (stand-in for the live ws.send)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _fetcher(snapshot, *, expect_in_url: str | None = None):
    async def fetch(url: str):
        if expect_in_url is not None:
            assert expect_in_url in url, f"{expect_in_url!r} not in {url!r}"
        return snapshot
    return fetch


def _cb_open(seq, oid, px, size="1.0"):
    return {"type": "open", "sequence": seq, "order_id": oid, "side": "buy",
            "price": px, "remaining_size": size}


# --------------------------------------------------------------------------------------
# Infra + design invariants.
# --------------------------------------------------------------------------------------
def test_default_fetch_is_a_coroutine():
    assert asyncio.iscoroutinefunction(R.default_fetch)


def test_resync_on_connect_flags():
    # REST venues must seed the book via the initial REST fetch; in-band venues must NOT fire a
    # resubscribe at startup (the initial subscribe already yields a snapshot -> F3b double-sub).
    assert R.RestSnapshotResyncer.resync_on_connect is True
    assert R.ResubscribeResyncer.resync_on_connect is False


def test_kraken_resync_builder_rejects_placeholder_token():
    with pytest.raises(ValueError):
        R.kraken_resync_builder("BTC/USD", token="<token>")
    with pytest.raises(ValueError):
        R.kraken_resync_builder("BTC/USD", token="")


# --------------------------------------------------------------------------------------
# (1) REST-snapshot venues — exercising the POST-snapshot live-path guard (F2/F6).
# --------------------------------------------------------------------------------------
def test_coinbase_live_path_guard_discards_stale_after_resync():
    p = CoinbaseProcessor(product="BTC-USD", channel="full")
    p.apply_snapshot({"sequence": 100, "bids": [], "asks": []})
    p.process(_cb_open(150, "z", "1.00"), 1)  # sequence jump -> needs_resnapshot
    assert p.needs_resnapshot is True

    snapshot = {"sequence": 200,
                "bids": [["27000.00", "1.0", "snapA"]],
                "asks": [["27010.00", "2.0", "snapB"]]}
    resyncer = R.RestSnapshotResyncer(R.coinbase_snapshot_url("BTC-USD"),
                                      _fetcher(snapshot, expect_in_url="book?level=3"))
    _run(resyncer(p))
    assert p.needs_resnapshot is False
    assert p.sequence == 200

    # Frames that arrive AFTER the snapshot go through the live path (synced=True). seq<=200 are
    # already in the snapshot -> discarded; the +1 applies. This is the real prod discard.
    p.process(_cb_open(198, "old1", "26999.00"), 2)
    p.process(_cb_open(199, "old2", "26998.00"), 3)
    p.process(_cb_open(201, "new1", "27001.00"), 4)

    assert "snapA" in p.book.orders and "snapB" in p.book.orders
    assert "old1" not in p.book.orders and "old2" not in p.book.orders
    assert "new1" in p.book.orders
    assert p.sequence == 201


def test_binance_live_path_guard_aligns_window_after_resync():
    p = BinanceDepthProcessor(product="BTCUSDT")
    snapshot = {"lastUpdateId": 1000,
                "bids": [["27000.0", "1.0"]], "asks": [["27010.0", "1.0"]]}
    resyncer = R.RestSnapshotResyncer(
        R.binance_snapshot_url("BTCUSDT"),
        _fetcher(snapshot, expect_in_url="depth?symbol=BTCUSDT&limit=1000"),
    )
    _run(resyncer(p))
    assert p.needs_resnapshot is False
    assert p.last_update_id == 1000 and p.prev_u is None

    # Live path after the snapshot: drop u<lastUpdateId, gate first event U<=1000<=u, then pu chain.
    p.process({"e": "depthUpdate", "U": 990, "u": 999, "b": [["26990.0", "9.0"]], "a": []}, 1)
    p.process({"e": "depthUpdate", "U": 998, "u": 1001, "b": [["27000.0", "2.0"]], "a": []}, 2)
    p.process({"e": "depthUpdate", "U": 1002, "u": 1005, "pu": 1001,
               "b": [["26990.0", "3.0"]], "a": []}, 3)

    assert p.book.bids[Decimal("27000.0")] == Decimal("2.0")
    assert p.book.bids[Decimal("26990.0")] == Decimal("3.0")  # chained event, not the dropped 9
    assert p.prev_u == 1005
    assert not p.gaps


def test_bitstamp_reseed_floor_drops_stale_events_no_double_apply():
    """F1/F6: a reseed records a microtimestamp FLOOR; events already in the snapshot
    (microtimestamp <= floor) must NOT alter the book and must NOT raise a false gap."""
    p = BitstampProcessor(product="btcusd")
    base = {"event": "order_created", "channel": "live_orders_btcusd"}
    # Reseed snapshot carries microtimestamp 5000 and an existing order "201".
    snapshot = {"microtimestamp": "5000",
                "bids": [["27000", "5", "201"]], "asks": [["27010", "4", "202"]]}
    resyncer = R.RestSnapshotResyncer(
        R.bitstamp_snapshot_url("btcusd"),
        _fetcher(snapshot, expect_in_url="order_book/btcusd/?group=2"),
    )
    _run(resyncer(p))
    assert p.needs_resnapshot is False
    assert p.snapshot_floor_micro == 5000
    assert p.last_event_id is None
    assert "201" in p.book.orders and "202" in p.book.orders

    # STALE events (micro <= 5000) are already in the snapshot -> dropped silently, no anchor.
    p.process({**base, "event": "order_changed", "event_id": "s1", "pre_event_id": "s0",
               "data": {"id": 201, "order_type": 0, "price": "27000", "amount": "99",
                        "microtimestamp": "4000"}}, 1)
    p.process({**base, "event": "order_created", "event_id": "s2", "pre_event_id": "s1",
               "data": {"id": 999, "order_type": 0, "price": "26000", "amount": "1",
                        "microtimestamp": "5000"}}, 2)
    assert p.book.orders["201"].qty == Decimal("5")  # NOT changed to 99
    assert "999" not in p.book.orders  # phantom NOT created
    assert p.gaps == []  # stale drops are expected, not gaps
    assert p.last_event_id is None  # chain still unanchored

    # First event PAST the floor re-anchors the chain with no false gap, and the floor clears.
    p.process({**base, "event": "order_created", "event_id": "e9", "pre_event_id": "e8",
               "data": {"id": 303, "order_type": 0, "price": "27001", "amount": "2",
                        "microtimestamp": "6000"}}, 3)
    assert p.gaps == []
    assert p.last_event_id == "e9"
    assert p.snapshot_floor_micro is None
    assert "303" in p.book.orders


# --------------------------------------------------------------------------------------
# (1c) Persisting the REST snapshot into the recording (ROADMAP 1.1c).
#
# The resyncer now writes its fetched snapshot into the SAME recording via an injected ``persist``
# callback (wired by run_recorder to writer.write), tagged so replay can rebuild a COMPLETE book.
# --------------------------------------------------------------------------------------
def test_rest_resyncer_persists_tagged_snapshot_and_applies():
    p = CoinbaseProcessor(product="BTC-USD", channel="full")
    persisted: list[tuple[str, int]] = []

    def persist(record: str, ts: int) -> None:
        persisted.append((record, ts))

    snapshot = {"sequence": 100,
                "bids": [["27000.00", "1.0", "s1"]],
                "asks": [["27010.00", "2.0", "s2"]]}
    resyncer = R.RestSnapshotResyncer(
        R.coinbase_snapshot_url("BTC-USD"), _fetcher(snapshot), persist=persist)
    _run(resyncer(p))

    # Applied to the processor (unchanged in-memory behaviour).
    assert p.synced is True and p.sequence == 100
    assert "s1" in p.book.orders and "s2" in p.book.orders

    # Persisted EXACTLY ONE tagged record, stamped with a real recv_ts.
    assert len(persisted) == 1
    record, ts = persisted[0]
    assert isinstance(ts, int) and ts > 0
    parsed = json.loads(record)
    assert parsed[R.REST_SNAPSHOT_TAG] == 1
    assert parsed["venue"] == "coinbase" and parsed["channel"] == "full"
    assert parsed["body"] == snapshot  # raw REST snapshot stored UNTOUCHED

    # The parse helper round-trips the envelope; a normal WS frame is NOT mistaken for one.
    assert R.parse_rest_snapshot_record(parsed)["body"] == snapshot
    assert R.parse_rest_snapshot_record(_cb_open(101, "x", "1.0")) is None


def test_rest_resyncer_persists_on_both_initial_sync_and_gap_resync():
    # ONE code path serves BOTH initial sync and gap resync, so a second invocation (after a gap)
    # persists a second tagged snapshot — recordings re-anchor completely on every resync.
    p = BinanceDepthProcessor(product="BTCUSDT")
    persisted: list[tuple[str, int]] = []
    snap = {"lastUpdateId": 1000, "bids": [["27000.0", "1.0"]], "asks": [["27010.0", "1.0"]]}
    resyncer = R.RestSnapshotResyncer(
        R.binance_snapshot_url("BTCUSDT"), _fetcher(snap),
        persist=lambda rec, ts: persisted.append((rec, ts)))

    _run(resyncer(p))  # INITIAL sync
    assert len(persisted) == 1 and p.last_update_id == 1000

    # A first-event window miss flags a resync; the IO layer clears the flag and re-calls.
    p.process({"e": "depthUpdate", "U": 1001, "u": 1005, "b": [], "a": []}, 1)
    assert p.needs_resnapshot is True
    p.clear_resnapshot()
    _run(resyncer(p))  # gap RESYNC — same resyncer, same code path

    assert len(persisted) == 2
    assert all(json.loads(r)[R.REST_SNAPSHOT_TAG] == 1 for r, _ in persisted)
    assert all(json.loads(r)["body"] == snap for r, _ in persisted)


def test_rest_resyncer_without_persist_applies_and_in_band_has_no_persist_seam():
    # No persist wired (offline construction / tests that only check the apply): the in-memory
    # apply is unchanged and nothing is written.
    p = CoinbaseProcessor(product="BTC-USD", channel="full")
    resyncer = R.RestSnapshotResyncer(R.coinbase_snapshot_url("BTC-USD"),
                                      _fetcher({"sequence": 5, "bids": [], "asks": []}))
    assert resyncer.persist is None
    _run(resyncer(p))
    assert p.sequence == 5 and p.synced is True

    # In-band resyncers expose NO ``persist`` attribute, so run_recorder never wires one to them
    # (their snapshot rides the WS stream and is recorded as a normal frame).
    send = _FakeSend()
    assert not hasattr(R.bybit_resync_builder("BTCUSDT", depth=50)(send), "persist")
    assert not hasattr(R.kalshi_resync_builder(tickers=["KXBTC15M"])(send), "persist")


# --------------------------------------------------------------------------------------
# (2) In-band-resubscribe venues — frames + rebuild + thrash suppression (F4).
# --------------------------------------------------------------------------------------
def test_kraken_resubscribe_frame_rebuild_and_no_thrash():
    p = KrakenProcessor(product="BTC/USD", depth=10)
    p.needs_resnapshot = True  # pretend a checksum mismatch flagged a resync
    send = _FakeSend()
    resyncer = R.kraken_resync_builder("BTC/USD", depth=10, token="TKN")(send)
    _run(resyncer(p))

    assert send.sent[0] == {"method": "unsubscribe",
                            "params": {"channel": "level3", "symbol": ["BTC/USD"],
                                       "token": "TKN"}}
    sub = send.sent[1]
    assert sub["method"] == "subscribe"
    assert sub["params"]["snapshot"] is True
    assert sub["params"]["symbol"] == ["BTC/USD"] and sub["params"]["token"] == "TKN"
    assert p.awaiting_snapshot is True

    msgs = load_fixture("kraken", "level3.jsonl")
    # A checksum-failing UPDATE arriving before the fresh snapshot must be SUPPRESSED (no thrash).
    p.process(msgs[2], 1)
    assert p.gaps == []
    assert p.awaiting_snapshot is True

    # The fresh snapshot frame rebuilds the book and resumes normal checks.
    p.process(msgs[0], 2)
    assert p.synced is True and p.awaiting_snapshot is False
    assert p.book.best_bid() == Decimal("99.5")
    assert p.gaps == []


def test_bybit_resubscribe_frame_rebuild_and_no_thrash():
    p = BybitOrderbookProcessor(product="BTCUSDT", depth=50)
    p.process({"topic": "orderbook.50.BTCUSDT", "type": "delta",
               "data": {"u": 5, "seq": 5, "b": [["1.0", "1.0"]], "a": []}}, 1)
    assert p.needs_resnapshot is True
    p.clear_resnapshot()  # the IO layer clears before calling the resyncer

    send = _FakeSend()
    resyncer = R.bybit_resync_builder("BTCUSDT", depth=50)(send)
    _run(resyncer(p))
    assert {"op": "unsubscribe", "args": ["orderbook.50.BTCUSDT"]} in send.sent
    assert any(f.get("op") == "subscribe" and "orderbook.50.BTCUSDT" in f.get("args", [])
               for f in send.sent)
    assert p.awaiting_snapshot is True
    gaps_before = len(p.gaps)  # one legit "delta before snapshot" gap from setup

    # A delta before the fresh snapshot is SUPPRESSED: no NEW gap, no re-arm (no resubscribe thrash).
    p.process({"topic": "orderbook.50.BTCUSDT", "type": "delta",
               "data": {"u": 6, "seq": 6, "b": [["2.0", "2.0"]], "a": []}}, 2)
    assert len(p.gaps) == gaps_before
    assert p.needs_resnapshot is False
    assert p.awaiting_snapshot is True

    snap_frame = load_fixture("bybit", "orderbook.jsonl")[0]
    p.process(snap_frame, 3)
    assert p.synced is True and p.awaiting_snapshot is False
    assert len(p.gaps) == gaps_before
    assert p.book.bids[Decimal("27000.0")] == Decimal("1.0")
    assert p.book.asks[Decimal("27010.0")] == Decimal("1.5")


def test_kalshi_resync_is_fail_safe_noop_pending_1_1b():
    """Kalshi in-band resync is a fail-safe NO-OP (UNVERIFIED sids protocol could double-count):
    a gap clears the flag, sends NO frames, and the stream stays processable — raw is persisted
    by run_recorder regardless, and the live book is not consumed yet."""
    p = KalshiProcessor(product="KXBTC15M")
    # Establish a live subscription (sid 7) via an initial snapshot (VERIFIED schema; nested under
    # ``msg`` to also exercise the robust wrapper parse).
    p.process({"type": "orderbook_snapshot", "sid": 7, "seq": 1,
               "msg": {"market_ticker": "KXBTC15M-TEST",
                       "yes_dollars_fp": [["0.50", "100.00"]], "no_dollars_fp": []}}, 1)
    # A seq gap flags a resync.
    p.process({"type": "orderbook_delta", "sid": 7, "seq": 5,
               "msg": {"market_ticker": "KXBTC15M-TEST", "side": "yes",
                       "price_dollars": "0.50", "delta_fp": "10.00"}}, 2)
    assert p.needs_resnapshot is True

    send = _FakeSend()
    resyncer = R.kalshi_resync_builder(tickers=["KXBTC15M-TEST"])(send)
    assert resyncer.resync_on_connect is False  # never fired on initial connect
    _run(resyncer(p))

    # No unsubscribe/subscribe frames were sent; the flag is cleared; no spurious awaiting state.
    assert send.sent == []
    assert p.needs_resnapshot is False
    assert p.awaiting_snapshot is False

    # The stream keeps flowing (raw persisted by run_recorder); a later snapshot still rebuilds.
    p.process({"type": "orderbook_snapshot", "sid": 7, "seq": 9,
               "msg": {"market_ticker": "KXBTC15M-TEST",
                       "yes_dollars_fp": [["0.51", "200.00"]],
                       "no_dollars_fp": [["0.49", "80.00"]]}}, 3)
    assert p.books["KXBTC15M-TEST"]["yes"] == {Decimal("0.51"): Decimal("200.00")}
    assert p.books["KXBTC15M-TEST"]["no"] == {Decimal("0.49"): Decimal("80.00")}
