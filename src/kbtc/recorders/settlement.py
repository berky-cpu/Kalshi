"""Kalshi KXBTC15M settlement capture — ground-truth results + settlement values.

ROADMAP 1.2. This records, for EVERY 15-minute KXBTC15M market, its final result and
numeric settlement value. It is the ground truth for the Phase-2 tracking-error study and
Phase-3 calibration, so **completeness matters as much as correctness**: a market that
closed but has not settled, or a 15-minute boundary with no market at all, is LOGGED as a
structured issue (mirroring the 1.1 "gaps are never silent" principle) — never dropped.

CLAUDE.md HARD RULE 1: this module reads MARKET DATA ONLY from the Kalshi *production* REST
API. There is no order path and none may be added here. Public market data needs no auth;
if auth is ever supplied it comes from env only and is never printed or logged.

VERIFICATION (primary source: Kalshi production REST, read-only, 2026-10-16)
--------------------------------------------------------------------------------
GET https://api.elections.kalshi.com/trade-api/v2/markets/<ticker>
GET https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&...

Fields on a settled market object (VERIFIED, examples from KXBTC15M-26OCT162130-30 and
KXBTC15M-26OCT162015-15):

* ``status``           -> "finalized" for a settled market. The *query filter* value is
                          ``status=settled`` (which returns status=="finalized" objects);
                          ``status=finalized`` is REJECTED ("invalid status filter").
                          Lifecycle seen: initialized -> active -> finalized.
* ``result``           -> "yes" | "no"  (empty string "" before settlement). VERIFIED.
* ``settlement_value_dollars`` -> per-contract YES payout as a dollar STRING:
                          "1.0000" when result=="yes", "0.0000" when "no". This is the
                          BINARY PAYOUT, not the BTC price. Absent before settlement.
* ``expiration_value`` -> STRING BTC price, e.g. "81423.86". This is the authoritative
                          numeric settlement quantity (VERIFIED as the settling value). That
                          it is specifically the CLOSE-window **60 s BRTI average** (rounded
                          to $0.01) is INFERRED/PARTIAL — inherited from 0.1's contract-rules
                          reading, and the window endpoint (``[T-59,T]`` vs ``[T-60,T-1]``)
                          is still PARTIAL (see facts.md 0.1/0.3). The field role/value is
                          VERIFIED; the 60 s-average identity is not independently confirmed.
                          CROSS-CHECKED: window N's ``expiration_value`` == window N+1's
                          ``floor_strike`` (the close avg becomes the next open reference),
                          and ``result`` == "yes" iff expiration_value >= floor_strike.
                          Empty "" before settlement.
* ``floor_strike``     -> FLOAT BTC price, e.g. 81442.59 = the OPEN-window 60 s BRTI average
                          = the reference/strike, fixed at open (also in ``yes_sub_title``
                          "Target Price: $81,442.59"). Present once the market is active.
* ``close_time``       -> economic close = END of the settlement reference window. USE THIS.
* ``open_time``        -> window open = end of the open-reference window.
* ``settlement_ts``    -> determination/settle instant, ~1-6 s after close_time.
* ``settlement_timer_seconds`` -> 1 (observed).
* ``expiration_time`` / ``latest_expiration_time`` -> ~1 week out, an administrative
                          backstop. **Do NOT use as the settle instant** (confirms 0.1).
* ``expected_expiration_time`` / ``occurrence_datetime`` -> close_time + 5 min (admin).

Enumeration / completeness (VERIFIED):
* One market per event; ticker = ``KXBTC15M-<YYMONDDHHMM>-<MM>`` where the suffix is the
  close minute-of-hour (-00/-15/-30/-45), NOT always "-00".
* Pagination: response carries ``cursor``; pass it back as ``&cursor=...``; an empty/absent
  cursor means no more pages. Pages verified non-overlapping.
* ``min_close_ts`` / ``max_close_ts`` (unix SECONDS, inclusive) filter by ``close_time`` —
  so a date range maps to an exact, countable set of 15-minute boundaries, which is how we
  PROVE capture is complete (every boundary must resolve to a finalized market).

So: the result AND the numeric settlement value (close 60 s BRTI avg) AND the open reference
are all exposed by the API — no BRTI reconstruction is needed for ground truth. (Phase 2
still reconstructs BRTI to *study tracking error* against these published values.)
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("kbtc.recorders.settlement")
logger.addHandler(logging.NullHandler())

now_ns = time.time_ns

# Production host, READ-ONLY. No order endpoint is ever referenced here.
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"
WINDOW_S = 15 * 60  # 15-minute cadence, aligned to clock quarter-hours (facts.md 0.1)

SETTLED_STATUS = "finalized"  # the value the object carries once settled (VERIFIED)
SETTLED_FILTER = "settled"  # the query-param value that selects finalized markets (VERIFIED)


# ---------------------------------------------------------------------------------------
# Time helpers (all internal timestamps are integer ns UTC, per CLAUDE.md conventions).
# ---------------------------------------------------------------------------------------
def iso_to_ns(s: str | None) -> int | None:
    """Parse an RFC3339/ISO-8601 UTC string (``...Z``) to integer nanoseconds, or None."""
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000_000)


def ns_to_s(ns: int) -> int:
    return ns // 1_000_000_000


def expected_close_times_ns(start_close_ns: int, end_close_ns: int) -> list[int]:
    """Every 15-minute clock boundary in ``[start, end]`` (inclusive), as ns.

    ``start``/``end`` are snapped to the enclosing quarter-hour grid; the result is the exact
    set of ``close_time`` values a complete KXBTC15M history must contain over the range.
    """
    step = WINDOW_S * 1_000_000_000
    first = ((start_close_ns + step - 1) // step) * step  # ceil to grid
    out: list[int] = []
    t = first
    while t <= end_close_ns:
        out.append(t)
        t += step
    return out


def _dec_or_none(x: Any) -> Decimal | None:
    """Parse a dollar/price STRING to Decimal; '' or None -> None. Never float."""
    if x is None:
        return None
    s = str(x).strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------------------
# Parsed record + completeness issue.
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SettlementRecord:
    """One market's settlement ground truth. Parsed fields + the untouched raw payload.

    ``settlement_value`` is Kalshi's per-contract YES payout in dollars (0 or 1), from
    ``settlement_value_dollars``. ``close_value`` is the numeric settlement quantity — the
    CLOSE-window 60 s BRTI average (``expiration_value``). ``open_value`` is the OPEN-window
    60 s reference / strike (``floor_strike``). All monetary values are Decimal, never float.
    """

    market_ticker: str
    event_ticker: str | None
    status: str
    result: str  # "yes" | "no" | "" (unsettled)
    settlement_value: Decimal | None  # per-contract payout $ (0/1)
    close_value: Decimal | None  # close-window 60s BRTI avg (numeric settlement value)
    open_value: Decimal | None  # open-window 60s BRTI avg (reference / strike)
    open_time_ns: int | None
    close_time_ns: int | None  # economic close = end of settlement window
    settle_time_ns: int | None  # determination instant
    recv_ts_ns: int  # stamped on receipt, before parsing
    raw_payload: dict  # the market object exactly as received (untouched)

    @property
    def is_settled(self) -> bool:
        return self.status == SETTLED_STATUS and self.result in ("yes", "no")

    def to_envelope(self) -> dict:
        """Serializable envelope: parsed fields (Decimals -> str) + untouched raw payload."""
        d = asdict(self)
        for k in ("settlement_value", "close_value", "open_value"):
            d[k] = None if d[k] is None else str(d[k])
        return d


@dataclass(frozen=True)
class CompletenessIssue:
    """A market that could not be captured as settled — LOGGED, never silently dropped."""

    reason: str  # "missing_market" | "closed_unsettled" | "result_inconsistent"
    close_time_ns: int
    market_ticker: str | None
    recv_ts_ns: int
    detail: str = ""

    def __str__(self) -> str:
        ct = datetime.fromtimestamp(self.close_time_ns / 1e9, tz=UTC).isoformat()
        return (
            f"SETTLEMENT_GAP reason={self.reason} close_time={ct} "
            f"market={self.market_ticker!r} detail={self.detail!r}"
        )


def log_issue(issue: CompletenessIssue, log: logging.Logger = logger) -> None:
    log.warning(
        "%s",
        issue,
        extra={
            "event": "settlement_gap",
            "reason": issue.reason,
            "close_time_ns": issue.close_time_ns,
            "market_ticker": issue.market_ticker,
            "recv_ts_ns": issue.recv_ts_ns,
        },
    )


# ---------------------------------------------------------------------------------------
# Pure parsing.
# ---------------------------------------------------------------------------------------
def parse_market(market: dict, recv_ts_ns: int) -> SettlementRecord:
    """Build a :class:`SettlementRecord` from a Kalshi market object (pure, no IO).

    The raw ``market`` dict is stored untouched on ``raw_payload``. Parsing is tolerant of
    the unsettled states (empty ``result``/``expiration_value``, absent settlement fields).
    """
    return SettlementRecord(
        market_ticker=market["ticker"],
        event_ticker=market.get("event_ticker"),
        status=market.get("status", ""),
        result=market.get("result", "") or "",
        settlement_value=_dec_or_none(market.get("settlement_value_dollars")),
        close_value=_dec_or_none(market.get("expiration_value")),
        open_value=_dec_or_none(market.get("floor_strike")),
        open_time_ns=iso_to_ns(market.get("open_time")),
        close_time_ns=iso_to_ns(market.get("close_time")),
        settle_time_ns=iso_to_ns(market.get("settlement_ts")),
        recv_ts_ns=recv_ts_ns,
        raw_payload=market,
    )


def check_result_invariant(rec: SettlementRecord, recv_ts_ns: int) -> CompletenessIssue | None:
    """Enforce ``(result == "yes") == (close_value >= open_value)`` at capture time.

    This is the load-bearing ground-truth invariant (0.1: close 60 s avg >= open 60 s avg,
    ties -> YES). It is checked only when the record is settled AND both values are present;
    a Kalshi schema change or a field swap that breaks it must NOT be written as clean ground
    truth, so on mismatch we return a structured ``result_inconsistent`` issue (the caller
    still persists the raw payload, but the record is flagged — "no silently-inconsistent
    records"). Uses ``>=`` to preserve the exact-tie -> YES rule.
    """
    if not rec.is_settled or rec.close_value is None or rec.open_value is None:
        return None
    expected_yes = rec.close_value >= rec.open_value
    if (rec.result == "yes") != expected_yes:
        return CompletenessIssue(
            "result_inconsistent",
            rec.close_time_ns if rec.close_time_ns is not None else recv_ts_ns,
            rec.market_ticker,
            recv_ts_ns,
            f"result={rec.result!r} but close_value={rec.close_value} "
            f"open_value={rec.open_value} (expected result="
            f"{'yes' if expected_yes else 'no'})",
        )
    return None


def check_completeness(
    markets: list[dict],
    start_close_ns: int,
    end_close_ns: int,
    recv_ts_ns: int,
) -> list[CompletenessIssue]:
    """Compare the markets seen over a close-time range against the expected 15-min grid.

    Emits an issue for (a) every expected boundary with NO market (``missing_market``) and
    (b) every market whose ``close_time`` has passed but is not finalized
    (``closed_unsettled``). ``recv_ts_ns`` doubles as "now" for the closed-unsettled test.
    """
    by_close: dict[int, dict] = {}
    for m in markets:
        ct = iso_to_ns(m.get("close_time"))
        if ct is not None:
            by_close[ct] = m

    issues: list[CompletenessIssue] = []
    for ct in expected_close_times_ns(start_close_ns, end_close_ns):
        m = by_close.get(ct)
        if m is None:
            issues.append(
                CompletenessIssue("missing_market", ct, None, recv_ts_ns,
                                  "no market found for this 15-min boundary")
            )
            continue
        rec = parse_market(m, recv_ts_ns)
        if not rec.is_settled and ct <= recv_ts_ns:
            issues.append(
                CompletenessIssue(
                    "closed_unsettled", ct, rec.market_ticker, recv_ts_ns,
                    f"close passed but status={rec.status!r} result={rec.result!r}",
                )
            )
    return issues


# ---------------------------------------------------------------------------------------
# HTTP (read-only) + pagination.
# ---------------------------------------------------------------------------------------
# A fetcher takes an absolute URL and returns the decoded JSON dict. Injected in tests so the
# pure enumeration logic runs with no network.
Fetcher = Callable[[str], dict]


def _default_fetch(url: str, timeout: float = 20.0) -> dict:
    """Read-only GET returning decoded JSON. No auth (public market data); never logs secrets."""
    req = Request(url, method="GET", headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host, reads only
        # parse_float=Decimal so JSON-number prices (e.g. floor_strike) never become binary
        # floats — keeps prices off the float boundary per CLAUDE.md conventions.
        return json.loads(resp.read().decode("utf-8"), parse_float=Decimal)


def markets_url(params: dict[str, Any], base: str = PROD_BASE) -> str:
    return f"{base}/markets?{urlencode(params)}"


def enumerate_markets(
    params: dict[str, Any],
    fetch: Fetcher = _default_fetch,
    base: str = PROD_BASE,
    max_pages: int = 10_000,
) -> Iterator[dict]:
    """Yield every market across ALL pages, following ``cursor`` until it is empty/absent.

    Fully paginating is what makes completeness provable — we never stop at the first page.
    ``series_ticker`` and any status/close-ts filters are passed in ``params``.
    """
    q = dict(params)
    for _ in range(max_pages):
        data = fetch(markets_url(q, base))
        for m in data.get("markets", []):
            yield m
        cursor = data.get("cursor") or ""
        if not cursor:
            return
        q["cursor"] = cursor


# ---------------------------------------------------------------------------------------
# Capture orchestration — idempotent, resumable, completeness-logged.
# ---------------------------------------------------------------------------------------
class SettlementCapture:
    """Enumerate KXBTC15M markets over a close-time range, record newly-settled ones, and
    log any market that closed but could not be captured as settled.

    * **Idempotent / resumable**: ``seen`` is seeded from the existing output (see
      :meth:`load_seen`), so re-running neither re-writes a market already captured nor loses
      its place — enumeration always restarts from a fresh cursor but writes only new tickers.
    * **Completeness is not silent**: :meth:`capture_range` returns (and logs) a
      :class:`CompletenessIssue` for every missing boundary or closed-but-unsettled market.

    ``writer`` follows the ``recorders/writer.py`` pattern: ``writer.write(raw_str,
    recv_ts_ns=...)`` persists the stamped, untouched envelope. ``fetch`` and ``clock_ns`` are
    injectable for tests.
    """

    def __init__(
        self,
        writer: Any,
        fetch: Fetcher = _default_fetch,
        base: str = PROD_BASE,
        series: str = SERIES,
        clock_ns: Callable[[], int] = now_ns,
        log: logging.Logger = logger,
    ) -> None:
        self.writer = writer
        self.fetch = fetch
        self.base = base
        self.series = series
        self.clock_ns = clock_ns
        self.log = log
        self.seen: set[str] = set()
        self.issues: list[CompletenessIssue] = []

    def load_seen(self, paths: list[str] | None, read_records: Callable[[str], Iterator[dict]]) -> None:
        """Seed ``seen`` from prior output files so re-runs don't duplicate (resumable).

        ``read_records`` is ``recorders.writer.read_records``. Each stored line is
        ``{"recv_ts_ns", "raw": <envelope json str>}``; a record is counted as seen only if it
        was captured as SETTLED (so an earlier closed-unsettled snapshot is re-fetched later).
        """
        for path in paths or []:
            for rec in read_records(path):
                try:
                    env = json.loads(rec["raw"])
                except (KeyError, json.JSONDecodeError):
                    continue
                if env.get("status") == SETTLED_STATUS and env.get("result") in ("yes", "no"):
                    self.seen.add(env.get("market_ticker"))

    def _record(self, rec: SettlementRecord) -> None:
        """Persist one settled market (untouched raw payload + parsed envelope, stamped)."""
        envelope = rec.to_envelope()
        # default=str serializes any Decimal left in the (untouched) raw payload — the fetcher
        # decodes JSON numbers as Decimal to keep prices off the float boundary.
        self.writer.write(
            json.dumps(envelope, separators=(",", ":"), default=str),
            recv_ts_ns=rec.recv_ts_ns,
        )
        self.seen.add(rec.market_ticker)

    def capture_range(self, start_close_ns: int, end_close_ns: int) -> list[CompletenessIssue]:
        """Capture every settled market with ``close_time`` in ``[start, end]``; log gaps.

        Enumerates WITHOUT a status filter so that closed-but-unsettled markets are visible to
        the completeness check (``status=settled`` alone would hide them). Writes each settled,
        not-yet-seen market once; returns the completeness issues (also logged).
        """
        params = {
            "series_ticker": self.series,
            "min_close_ts": ns_to_s(start_close_ns),
            "max_close_ts": ns_to_s(end_close_ns),
            "limit": 1000,
        }
        markets = list(enumerate_markets(params, self.fetch, self.base))
        recv = self.clock_ns()
        issues: list[CompletenessIssue] = []
        for m in markets:
            rec = parse_market(m, recv)
            if rec.is_settled and rec.market_ticker not in self.seen:
                # Enforce the result<->inequality invariant BEFORE trusting it as clean
                # ground truth. We still persist the raw payload (so nothing is lost), but a
                # breach is flagged as a structured issue rather than written silently.
                bad = check_result_invariant(rec, recv)
                if bad is not None:
                    issues.append(bad)
                self._record(rec)
        issues.extend(check_completeness(markets, start_close_ns, end_close_ns, recv))
        for issue in issues:
            log_issue(issue, self.log)
        self.issues.extend(issues)
        return issues


# ---------------------------------------------------------------------------------------
# CLI entrypoint (ROADMAP 1.3): ``python -m kbtc.recorders.settlement`` on a systemd timer.
# Reads MARKET DATA ONLY from Kalshi production REST; no order path. Public data needs no auth.
# ---------------------------------------------------------------------------------------
def main(
    argv: list[str] | None = None,
    *,
    fetch: Fetcher = _default_fetch,
    clock_ns: Callable[[], int] = now_ns,
) -> int:
    """Capture KXBTC15M settlements whose ``close_time`` falls in the recent lookback window.

    Designed for a periodic timer: it enumerates the just-closed boundaries, writes any newly
    settled market once (idempotent via ``load_seen`` over prior output), and LOGS any boundary
    that is still unsettled or missing. A still-unsettled very-recent window is NORMAL (settlement
    lags close by a few seconds), so this exits 0 on completeness issues — only a usage/IO error is
    non-zero. ``fetch``/``clock_ns`` are injectable for tests (no network).
    """
    import argparse
    import glob
    import sys
    from pathlib import Path

    from .writer import RotatingZstdWriter, read_records

    p = argparse.ArgumentParser(
        prog="python -m kbtc.recorders.settlement",
        description="Capture KXBTC15M settlement ground truth (reads only; no order path).",
    )
    p.add_argument("--root", default="data/raw", help="data root (default: data/raw)")
    p.add_argument("--lookback-min", type=float, default=30.0,
                   help="capture boundaries that closed within the last N minutes (default: 30)")
    p.add_argument("--base", default=PROD_BASE, help="Kalshi REST base (reads only)")
    p.add_argument("--no-resume", action="store_true",
                   help="do not seed 'seen' from existing output (re-writes already-captured)")
    args = p.parse_args(argv)

    end_ns = clock_ns()
    start_ns = end_ns - int(args.lookback_min * 60 * 1_000_000_000)

    writer = RotatingZstdWriter(args.root, "kalshi", "settlement")
    cap = SettlementCapture(writer, fetch=fetch, base=args.base, clock_ns=clock_ns)
    try:
        if not args.no_resume:
            existing = sorted(glob.glob(
                str(Path(args.root) / "kalshi" / "settlement" / "**" / "*.jsonl.zst"),
                recursive=True,
            ))
            cap.load_seen(existing, read_records)
        cap.capture_range(start_ns, end_ns)
    finally:
        writer.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
