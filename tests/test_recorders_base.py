from decimal import Decimal

import pytest

from kbtc.recorders.base import (
    ASK,
    BID,
    BackoffPolicy,
    GapDetected,
    OrderIdBook,
    PriceLevelBook,
    _crc_token,
    kraken_crc32,
    log_gap,
    normalize_side,
)


def test_ioconfig_max_size_default_preserves_websockets_default():
    """Default ``max_size`` is the websockets 1 MiB default, so no existing feed is shrunk."""
    from kbtc.recorders.base import IoConfig

    cfg = IoConfig(venue="v", channel="c", urls=["wss://x"], subscribe_messages=[])
    assert cfg.max_size == 2 ** 20


def test_run_recorder_passes_max_size_into_connect(monkeypatch):
    """``run_recorder`` is live-only (# pragma: no cover); assert via a fake ``websockets.connect``
    that it forwards ``config.max_size`` (so the Coinbase unbounded setting actually reaches the
    socket). The fake records kwargs then raises to break out of the first connect attempt."""
    import asyncio

    import websockets

    from kbtc.recorders.base import IoConfig, run_recorder

    captured: dict = {}

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        raise RuntimeError("stop after recording connect kwargs")

    monkeypatch.setattr(websockets, "connect", fake_connect)

    async def drive(max_size):
        cfg = IoConfig(venue="coinbase_advanced", channel="level2", urls=["wss://x"],
                       subscribe_messages=[], max_size=max_size)
        stop = asyncio.Event()

        async def runner():
            # No real backoff sleep: base/max 0 ⇒ delay 0. Stop the loop after the first attempt.
            await run_recorder(cfg, [], writer=object(), resync_builder=lambda send: None,
                               backoff=BackoffPolicy(base_s=0, factor=1, max_s=0, jitter=0),
                               stop_event=stop)

        task = asyncio.create_task(runner())
        await asyncio.sleep(0)  # let it reach the fake connect and record
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return captured["max_size"], captured["url"]

    ms, url = asyncio.run(drive(None))
    assert ms is None and url == "wss://x"  # unbounded forwarded exactly
    captured.clear()
    ms2, _ = asyncio.run(drive(2 ** 20))
    assert ms2 == 2 ** 20  # finite default also forwarded


def test_gap_detected_logs_structured(caplog):
    gap = GapDetected("coinbase", "BTC-USD", "full", "sequence jump", 42, expected=105, got=106)
    with caplog.at_level("WARNING", logger="kbtc.recorders"):
        log_gap(gap)
    rec = caplog.records[-1]
    assert rec.levelname == "WARNING"
    assert rec.event == "gap_detected"
    assert rec.expected == 105 and rec.got == 106
    assert "GAP venue=coinbase" in rec.getMessage()


def test_backoff_bounded_and_jittered():
    pol = BackoffPolicy(base_s=1.0, factor=2.0, max_s=30.0, jitter=0.5)
    for attempt in range(1, 12):
        d = pol.delay(attempt)
        assert 0.0 <= d <= 30.0
    # Later attempts trend larger (cap-bounded) — check the midpoint grows pre-cap.
    assert pol.base_s * pol.factor <= 30.0


def test_crc_token_strips_decimal_and_leading_zeros():
    assert _crc_token("44939.5") == "449395"
    assert _crc_token("4.52308393") == "452308393"
    assert _crc_token("0.02000000") == "2000000"
    assert _crc_token("0.0") == "0"


def test_price_level_book_set_and_remove():
    b = PriceLevelBook()
    b.set(BID, "100.0", "1.5")
    b.set(ASK, "101.0", "2.0")
    assert b.best_bid() == Decimal("100.0")
    assert b.best_ask() == Decimal("101.0")
    b.set(BID, "100.0", "0")  # zero removes
    assert b.best_bid() is None


def test_order_id_book_aggregates_levels_and_checksum_stable():
    b = OrderIdBook()
    b.add("1", BID, "99.5", "1.0")
    b.add("2", BID, "99.5", "0.5")  # same level, two orders
    assert b.levels(BID) == [(Decimal("99.5"), Decimal("1.5"))]
    c1 = kraken_crc32(b)
    b.modify("2", qty="0.5")  # no-op value
    assert kraken_crc32(b) == c1


def _full_scan_levels(book, side):
    """Reference top-of-book aggregation by a from-scratch scan of the authoritative order store.

    This is exactly the pre-optimization ``OrderIdBook.levels`` body: sum resting-order qty per
    price over ALL orders, keeping a price key as long as >=1 order rests there (so a price whose
    orders sum to 0 is retained), then sort best-first.
    """
    agg: dict = {}
    for o in book.orders.values():
        if o.side == side:
            agg[o.price] = agg.get(o.price, Decimal(0)) + o.qty
    rev = side == BID
    return sorted(agg.items(), key=lambda kv: kv[0], reverse=rev)


