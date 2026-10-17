"""Replay tests for the KXBTC15M settlement capture (ROADMAP 1.2).

Fixtures in tests/fixtures/kalshi_settlement/ are REAL trimmed Kalshi production responses
captured 2026-10-16 (reads only), except market_closed_unsettled.json which is a real market
object edited to the unsettled state (status=active, blank result) to exercise the
completeness flag. Fields verified in settlement.py's module docstring.
"""
from __future__ import annotations

import json
import pathlib

from kbtc.recorders.settlement import (
    SettlementCapture,
    check_completeness,
    check_result_invariant,
    enumerate_markets,
    iso_to_ns,
    parse_market,
)

FX = pathlib.Path(__file__).parent / "fixtures" / "kalshi_settlement"


def _market(name: str) -> dict:
    return json.loads((FX / name).read_text())["market"]


def _page(name: str) -> dict:
    return json.loads((FX / name).read_text())


# ---------------------------------------------------------------------------------------
# Field parsing: result + numeric settlement value (and the open reference).
# ---------------------------------------------------------------------------------------
def test_parse_finalized_no():
    rec = parse_market(_market("market_finalized_no.json"), recv_ts_ns=123)
    assert rec.market_ticker == "KXBTC15M-26OCT162130-30"
    assert rec.status == "finalized"
    assert rec.result == "no"
    assert rec.is_settled is True
    # per-contract YES payout = $0 for a NO result
    assert str(rec.settlement_value) == "0.0000"
    # numeric settlement value = close-window 60s BRTI average
    assert str(rec.close_value) == "81423.86"
    # open-window reference / strike
    assert str(rec.open_value) == "81442.59"
    # result == "yes" iff close_value >= open_value  -> here 81423.86 < 81442.59 -> no
    assert rec.close_value < rec.open_value
    assert rec.recv_ts_ns == 123
    # raw payload kept untouched alongside the parsed fields
    assert rec.raw_payload["ticker"] == rec.market_ticker
    env = rec.to_envelope()
    assert env["close_value"] == "81423.86" and env["raw_payload"]["result"] == "no"


def test_parse_finalized_yes():
    rec = parse_market(_market("market_finalized_yes.json"), recv_ts_ns=1)
    assert rec.result == "yes"
    assert str(rec.settlement_value) == "1.0000"
    assert str(rec.close_value) == "81504.11"
    assert str(rec.open_value) == "81501.4"
    assert rec.close_value >= rec.open_value  # yes iff close >= open
    # timestamps parsed to ns; close precedes settle, which is within ~seconds
    assert rec.close_time_ns == iso_to_ns("2026-10-17T00:15:00Z")
    assert rec.settle_time_ns > rec.close_time_ns


def test_parse_unsettled_has_no_values():
    rec = parse_market(_market("market_closed_unsettled.json"), recv_ts_ns=1)
    assert rec.result == ""
    assert rec.is_settled is False
    assert rec.settlement_value is None  # settlement_value_dollars absent before settling
    assert rec.close_value is None  # expiration_value is "" before settling


# ---------------------------------------------------------------------------------------
# Completeness: a closed-but-unsettled market is FLAGGED, and a missing boundary too.
# ---------------------------------------------------------------------------------------
def test_completeness_flags_closed_unsettled():
    settled = _market("market_finalized_no.json")  # close 01:30, finalized
    unsettled = _market("market_closed_unsettled.json")  # close 01:45, active
    start = iso_to_ns("2026-10-17T01:30:00Z")
    end = iso_to_ns("2026-10-17T01:45:00Z")
    recv = iso_to_ns("2026-10-17T02:00:00Z")  # both closes are in the past
    issues = check_completeness([settled, unsettled], start, end, recv)
    assert len(issues) == 1
    assert issues[0].reason == "closed_unsettled"
    assert issues[0].market_ticker == "KXBTC15M-26OCT162145-45"
    assert issues[0].close_time_ns == end


