from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.binance import BinanceAggTradeProcessor, BinanceDepthProcessor


def test_depth_pu_chain_reconstruction_and_seeded_break():
    msgs = load_fixture("binance", "depth.jsonl")
    p = BinanceDepthProcessor(product="BTCUSDT")
    # REST snapshot: lastUpdateId sits inside the first event's [U, u] = [100, 110].
    p.apply_snapshot({"lastUpdateId": 105,
                      "bids": [["27000.0", "0.1"]], "asks": [["27010.0", "0.1"]]})
    ts = 1_760_000_000_000_000_000
    for i, m in enumerate(msgs):
        p.process(m, ts + i)
        if p.gaps:
            break

    # Clean chain (pu == prev.u) reconstructs; qty 0 removed the 27010 ask.
    assert p.book.bids[Decimal("27000.0")] == Decimal("0.5")
    assert p.book.bids[Decimal("26980.0")] == Decimal("4.0")
    assert Decimal("27010.0") not in p.book.asks
    assert p.book.asks[Decimal("27020.0")] == Decimal("3.0")

    # Seeded pu break (pu=125 but prev u=120) -> gap + resync, never silent.
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "pu chain break"
    assert p.gaps[0].expected == 120 and p.gaps[0].got == 125
    assert p.needs_resnapshot is True


def test_aggtrade_non_monotonic_id_flagged():
    p = BinanceAggTradeProcessor(product="BTCUSDT")
    p.process({"e": "aggTrade", "a": 10, "p": "27000", "q": "0.1", "T": 1, "m": False}, 1)
    p.process({"e": "aggTrade", "a": 11, "p": "27001", "q": "0.2", "T": 2, "m": True}, 2)
    assert not p.gaps
    p.process({"e": "aggTrade", "a": 11, "p": "27002", "q": "0.3", "T": 3, "m": True}, 3)
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "aggTrade id non-monotonic"
    assert p.needs_resnapshot is False  # log-only: a trade tape has no book to resync