# Spread of seeds so a future refactor can't regress on a code path one seed under-weights
# (red-team LOW hardening). 20251017 is the original seed the reviewer confirmed has teeth; the
# 0..29 range gives broad, reproducible coverage. Per-seed step count stays modest so the full
# matrix runs in well under a second.
@pytest.mark.parametrize("seed", [20251017, *range(30)])
def test_order_id_book_incremental_aggregate_matches_full_scan_property(seed):
    """The derived price→qty cache must equal a from-scratch scan after EVERY mutation.

    Drives a long randomized sequence of add/modify/delete/clear — prices sharing levels, qty
    changes, price-changing modifies, deletes of absent ids, re-adds under an existing id — and
    checks both sides' ``levels(side)`` against the reference scan at every step, plus a few top-n
    slices and ``best_bid``/``best_ask``. This is the cache-coherence guard for the perf fix.
    """
    import random

    rng = random.Random(seed)
    b = OrderIdBook()
    prices = [Decimal(p) for p in ("99.0", "99.5", "100.0", "100.5", "101.0")]
    live: dict[str, str] = {}  # order_id -> side, mirror of what we believe rests

    def check():
        for side in (BID, ASK):
            full = _full_scan_levels(b, side)
            assert b.levels(side) == full
            for n in (0, 1, 2, 3, 10):
                assert b.levels(side, n) == full[:n]
            # best_* agree with the scan's top price (None when empty).
            best = full[0][0] if full else None
            got = b.best_bid() if side == BID else b.best_ask()
            assert got == best

    steps = 600
    for step in range(steps):
        op = rng.random()
        oid = str(rng.randint(0, 40))  # small id space ⇒ frequent re-adds / collisions
        if op < 0.40:  # add (may overwrite an existing id)
            side = rng.choice([BID, ASK])
            price = rng.choice(prices)
            qty = Decimal(rng.choice(["0", "0.5", "1.0", "2.5", "10"]))
            b.add(oid, side, price, qty)
            live[oid] = side
        elif op < 0.70:  # modify (qty and/or price); frequently targets an absent id
            qty = Decimal(rng.choice(["0", "0.25", "3.0", "7.5"])) if rng.random() < 0.8 else None
            price = rng.choice(prices) if rng.random() < 0.5 else None
            b.modify(oid, qty=qty, price=price)
        elif op < 0.95:  # delete (frequently an absent id)
            b.delete(oid)
            live.pop(oid, None)
        else:  # occasional full clear
            b.clear()
            live.clear()
        check()
    assert step == steps - 1


def _price_book_full_scan_levels(book, side):
    """Reference best-first levels by a from-scratch full sort of the side dict — exactly the
    pre-optimization ``PriceLevelBook.levels`` body."""
    d = book.bids if side == BID else book.asks
    rev = side == BID
    return sorted(d.items(), key=lambda kv: kv[0], reverse=rev)


@pytest.mark.parametrize("seed", [20251020, *range(30)])
def test_price_level_book_sorted_aggregate_matches_full_scan_property(seed):
    """``PriceLevelBook.levels`` off the maintained SortedDict must equal a from-scratch full sort
    after EVERY mutation. Randomized ``set`` (incl. qty 0 removal, re-set of an existing price,
    set of a never-seen price) + occasional ``clear`` over both sides, checking ``levels(side)``,
    top-n slices, and ``best_bid``/``best_ask`` against the reference at every step."""
    import random

    from kbtc.recorders.base import PriceLevelBook

    rng = random.Random(seed)
    b = PriceLevelBook()
    prices = [Decimal(p) for p in ("99.0", "99.5", "100.0", "100.5", "101.0", "102.0")]

    def check():
        for side in (BID, ASK):
            full = _price_book_full_scan_levels(b, side)
            assert b.levels(side) == full
            for n in (0, 1, 2, 3, 10):
                assert b.levels(side, n) == full[:n]
            best = full[0][0] if full else None
            got = b.best_bid() if side == BID else b.best_ask()
            assert got == best

    steps = 600
    for step in range(steps):
        op = rng.random()
        if op < 0.85:  # set a level (qty 0 ⇒ remove; may re-set an existing or a new price)
            side = rng.choice([BID, ASK])
            price = rng.choice(prices)
            qty = rng.choice(["0", "0.5", "1.0", "2.5", "10"])  # mix of removals and sets
            b.set(side, price, qty)
        else:  # occasional full clear
            b.clear()
        check()
    assert step == steps - 1


def test_normalize_side():
    assert normalize_side("buy") == BID and normalize_side("sell") == ASK
    assert normalize_side(0) == BID and normalize_side(1) == ASK


def test_resync_on_gap_flag_controls_needs_resnapshot():
    """A log-only (trade) processor logs the gap but does NOT raise needs_resnapshot."""
    from kbtc.recorders.base import MessageProcessor

    class BookProc(MessageProcessor):
        venue = "t"

        def process(self, raw, recv_ts_ns):
            return []

    class TapeProc(MessageProcessor):
        venue = "t"
        resync_on_gap = False

        def process(self, raw, recv_ts_ns):
            return []

    bp = BookProc("p", "c")
    bp._emit_gap("x", 1)
    assert bp.needs_resnapshot is True and len(bp.gaps) == 1

    tp = TapeProc("p", "c")
    tp._emit_gap("x", 1)
    assert tp.needs_resnapshot is False and len(tp.gaps) == 1
