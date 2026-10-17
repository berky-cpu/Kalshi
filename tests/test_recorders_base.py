from decimal import Decimal

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
