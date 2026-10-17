"""Replay test for the Coinbase Advanced Trade (retail) level2 adapter (ROADMAP 1.1d).

Drives the pure processor from a synthetic fixture built to the primary-doc schema
(docs.cdp.coinbase.com, 2026-10-17): a snapshot + updates (incl. a ``new_quantity="0"`` level
removal), a SEEDED per-connection ``sequence_num`` gap (logged, not silent, forces resync), and a
fresh snapshot after resync that rebuilds the book and clears the flag. Also exercises the gap
check running across heartbeats (the per-connection subtlety) and the end-to-end normalizer
completeness (``synced``) transitions.
"""
from decimal import Decimal

from conftest import load_fixture

from kbtc.recorders.base import ASK, BID
from kbtc.recorders.coinbase_advanced import CoinbaseAdvancedProcessor
from kbtc.normalize import pipeline

T0 = 1_760_000_000_000_000_000


def test_snapshot_updates_removal_gap_and_resync():
    msgs = load_fixture("coinbase_advanced", "level2.jsonl")
    p = CoinbaseAdvancedProcessor(product="BTC-USD")

    # seq 0 subscriptions ack, seq 1 heartbeat — control frames, no book, no gap (anchor sequence).
    p.process(msgs[0], T0)
    p.process(msgs[1], T0 + 1)
    assert not p.synced and not p.gaps

    # seq 2 snapshot establishes a COMPLETE baseline.
    p.process(msgs[2], T0 + 2)
    assert p.synced
    assert p.book.levels(BID) == [(Decimal("27000.00"), Decimal("1.0")),
                                  (Decimal("26990.00"), Decimal("2.0"))]
    assert p.book.best_ask() == Decimal("27010.00")

    # seq 3 update: set bid 27000→0.5 and add a new offer level 27005@0.8.
    p.process(msgs[3], T0 + 3)
    assert p.book.bids[Decimal("27000.00")] == Decimal("0.5")
    assert p.book.asks[Decimal("27005.00")] == Decimal("0.8")

    # seq 4 heartbeat (the gap check must tolerate it — sequence is per-CONNECTION, not per-product).
    p.process(msgs[4], T0 + 4)
    assert not p.gaps

    # seq 5 update: new_quantity "0" REMOVES bid level 26990.
    p.process(msgs[5], T0 + 5)
    assert Decimal("26990.00") not in p.book.bids
    assert p.book.best_bid() == Decimal("27000.00")
    assert not p.gaps  # no gap through the heartbeat + removal

    # seq 8 (6,7 dropped) is a GAP: logged, not silent, forces resync; its update is NOT applied.
    out = p.process(msgs[6], T0 + 8)
    assert out == []
    assert len(p.gaps) == 1
    assert p.gaps[0].reason == "sequence_num gap"
    assert p.gaps[0].expected == 6 and p.gaps[0].got == 8
    assert p.needs_resnapshot is True
    assert p.book.asks[Decimal("27010.00")] == Decimal("1.5")  # the gapped update was dropped

    # seq 9 fresh snapshot (the resync result) rebuilds the book and clears the flags.
    p.process(msgs[7], T0 + 9)
    assert p.synced and p.needs_resnapshot is False and p.awaiting_snapshot is False
    assert p.book.bids == {Decimal("27001.00"): Decimal("4.0")}
    assert p.book.asks == {Decimal("27011.00"): Decimal("5.0")}

    # seq 10 update after resync applies normally.
    p.process(msgs[8], T0 + 10)
    assert p.book.bids == {Decimal("27001.00"): Decimal("4.5")}


def test_second_gap_while_awaiting_snapshot_is_silent_no_resubscribe_thrash():
    """Under sustained loss, a second gap BEFORE the resync snapshot must NOT re-log or re-arm the
    resync (which would thrash resubscribe frames against Coinbase's PARTIAL/unknown rate limits).
    Exactly ONE gap is logged and ONE resync armed; the fresh snapshot still clears + rebuilds."""
    p = CoinbaseAdvancedProcessor(product="BTC-USD")

    def snap(seq, px, qty):
        return {"channel": "l2_data", "sequence_num": seq,
                "events": [{"type": "snapshot", "product_id": "BTC-USD",
                            "updates": [{"side": "bid", "price_level": px, "new_quantity": qty}]}]}

    def upd(seq, px, qty):
        return {"channel": "l2_data", "sequence_num": seq,
                "events": [{"type": "update", "product_id": "BTC-USD",
                            "updates": [{"side": "bid", "price_level": px, "new_quantity": qty}]}]}

    p.process(snap(2, "100.0", "1.0"), T0)
    assert p.synced and not p.gaps

    # First gap (expected 3, got 5): logged once, resync armed, now awaiting the snapshot.
    p.process(upd(5, "100.0", "2.0"), T0 + 1)
    assert len(p.gaps) == 1 and p.needs_resnapshot is True and p.awaiting_snapshot is True

    # Second gap during the wait (expected 6, got 9): SILENT — no new gap, no re-arm.
    out = p.process(upd(9, "100.0", "3.0"), T0 + 2)
    assert out == []
    assert len(p.gaps) == 1              # still exactly ONE gap logged
    assert p.awaiting_snapshot is True   # still waiting; not thrashing
    assert p.book.bids[Decimal("100.0")] == Decimal("1.0")  # neither gapped update applied

    # The fresh resync snapshot clears the state and rebuilds (one resubscribe sufficed).
    p.process(snap(10, "101.0", "7.0"), T0 + 3)
    assert p.synced and p.needs_resnapshot is False and p.awaiting_snapshot is False
    assert p.book.bids == {Decimal("101.0"): Decimal("7.0")}
    assert len(p.gaps) == 1


