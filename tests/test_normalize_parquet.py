"""Parquet schema, exact (no-float) round-trip, settlement normalization, Kalshi books, and the
end-to-end CLI over recorded ``.jsonl.zst`` inputs (ROADMAP 2.1)."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import polars as pl
from conftest import load_fixture

from kbtc.normalize import pipeline, schema, writer
from kbtc.normalize import __main__ as cli
from kbtc.recorders import settlement as st
from kbtc.recorders.writer import RotatingZstdWriter

T0 = 1_760_000_000_000_000_000  # 2025-10-09 UTC


def _records(venue, name):
    for i, msg in enumerate(load_fixture(venue, name)):
        yield T0 + i, msg


# ---------------------------------------------------------------------------------------
# Schema + exact round-trip (prices Decimal, no float drift; ts int ns).
# ---------------------------------------------------------------------------------------
def test_book_parquet_roundtrip_is_exact(tmp_path):
    res = pipeline.normalize_crypto("kraken", "level3", _records("kraken", "level3.jsonl"))
    df = schema.rows_to_df(res.books, schema.BOOK_SCHEMA)
    assert df["price"].dtype == pl.Decimal(precision=38, scale=8)
    assert df["recv_ts_ns"].dtype == pl.Int64
    paths = writer.write_partitioned(tmp_path, schema.DS_BOOKS, df,
                                     venue="kraken", channel="level3")
    assert len(paths) == 1
    back = pl.read_parquet(paths[0])
    # exact equality of the whole frame after a write→read cycle (no float drift).
    assert back.sort(["snap_id", "side", "level"]).equals(df.sort(["snap_id", "side", "level"]))
    # prices are exact Decimals, not floats.
    assert set(back["price"].to_list()) >= {Decimal("99.8"), Decimal("100.5")}


def test_empty_rows_make_schema_stable_empty_frame():
    df = schema.rows_to_df([], schema.TRADE_SCHEMA)
    assert df.is_empty()
    assert df.schema["price"] == pl.Decimal(precision=38, scale=8)
    assert writer.write_partitioned("/tmp/does-not-matter", schema.DS_TRADES, df) == []


# ---------------------------------------------------------------------------------------
# Kalshi yes/no books.
# ---------------------------------------------------------------------------------------
def test_kalshi_book_normalization(tmp_path):
    res = pipeline.normalize_kalshi("market", _records("kalshi", "orderbook.jsonl"))
    df = schema.rows_to_df(res.books, schema.KALSHI_BOOK_SCHEMA)
    paths = writer.write_partitioned(tmp_path, schema.DS_KALSHI_BOOKS, df,
                                     venue="kalshi", channel="market")
    back = pl.read_parquet(paths[0])
    final = back.filter(pl.col("snap_id") == back["snap_id"].max()).sort(["side", "level"])
    # after delta seq2 (+50 on yes 40) and seq3 (remove yes 41); the seq5 'no' delta is a seq-gap
    # (seq 4 missing) and is dropped, so no 58 stays 120 — a gap is never silently applied.
    got = [(r["side"], r["price"], r["size"]) for r in final.iter_rows(named=True)]
    assert got == [("no", Decimal("58"), Decimal("120")),
                   ("yes", Decimal("40"), Decimal("150"))]


# ---------------------------------------------------------------------------------------
# Settlements.
# ---------------------------------------------------------------------------------------
def _envelopes():
    envs = []
    for name in ("market_finalized_yes", "market_finalized_no", "market_closed_unsettled"):
        m = json.loads(
            Path(f"tests/fixtures/kalshi_settlement/{name}.json").read_text())["market"]
        envs.append(st.parse_market(m, recv_ts_ns=T0).to_envelope())
    return envs


def test_settlement_normalization_values_and_types(tmp_path):
    rows = pipeline.settlement_rows(_envelopes())
    df = schema.rows_to_df(rows, schema.SETTLEMENT_SCHEMA)
    paths = writer.write_partitioned(tmp_path, schema.DS_SETTLEMENTS, df)
    back = pl.read_parquet(paths[0]).sort("market_ticker")

    yes = back.filter(pl.col("result") == "yes").row(0, named=True)
    assert yes["settlement_value"] == Decimal("1.0000")
    assert yes["close_value"] == Decimal("81504.11")   # expiration_value (close 60s avg)
    assert yes["open_value"] == Decimal("81501.40")    # floor_strike (open ref / strike)
    assert yes["close_value"] >= yes["open_value"]      # result=="yes" ⇒ close ≥ open (0.1 rule)

    no = back.filter(pl.col("result") == "no").row(0, named=True)
    assert no["settlement_value"] == Decimal("0.0000")
    assert no["close_value"] < no["open_value"]

    unsettled = back.filter(pl.col("status") == "active").row(0, named=True)
    assert unsettled["result"] == "" and unsettled["close_value"] is None
    # raw provenance is preserved and parseable.
    assert json.loads(yes["raw_json"])["ticker"] == yes["market_ticker"]


# ---------------------------------------------------------------------------------------
# End-to-end CLI over recorded .jsonl.zst inputs.
# ---------------------------------------------------------------------------------------
def _write_raw(root: Path, venue: str, channel: str, msgs: list[dict]) -> None:
    w = RotatingZstdWriter(root, venue, channel)
    try:
        for i, m in enumerate(msgs):
            w.write(json.dumps(m), recv_ts_ns=T0 + i)
    finally:
        w.close()


def test_cli_end_to_end(tmp_path):
    root = tmp_path / "raw"
    out = tmp_path / "clean"
    _write_raw(root, "kraken", "level3", load_fixture("kraken", "level3.jsonl"))
    _write_raw(root, "bybit", "perp", load_fixture("bybit", "orderbook.jsonl"))
    _write_raw(root, "kalshi", "market", load_fixture("kalshi", "orderbook.jsonl"))
    # settlement raw: the SettlementRecord envelopes, as the recorder stores them.
    sw = RotatingZstdWriter(root, "kalshi", "settlement")
    try:
        for i, env in enumerate(_envelopes()):
            sw.write(json.dumps(env, default=str), recv_ts_ns=T0 + i)
    finally:
        sw.close()

    rc = cli.main(["--root", str(root), "--out", str(out)])
    assert rc == 0

    # books (crypto) + kalshi_books + trades + settlements datasets all produced.
    kraken_books = pl.read_parquet(
        out / "books" / "venue=kraken" / "channel=level3" / "date=2025-10-09" / "part.parquet")
    assert kraken_books.height > 0
    assert kraken_books["price"].dtype == pl.Decimal(precision=38, scale=8)

    kalshi_books = pl.read_parquet(
        out / "kalshi_books" / "venue=kalshi" / "channel=market" / "date=2025-10-09"
        / "part.parquet")
    assert kalshi_books.height > 0

    kalshi_trades = pl.read_parquet(
        out / "trades" / "venue=kalshi" / "channel=market" / "date=2025-10-09" / "part.parquet")
    assert kalshi_trades.height == 1 and kalshi_trades.row(0, named=True)["side"] == "yes"

    settlements = pl.read_parquet(out / "settlements" / "date=2025-10-09" / "part.parquet")
    assert settlements.height == 3
    assert settlements.filter(pl.col("result") == "yes").row(0, named=True)["close_value"] \
        == Decimal("81504.11")


def test_cli_date_filter(tmp_path):
    root = tmp_path / "raw"
    out = tmp_path / "clean"
    _write_raw(root, "kraken", "level3", load_fixture("kraken", "level3.jsonl"))
    rc = cli.main(["--root", str(root), "--out", str(out), "--date", "2099-01-01"])
    assert rc == 0
    # no data for that date ⇒ nothing written.
    assert not (out / "books").exists()