def test_completeness_flags_missing_boundary():
    settled = _market("market_finalized_no.json")  # only the 01:30 boundary present
    start = iso_to_ns("2026-10-17T01:30:00Z")
    end = iso_to_ns("2026-10-17T02:00:00Z")  # expects 01:30, 01:45, 02:00
    recv = iso_to_ns("2026-10-17T02:30:00Z")
    issues = check_completeness([settled], start, end, recv)
    reasons = {(i.reason, i.close_time_ns) for i in issues}
    assert ("missing_market", iso_to_ns("2026-10-17T01:45:00Z")) in reasons
    assert ("missing_market", iso_to_ns("2026-10-17T02:00:00Z")) in reasons
    assert all(i.reason == "missing_market" for i in issues)


def test_completeness_happy_path_no_issues():
    markets = _page("enumerate_page1.json")["markets"] + _page("enumerate_page2.json")["markets"]
    start = iso_to_ns("2026-10-17T00:00:00Z")
    end = iso_to_ns("2026-10-17T01:00:00Z")  # 5 boundaries, all finalized in the fixtures
    recv = iso_to_ns("2026-10-17T02:00:00Z")
    assert check_completeness(markets, start, end, recv) == []


# ---------------------------------------------------------------------------------------
# Result<->inequality invariant: a swapped result vs values is flagged, not written clean.
# ---------------------------------------------------------------------------------------
def test_result_invariant_flags_mismatch():
    m = _market("market_finalized_no.json")  # result=no, close 81423.86 < open 81442.59 (consistent)
    m["result"] = "yes"  # now INCONSISTENT: result says yes but close < open
    rec = parse_market(m, recv_ts_ns=5)
    issue = check_result_invariant(rec, recv_ts_ns=5)
    assert issue is not None
    assert issue.reason == "result_inconsistent"
    assert issue.market_ticker == "KXBTC15M-26OCT162130-30"
    assert "expected result=no" in issue.detail


def test_result_invariant_ok_on_consistent_and_tie():
    assert check_result_invariant(parse_market(_market("market_finalized_no.json"), 1), 1) is None
    assert check_result_invariant(parse_market(_market("market_finalized_yes.json"), 1), 1) is None
    # exact tie -> YES: close_value == open_value with result=yes must pass
    m = _market("market_finalized_yes.json")
    m["floor_strike"] = m["expiration_value"]  # open == close
    assert check_result_invariant(parse_market(m, 1), 1) is None


def test_capture_flags_result_inconsistent_but_keeps_raw():
    bad = _market("market_finalized_no.json")
    bad["result"] = "yes"  # poisoned: result contradicts close < open

    def fetch(_url: str) -> dict:
        return {"markets": [bad], "cursor": ""}

    writer = FakeWriter()
    clock = lambda: iso_to_ns("2026-10-17T02:00:00Z")  # noqa: E731
    cap = SettlementCapture(writer=writer, fetch=fetch, clock_ns=clock)
    start = iso_to_ns("2026-10-17T01:30:00Z")
    end = iso_to_ns("2026-10-17T01:30:00Z")
    issues = cap.capture_range(start, end)
    inconsistent = [i for i in issues if i.reason == "result_inconsistent"]
    assert len(inconsistent) == 1
    # raw payload IS still persisted (nothing lost) even though the record is flagged.
    assert len(writer.lines) == 1
    assert json.loads(writer.lines[0]["raw"])["raw_payload"]["result"] == "yes"


# ---------------------------------------------------------------------------------------
# Pagination: follow the cursor across a 2-page enumeration.
# ---------------------------------------------------------------------------------------
def _two_page_fetch(calls: list[str] | None = None):
    def fetch(url: str) -> dict:
        if calls is not None:
            calls.append(url)
        return _page("enumerate_page2.json") if "cursor=" in url else _page("enumerate_page1.json")

    return fetch


