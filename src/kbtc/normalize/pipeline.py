"""Replay-driven normalization pipeline (ROADMAP 2.1).

Each raw recording is replayed through the SAME pure venue ``MessageProcessor`` the recorders run
(reused via ``recorders.health.make_processor`` — the book logic is NOT reimplemented here). On
each book-changing update we snapshot the reconstructed top-N L2 book (event-driven; see
``schema.py``), normalize the trade tape, and validate against the venue checkpoint oracle.

Inputs are ``(recv_ts_ns, raw)`` pairs so tests can drive the fixtures directly and the CLI can
wrap ``recorders.writer.read_records``. Reads/transforms only — no network, no order path.
"""
from __future__ import annotations

import logging
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
from kbtc.recorders.resync import parse_rest_snapshot_record

from . import checkpoint as ckpt
from . import extract

logger = logging.getLogger("kbtc.normalize")
logger.addHandler(logging.NullHandler())

# Prime functions by venue (REST-snapshot venues need a baseline not in the recording). Reused
# verbatim from the health module so normalization and health agree on replay seeding.
_PRIME = {"coinbase": _prime_coinbase, "binance": _prime_binance}


def _is_recorded_complete_snapshot(venue: str, msg: Any) -> bool:
    """True iff ``msg`` is a RECORDED complete-book snapshot that establishes a full baseline.

    This covers the in-band-snapshot venues whose snapshot rides in the WS stream (Kraken/Bybit
    ``snapshot``, Kalshi ``orderbook_snapshot``). Coinbase/Binance/Bitstamp seed from a REST
    snapshot; as of ROADMAP 1.1c the recorder PERSISTS that snapshot into the recording as a tagged
    envelope (``parse_rest_snapshot_record``), which ``normalize_crypto`` handles SEPARATELY (it
    routes ``body`` to ``apply_snapshot`` and marks the baseline there). An OLD, snapshot-less
    recording of those venues carries no such frame → they never reach a complete baseline offline
    (``synced`` stays False — the honest result; the reconstructed book is partial). See schema.py
    COMPLETENESS MARKER.
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
    if v == "coinbase_advanced":
        return msg.get("channel") == "l2_data" and any(
            isinstance(e, dict) and e.get("type") == "snapshot"
            for e in (msg.get("events") or [])
        )
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
    """Replay a crypto feed → event-driven top-N L2 book rows + trade rows + checkpoint report.

    Buffers the whole result in memory; fine for tests/small slices. The CLI uses
    :func:`stream_crypto` instead, which yields the SAME rows in the SAME order without buffering,
    so peak memory is independent of total data size (the OOM-at-scale fix).
    """
    report = ckpt.new_report(venue, channel)
    res = NormalizeResult(checkpoint=report)
    for kind, row in stream_crypto(venue, channel, records, report, levels=levels):
        (res.books if kind == "book" else res.trades).append(row)
    return res


# Event kinds yielded by the streaming normalizers.
BOOK = "book"
TRADE = "trade"


def stream_crypto(venue: str, channel: str, records: Iterable[Record], report: ckpt.CheckpointReport,
                  levels: int = 10) -> Iterator[tuple[str, dict]]:
    """Streaming core of :func:`normalize_crypto`: yield ``(kind, row)`` as replay proceeds.

    ``kind`` is :data:`BOOK` or :data:`TRADE`. Rows and their order are IDENTICAL to what
    ``normalize_crypto`` accumulates (the parity test asserts this), but nothing is retained here —
    the caller flushes to parquet in bounded chunks. ``report`` is mutated in place with the
    checkpoint-validation results (Kraken CRC / Bybit), exactly as before.
    """
    proc, _prime = make_processor(venue, channel)
    prime = _PRIME.get(venue.lower())
    if proc is None:  # pragma: no cover - every crypto venue has a processor
        return

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

        # 0) Persisted REST snapshot (ROADMAP 1.1c). Coinbase/Binance/Bitstamp recordings may now
        # OPEN with (and re-anchor on) a tagged REST-snapshot envelope. Route its ``body`` to
        # ``apply_snapshot`` to establish a COMPLETE book baseline — the pre-existing resting orders
        # become part of the reconstruction, not just post-recording deltas. This IS the resync, so
        # it clears any pending divergence and establishes the baseline → ``synced`` goes True.
        rest = parse_rest_snapshot_record(msg)
        if rest is not None:
            # N1 (defense-in-depth): only apply a snapshot stamped for THIS feed. A cross-wired or
            # corrupt record carrying a foreign venue/channel must not seed the wrong processor —
            # skip and log, don't crash.
            rec_venue = str(rest.get("venue", "")).lower()
            rec_channel = rest.get("channel")
            if rec_venue != venue.lower() or rec_channel != channel:
                logger.warning(
                    "skipping REST snapshot stamped for %s/%s on %s/%s feed",
                    rest.get("venue"), rec_channel, venue, channel,
                    extra={"event": "rest_snapshot_feed_mismatch", "venue": venue,
                           "channel": channel, "record_venue": rest.get("venue"),
                           "record_channel": rec_channel},
                )
                continue
            # N2 (resilience): a malformed body must not abort the whole replay. On failure, log,
            # leave the baseline unset (subsequent rows stay synced=False), and move on.
            try:
                proc.apply_snapshot(rest["body"], recv)
            except Exception as exc:  # noqa: BLE001 - any parse/shape error in the stored body
                logger.warning(
                    "malformed REST snapshot on %s/%s skipped: %s", venue, channel, exc,
                    extra={"event": "rest_snapshot_malformed", "venue": venue, "channel": channel},
                )
                continue
            baseline_established = True
            pending_resync = False
            primed = True  # the persisted snapshot IS the baseline; no first-message priming needed
            rows, snap_id, last_fp = _emit_book(proc, levels, recv, None, venue, channel,
                                                snap_id, last_fp, synced=True)
            for r in rows:
                yield (BOOK, r)
            continue

        # 1) Trade tape (independent of the book processor).
        for t in extract.trade_rows(venue, channel, raw):
            t["recv_ts_ns"] = recv
            yield (TRADE, t)

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
            # Re-priming (empty-reseed from the next message) only seeds the INITIAL baseline for a
            # REST-snapshot venue whose recording carries NO persisted snapshot (old format). Once a
            # persisted REST snapshot has established a baseline, re-anchoring comes from the next
            # persisted snapshot (handled above), so we must NOT re-prime — the book stays diverged
            # and is honestly emitted as ``synced=False`` until that snapshot lands.
            if prime is not None and not baseline_established:
                primed = False

        synced = baseline_established and not pending_resync

        # 5) Emit a top-N L2 snapshot if the top-N actually changed. We gate on the book being
        # non-empty (``rows``) rather than ``proc.synced``: Coinbase/Binance buffer pre-baseline
        # frames (book stays empty → no emit), while Bitstamp never sets ``synced`` offline (it has
        # no snapshot message — it applies events straight to the book), so a ``synced`` gate would
        # wrongly suppress every Bitstamp snapshot. The completeness ``synced`` flag is a COLUMN,
        # not a filter — rows are always kept, just marked.
        if do_emit and getattr(proc, "book", None) is not None:
            exch = extract.exch_ts_ns(venue, msg)
            rows, snap_id, last_fp = _emit_book(proc, levels, recv, exch, venue, channel,
                                                snap_id, last_fp, synced=synced)
            for r in rows:
                yield (BOOK, r)


def _emit_book(proc: Any, levels: int, recv: int, exch: int | None,
               venue: str, channel: str, snap_id: int, last_fp: tuple | None,
               synced: bool) -> tuple[list[dict], int, tuple | None]:
    """One top-N L2 snapshot iff the aggregated top-N changed; return ``(rows, snap_id, last_fp)``.

    Shared by the normal delta path and the persisted-REST-snapshot path so both increment
    ``snap_id`` and dedupe against ``last_fp`` identically (no divergent emit logic). Returns a
    SMALL (≤2·levels) row list the caller yields — never accumulates a whole run.
    """
    if getattr(proc, "book", None) is None:
        return [], snap_id, last_fp
    rows = extract.l2_levels(proc.book, levels)
    fp = extract.book_fingerprint(rows)
    out: list[dict] = []
    if rows and fp != last_fp:
        last_fp = fp
        snap_id += 1
        for side, lvl, price, size in rows:
            out.append(_book_row(recv, exch, venue, proc.product, channel,
                                 snap_id, synced, side, lvl, price, size))
    return out, snap_id, last_fp


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
    report = kalshi_report(channel)
    res = NormalizeResult(checkpoint=report)
    for kind, row in stream_kalshi(channel, records):
        (res.books if kind == BOOK else res.trades).append(row)
    return res


def kalshi_report(channel: str) -> ckpt.CheckpointReport:
    """The fixed deferred checkpoint report for a Kalshi feed (no cross-book oracle; 1.1b)."""
    return ckpt.CheckpointReport(
        "kalshi", channel, ckpt.METHOD_DEFERRED,
        notes="Kalshi payload/seq schema UNVERIFIED (ROADMAP 1.1b); no cross-book checkpoint oracle",
    )


def stream_kalshi(channel: str, records: Iterable[Record]) -> Iterator[tuple[str, dict]]:
    """Streaming core of :func:`normalize_kalshi`: yield ``(kind, row)`` without buffering.

    Same rows/order as ``normalize_kalshi`` accumulates (parity test asserts it)."""
    proc, _ = make_processor("kalshi", channel)
    if proc is None:  # pragma: no cover
        return

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
            yield (TRADE, t)

        proc.process(raw, recv)

        typ = msg.get("type") if isinstance(msg, dict) else None
        if typ not in {"orderbook_snapshot", "orderbook_delta"}:
            continue
        # Robust to the UNVERIFIED msg-wrapper (1.1b): fields may be nested under ``msg`` or FLAT.
        inner = msg.get("msg") if isinstance(msg, dict) else None
        body = inner if isinstance(inner, dict) else (msg if isinstance(msg, dict) else {})
        ticker = body.get("market_ticker", proc.product)
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
                yield (BOOK, _book_row(recv, None, "kalshi", ticker, channel,
                                       snap_id, synced, side, lvl, price, size))


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