def test_duplicate_or_out_of_order_sequence_is_ignored_not_a_gap():
    p = CoinbaseAdvancedProcessor(product="BTC-USD")
    snap = {"channel": "l2_data", "sequence_num": 5,
            "events": [{"type": "snapshot", "product_id": "BTC-USD",
                        "updates": [{"side": "bid", "price_level": "100.0",
                                     "new_quantity": "1.0"}]}]}
    p.process(snap, 1)
    assert p.book.best_bid() == Decimal("100.0")
    # A lower sequence_num (replay / out-of-order) is ignored — not a gap, book untouched.
    stale = {"channel": "l2_data", "sequence_num": 3,
             "events": [{"type": "update", "product_id": "BTC-USD",
                         "updates": [{"side": "bid", "price_level": "100.0",
                                      "new_quantity": "9.0"}]}]}
    out = p.process(stale, 2)
    assert out == [] and not p.gaps
    assert p.book.bids[Decimal("100.0")] == Decimal("1.0")  # unchanged


def test_normalizer_completeness_and_processor_transitions_across_resync():
    """Completeness behaviour for Coinbase-retail (a COMPLETE constituent).

    Reality (not a synced=False row): after the snapshot the book is COMPLETE and every emitted row
    is synced=True. On a gap the stale book is simply NOT re-emitted during the wait (updates are
    dropped until the resync snapshot), so NO synced=False row is produced; a fresh synced=True book
    follows the resync. We assert (1) the normalizer emits only synced=True rows, and (2) the
    PROCESSOR-level synced / needs_resnapshot / awaiting_snapshot transitions directly across the
    snapshot → gap → resync sequence, so a regression in synced emission is actually caught."""
    msgs = load_fixture("coinbase_advanced", "level2.jsonl")

    # (1) Normalizer: all emitted book rows are synced=True (no synced=False row is ever emitted
    # because the diverged book is frozen, not re-emitted, during the gap window).
    records = [(T0 + i, m) for i, m in enumerate(msgs)]
    res = pipeline.normalize_crypto("coinbase_advanced", "level2", records, levels=10)
    assert res.books, "expected emitted book snapshots"
    assert all(r["synced"] is True for r in res.books)  # catches any synced=False emission regression
    assert res.books[0]["synced"] is True and res.books[-1]["synced"] is True
    assert res.trades == []                              # no crypto trade tape on level2
    assert res.books[-1]["exch_ts_ns"] is not None       # RFC3339 envelope timestamp parsed

    # (2) Processor-level completeness flags across the sequence (indices per the fixture).
    p = CoinbaseAdvancedProcessor(product="BTC-USD")
    p.process(msgs[0], T0); p.process(msgs[1], T0 + 1)          # acks/heartbeat: no baseline yet
    assert (p.synced, p.needs_resnapshot, p.awaiting_snapshot) == (False, False, False)
    p.process(msgs[2], T0 + 2)                                  # snapshot → COMPLETE baseline
    assert (p.synced, p.needs_resnapshot, p.awaiting_snapshot) == (True, False, False)
    p.process(msgs[3], T0 + 3); p.process(msgs[4], T0 + 4); p.process(msgs[5], T0 + 5)
    assert (p.synced, p.needs_resnapshot, p.awaiting_snapshot) == (True, False, False)  # still complete
    p.process(msgs[6], T0 + 8)                                  # seq gap (8, 6–7 dropped)
    # synced stays True at the processor; the book is diverged (awaiting) and resync is armed once.
    assert (p.synced, p.needs_resnapshot, p.awaiting_snapshot) == (True, True, True)
    p.process(msgs[7], T0 + 9)                                  # resync snapshot re-anchors
    assert (p.synced, p.needs_resnapshot, p.awaiting_snapshot) == (True, False, False)