def test_enumerate_follows_cursor_across_pages():
    calls: list[str] = []
    markets = list(enumerate_markets({"series_ticker": "KXBTC15M"}, fetch=_two_page_fetch(calls)))
    assert len(markets) == 5  # 3 on page 1 + 2 on page 2
    tickers = [m["ticker"] for m in markets]
    assert len(set(tickers)) == 5  # no overlap
    assert len(calls) == 2  # stopped after page 2 (empty cursor)


# ---------------------------------------------------------------------------------------
# Capture orchestration: idempotent / resumable, writes raw+parsed, logs gaps.
# ---------------------------------------------------------------------------------------
class FakeWriter:
    def __init__(self) -> None:
        self.lines: list[dict] = []

    def write(self, raw: str, recv_ts_ns: int | None = None) -> None:
        self.lines.append({"recv_ts_ns": recv_ts_ns, "raw": raw})


def test_capture_range_writes_and_is_idempotent():
    writer = FakeWriter()
    clock = lambda: iso_to_ns("2026-10-17T02:00:00Z")  # noqa: E731
    cap = SettlementCapture(writer=writer, fetch=_two_page_fetch(), clock_ns=clock)
    start = iso_to_ns("2026-10-17T00:00:00Z")
    end = iso_to_ns("2026-10-17T01:00:00Z")

    issues = cap.capture_range(start, end)
    assert issues == []  # all 5 boundaries settled
    assert len(writer.lines) == 5  # five settled markets written
    # envelope keeps parsed fields AND the untouched raw payload, stamped recv_ts_ns
    env = json.loads(writer.lines[0]["raw"])
    assert env["raw_payload"]["ticker"] == env["market_ticker"]
    assert writer.lines[0]["recv_ts_ns"] == clock()

    # Re-running the SAME capture writes nothing new (seen within the process).
    cap.capture_range(start, end)
    assert len(writer.lines) == 5


def test_capture_resumes_from_prior_output():
    # First run populates an output; a fresh capture seeds `seen` from it and re-writes nothing.
    writer = FakeWriter()
    clock = lambda: iso_to_ns("2026-10-17T02:00:00Z")  # noqa: E731
    cap1 = SettlementCapture(writer=writer, fetch=_two_page_fetch(), clock_ns=clock)
    start = iso_to_ns("2026-10-17T00:00:00Z")
    end = iso_to_ns("2026-10-17T01:00:00Z")
    cap1.capture_range(start, end)
    assert len(writer.lines) == 5

    def read_records(_path: str):
        yield from writer.lines

    writer2 = FakeWriter()
    cap2 = SettlementCapture(writer=writer2, fetch=_two_page_fetch(), clock_ns=clock)
    cap2.load_seen(["prior.jsonl.zst"], read_records=read_records)
    assert len(cap2.seen) == 5
    cap2.capture_range(start, end)
    assert writer2.lines == []  # everything already captured -> no duplicates


def test_capture_passes_inclusive_close_ts_params():
    # The fetch URL must carry min/max_close_ts in SECONDS matching the requested ns range,
    # so the venue-side inclusive close_time filter is exercised end-to-end.
    seen_urls: list[str] = []

    def fetch(url: str) -> dict:
        seen_urls.append(url)
        return {"markets": [], "cursor": ""}

    cap = SettlementCapture(writer=FakeWriter(), fetch=fetch, clock_ns=lambda: 0)
    start = iso_to_ns("2026-10-17T00:00:00Z")
    end = iso_to_ns("2026-10-17T01:00:00Z")
    cap.capture_range(start, end)
    assert len(seen_urls) == 1
    assert f"min_close_ts={start // 1_000_000_000}" in seen_urls[0]
    assert f"max_close_ts={end // 1_000_000_000}" in seen_urls[0]
    assert "series_ticker=KXBTC15M" in seen_urls[0]
