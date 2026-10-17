"""Smoke test for the settlement CLI entrypoint (ROADMAP 1.3): no network, writes to a tmp root."""
from __future__ import annotations

from datetime import UTC, datetime

from kbtc.recorders import settlement
from kbtc.recorders.writer import read_records


def _finalized_market(close_dt: datetime) -> dict:
    close = close_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    settle = close_dt.replace(second=5).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "ticker": "KXBTC15M-26OCT160000-00",
        "event_ticker": "KXBTC15M-26OCT160000",
        "status": "finalized",
        "result": "no",
        "settlement_value_dollars": "0.0000",
        "expiration_value": "81423.86",
        "floor_strike": 81442.59,
        "open_time": "2026-10-15T23:45:00Z",
        "close_time": close,
        "settlement_ts": settle,
    }


def test_main_captures_recent_settlement(tmp_path):
    fixed_now_ns = int(datetime(2026, 10, 16, 0, 0, 10, tzinfo=UTC).timestamp() * 1e9)
    # Market closed 10 s ago -> inside the default 30-min lookback window.
    close_dt = datetime(2026, 10, 16, 0, 0, 0, tzinfo=UTC)

    def fake_fetch(url: str):
        # single page, no cursor
        return {"markets": [_finalized_market(close_dt)], "cursor": ""}

    rc = settlement.main(
        ["--root", str(tmp_path)],
        fetch=fake_fetch,
        clock_ns=lambda: fixed_now_ns,
    )
    assert rc == 0

    files = list((tmp_path / "kalshi" / "settlement").rglob("*.jsonl.zst"))
    assert files, "settlement output not written"
    records = [r for f in files for r in read_records(f)]
    assert len(records) == 1
    import json

    env = json.loads(records[0]["raw"])
    assert env["market_ticker"] == "KXBTC15M-26OCT160000-00"
    assert env["result"] == "no"
