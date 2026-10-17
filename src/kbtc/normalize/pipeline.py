"""Replay-driven normalization pipeline (ROADMAP 2.1).

Each raw recording is replayed through the SAME pure venue ``MessageProcessor`` the recorders run
(reused via ``recorders.health.make_processor`` — the book logic is NOT reimplemented here). On
each book-changing update we snapshot the reconstructed top-N L2 book (event-driven; see
``schema.py``), normalize the trade tape, and validate against the venue checkpoint oracle.

Inputs are ``(recv_ts_ns, raw)`` pairs so tests can drive the fixtures directly and the CLI can
wrap ``recorders.writer.read_records``. Reads/transforms only — no network, no order path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Iterator

from kbtc.recorders.base import as_dict
from kbtc.recorders.health import (
    _prime_binance,
    _prime_coinbase,
    data_classifier,
    make_processor,
)

from . import checkpoint as ckpt
from . import extract

# Prime functions by venue (REST-snapshot venues need a baseline not in the recording). Reused
# verbatim from the health module so normalization and health agree on replay seeding.
_PRIME = {"coinbase": _prime_coinbase, "binance": _prime_binance}


def _is_recorded_complete_snapshot(venue: str, msg: Any) -> bool:
    """True iff ``msg`` is a RECORDED complete-book snapshot that establishes a full baseline.

    Only the in-band-snapshot venues carry such a frame in the recording (Kraken/Bybit ``snapshot``,
    Kalshi ``orderbook_snapshot``). Coinbase/Binance/Bitstamp seed from a REST snapshot that the
    recorder applies in-memory and does NOT persist, so no such frame ever appears in a WS-only
    recording → they never reach a complete baseline offline (``synced`` stays False — the honest
    result; the reconstructed book is partial). See schema.py COMPLETENESS MARKER.
    """
    if not isinstance(msg, dict):
        return False
    v = venue.lower()
    if v == "kraken":
        return msg.get("channel") == "level3" and msg.get("type") == "snapshot"
    if v == "bybit":
        return str(msg.get("topic", "")).startswith("orderbook.") and msg.get("type") == "snapshot"
    if v == "kalshi":
        return msg.get("type") == "orderbook_snapshot"
    return False


@dataclass
class NormalizeResult:
    books: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    checkpoint: ckpt.CheckpointReport | None = None


Record = tuple[int, Any]  # (recv_ts_ns, raw-json-str-or-dict)


# ---------------------------------------------------------------------------------------
# Crypto venues (Coinbase/Kraken/Bitstamp L3; Binance/Bybit L2).
# ---------------------------------------------------------------------------------------
def normalize_crypto(venue: str, channel: str, records: Iterable[Record],
                     levels: int = 10) -> NormalizeResult:
    """Replay a crypto feed → event-driven top-N L2 book rows + trade rows + checkpoint report."""
    proc, _prime = make_processor(venue, channel)
    prime = _PRIME.get(venue.lower())
    report = ckpt.new_report(venue, channel)
    res = NormalizeResult(checkpoint=report)
    if proc is None:  # pragma: no cover - every crypto venue has a processor
        return res

    primed = prime is None
    last_fp: tuple | None = None
    snap_id = 0
    # Completeness tracking (red-team #2/#4). ``baseline_established`` flips True only when a
    # RECORDED complete-book snapshot is applied; ``pending_resync`` is True from a detected gap
    # until the next such snapshot re-anchors. Emitted ``synced = baseline_established and not
    # pending_resync``.
    baseline_established = False
    pending_resync = False

    for recv, raw in records:
        msg = as_dict(raw)

        # 1) Trade tape (independent of the book processor).
        for t in extract.trade_rows(venue, channel, raw):
            t["recv_ts_ns"] = recv
            res.trades.append(t)

        # 2) Bybit checkpoint: compare reconstruction to a mid-stream snapshot BEFORE it resets.
        if venue.lower() == "bybit":
            ckpt.bybit_checkpoint(report, proc, raw)

        is_snap = _is_recorded_complete_snapshot(venue, msg)

        # 3) Feed the book processor, mirroring health's priming for REST-snapshot venues.
        do_emit = True
        if not primed:
            action = prime(proc, raw)  # type: ignore[misc]
            if action == "skip":
                proc.process(raw, recv)
                continue  # ack / pre-baseline frame: no book yet (trades already captured)
            primed = True
            if action == "consume":
                do_emit = False  # baseline applied from this frame; nothing to emit yet
            else:  # "keep"
                proc.process(raw, recv)
        else:
            proc.process(raw, recv)

        # 4) Kraken gold checkpoint: independent CRC32 recompute vs the frame checksum.
        if venue.lower() == "kraken":
            ckpt.kraken_checkpoint(report, proc, raw)

        # A recorded complete snapshot (re)establishes a full baseline and clears any pending
        # resync; it IS the resync, so it must be checked AFTER the gap test below cannot undo it.
        if is_snap and getattr(proc, "synced", False):
            baseline_established = True
            pending_resync = False

        # Mirror the live IO layer: a detected gap diverges the book until a resync re-anchors.
        if proc.needs_resnapshot:
            pending_resync = True
            proc.clear_resnapshot()
            if prime is not None:
                primed = False

        synced = baseline_established and not pending_resync

        # 5) Emit a top-N L2 snapshot if the top-N actually changed. We gate on the book being
        # non-empty (``rows``) rather than ``proc.synced``: Coinbase/Binance buffer pre-baseline
        # frames (book stays empty → no emit), while Bitstamp never sets ``synced`` offline (it has
        # no snapshot message — it applies events straight to the book), so a ``synced`` gate would
        # wrongly suppress every Bitstamp snapshot. The completeness ``synced`` flag is a COLUMN,
        # not a filter — rows are always kept, just marked.
        if do_emit and getattr(proc, "book", None) is not None:
            rows = extract.l2_levels(proc.book, levels)
            fp = extract.book_fingerprint(rows)
            if rows and fp != last_fp:
                last_fp = fp
                snap_id += 1
                exch = extract.exch_ts_ns(venue, msg)
                for side, lvl, price, size in rows:
                    res.books.append(_book_row(recv, exch, venue, proc.product, channel,
                                               snap_id, synced, side, lvl, price, size))
    return res


def _book_row(recv: int, exch: int | None, venue: str, product: str, channel: str,
              snap_id: int, synced: bool, side: str, level: int,
              price: Decimal, size: Decimal) -> dict:
    return {
        "recv_ts_ns": recv, "exch_ts_ns": exch, "venue": venue, "product": product,
        "channel": channel, "snap_id": snap_id, "synced": synced, "side": side, "level": level,
        "price": price, "size": size,
    }


# ---------------------------------------------------------------------------------------
# Kalshi yes/no book + trades (its processor keeps a per-ticker dict book, not an Order/PriceBook).
# ---------------------------------------------------------------------------------------
def normalize_kalshi(channel: str, records: Iterable[Record]) -> NormalizeResult:
    """Replay a Kalshi feed → per-ticker yes/no L2 book rows + trade rows.

    Kalshi has no cross-book checkpoint oracle; the payload/seq schema is UNVERIFIED (1.1b), so we
    normalize exactly what we record. ``checkpoint`` is a deferred report.
    """
    proc, _ = make_processor("kalshi", channel)
    report = ckpt.CheckpointReport(
        "kalshi", channel, ckpt.METHOD_DEFERRED,
        notes="Kalshi payload/seq schema UNVERIFIED (ROADMAP 1.1b); no cross-book checkpoint oracle",
    )
    res = NormalizeResult(checkpoint=report)
    if proc is None:  # pragma: no cover
        return res

    last_fp: dict[str, tuple] = {}
    snap_id = 0
    # Per-ticker completeness: a baseline is the ``orderbook_snapshot`` for that ticker; a seq gap
    # (processor ``needs_resnapshot``) flags the ticker's book diverged until the next snapshot.
    baseline: dict[str, bool] = {}
    pending: dict[str, bool] = {}

    for recv, raw in records:
        msg = as_dict(raw)

        for t in extract.trade_rows("kalshi", channel, raw):
            t["recv_ts_ns"] = recv
            res.trades.append(t)

        proc.process(raw, recv)

        typ = msg.get("type") if isinstance(msg, dict) else None
        if typ not in {"orderbook_snapshot", "orderbook_delta"}:
            continue
        ticker = (msg.get("msg", {}) or {}).get("market_ticker", proc.product)
        if typ == "orderbook_snapshot":
            baseline[ticker] = True
            pending[ticker] = False
        if proc.needs_resnapshot:
            pending[ticker] = True
            proc.clear_resnapshot()
        synced = baseline.get(ticker, False) and not pending.get(ticker, False)

        book = proc.books.get(ticker, {})
        rows = _kalshi_levels(book)
        fp = extract.book_fingerprint(rows)
        if fp != last_fp.get(ticker):
            last_fp[ticker] = fp
            snap_id += 1
            for side, lvl, price, size in rows:
                res.books.append(_book_row(recv, None, "kalshi", ticker, channel,
                                           snap_id, synced, side, lvl, price, size))
    return res


def _kalshi_levels(book: dict) -> list[tuple[str, int, Decimal, Decimal]]:
    """Each side's price→size map → best-first (price descending) ``(side, level, price, size)``.

    Prices/sizes are int cents / contract counts from the processor; coerced to Decimal (exact).
    """
    out: list[tuple[str, int, Decimal, Decimal]] = []
    for side in sorted(book.keys()):
        levels = sorted(book[side].items(), key=lambda kv: Decimal(str(kv[0])), reverse=True)
        for i, (price, size) in enumerate(levels):
            out.append((side, i, Decimal(str(price)), Decimal(str(size))))
    return out


# ---------------------------------------------------------------------------------------
# Settlements (ROADMAP 1.2 SettlementRecord envelopes → rows).
# ---------------------------------------------------------------------------------------
def settlement_rows(envelopes: Iterable[dict]) -> list[dict]:
    """Normalize stored settlement ENVELOPES (``SettlementRecord.to_envelope()`` shape) to rows.

    The envelope already carries parsed fields (monetary values as strings, times as ns ints) plus
    the untouched ``raw_payload``. Monetary values become Decimal (never float); ``raw_payload`` is
    preserved as a JSON string for provenance.
    """
    import json

    def dec(x: Any) -> Decimal | None:
        if x is None or x == "":
            return None
        return Decimal(str(x))

    rows: list[dict] = []
    for env in envelopes:
        rows.append({
            "market_ticker": env.get("market_ticker"),
            "event_ticker": env.get("event_ticker"),
            "status": env.get("status", ""),
            "result": env.get("result", "") or "",
            "settlement_value": dec(env.get("settlement_value")),
            "close_value": dec(env.get("close_value")),
            "open_value": dec(env.get("open_value")),
            "open_time_ns": env.get("open_time_ns"),
            "close_time_ns": env.get("close_time_ns"),
            "settle_time_ns": env.get("settle_time_ns"),
            "recv_ts_ns": env.get("recv_ts_ns"),
            "raw_json": json.dumps(env.get("raw_payload", {}), separators=(",", ":"),
                                   default=str),
        })
    return rows


def read_envelopes(paths: Iterable[str]) -> Iterator[dict]:
    """Yield settlement envelopes from recorded ``.jsonl.zst`` files (each line's ``raw`` field)."""
    import json

    from kbtc.recorders.writer import read_records

    for path in paths:
        for rec in read_records(path):
            try:
                yield json.loads(rec["raw"])
            except (KeyError, ValueError):
                continue
