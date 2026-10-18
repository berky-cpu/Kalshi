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
    # VERIFIED schema (dollar-string prices, fixed-point counts): after delta seq2 (+50 on yes
    # 0.40) and seq3 (remove yes 0.41); the seq5 'no' delta is a seq-gap (seq 4 missing) and is
    # dropped, so no 0.58 stays 120 — a gap is never silently applied.
    got = [(r["side"], r["price"], r["size"]) for r in final.iter_rows(named=True)]
    assert got == [("no", Decimal("0.58"), Decimal("120.00")),
                   ("yes", Decimal("0.40"), Decimal("150.00"))]


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
    assert kalshi_trades.height == 1
    kt = kalshi_trades.row(0, named=True)
    assert kt["side"] == "yes"                     # taker_side
    assert kt["price"] == Decimal("0.4100")        # yes_price_dollars (dollars)
    assert kt["size"] == Decimal("5.00")           # count_fp

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
    # no data for that date ⇒ nothing written (streaming writer creates dirs lazily).
    assert not (out / "books").exists()


# ---------------------------------------------------------------------------------------
# Streaming output == buffered output (the OOM-at-scale fix changed only memory, not rows).
# ---------------------------------------------------------------------------------------
def _synth_book_rows(n, t0):
    """n synthetic book rows on a single date, Decimal prices/sizes, strictly increasing ts."""
    out = []
    for i in range(n):
        side = "bid" if i % 2 == 0 else "ask"
        out.append({
            "recv_ts_ns": t0 + i * 1000, "exch_ts_ns": None, "venue": "bitstamp",
            "product": "BTC/USD", "channel": "live_orders_btcusd", "snap_id": i // 2 + 1,
            "synced": False, "side": side, "level": 0,
            "price": Decimal(f"{27000 + (i % 7)}.{i % 100:02d}"),
            "size": Decimal(f"{1 + (i % 5)}.{i % 10}"),
        })
    return out


def test_streaming_writer_parity_with_buffered(tmp_path):
    """PartitionedParquetWriter (row groups, many small chunks) == one buffered write_partitioned:
    same rows, same order, same schema. Proves chunking changed nothing but memory."""
    rows = _synth_book_rows(137, T0)  # odd count so the last chunk is partial

    # Buffered reference (the old path).
    buf_out = tmp_path / "buffered"
    df = schema.rows_to_df(rows, schema.BOOK_SCHEMA)
    writer.write_partitioned(buf_out, schema.DS_BOOKS, df,
                             venue="bitstamp", channel="live_orders_btcusd")
    ref = pl.read_parquet(buf_out / "books" / "venue=bitstamp"
                          / "channel=live_orders_btcusd" / "date=2025-10-09" / "part.parquet")

    # Streamed: feed the same rows in small chunks of 10 → multiple row groups in one part.parquet.
    str_out = tmp_path / "streamed"
    w = writer.PartitionedParquetWriter(str_out, schema.DS_BOOKS,
                                        venue="bitstamp", channel="live_orders_btcusd")
    for i in range(0, len(rows), 10):
        w.write_df(schema.rows_to_df(rows[i:i + 10], schema.BOOK_SCHEMA))
    paths = w.close()
    assert len(paths) == 1
    got = pl.read_parquet(paths[0])

    # Exact, order-preserving equality (no sort): same rows in the same sequence, same schema.
    assert got.equals(ref)
    assert got.schema == df.schema
    assert got.height == len(rows)
    # multiple row groups were actually written (streaming, not a single buffered write).
    import pyarrow.parquet as pq
    assert pq.ParquetFile(paths[0]).num_row_groups > 1


def test_cli_streaming_matches_buffered_normalize(tmp_path):
    """End-to-end: the streaming CLI writes exactly the rows the buffered normalizer produces."""
    root = tmp_path / "raw"
    out = tmp_path / "clean"
    _write_raw(root, "kraken", "level3", load_fixture("kraken", "level3.jsonl"))
    assert cli.main(["--root", str(root), "--out", str(out)]) == 0
    streamed = pl.read_parquet(
        out / "books" / "venue=kraken" / "channel=level3" / "date=2025-10-09" / "part.parquet")

    res = pipeline.normalize_crypto("kraken", "level3", _records("kraken", "level3.jsonl"))
    buffered = schema.rows_to_df(res.books, schema.BOOK_SCHEMA)
    assert streamed.equals(buffered)


# ---------------------------------------------------------------------------------------
# Peak memory is bounded as input grows (the fix): streaming stays flat, buffering grows.
# ---------------------------------------------------------------------------------------
def _peak_streaming(out_root, n, chunk, t0):
    import tracemalloc

    def gen():  # lazy — never materializes all rows
        for r in _synth_book_rows_iter(n, t0):
            yield r

    tracemalloc.start()
    w = writer.PartitionedParquetWriter(out_root, schema.DS_BOOKS,
                                        venue="bitstamp", channel="live_orders_btcusd")
    buf = []
    for r in gen():
        buf.append(r)
        if len(buf) >= chunk:
            w.write_df(schema.rows_to_df(buf, schema.BOOK_SCHEMA))
            buf.clear()
    if buf:
        w.write_df(schema.rows_to_df(buf, schema.BOOK_SCHEMA))
    w.close()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak


def _peak_buffered(out_root, n, t0):
    import tracemalloc
    tracemalloc.start()
    rows = list(_synth_book_rows_iter(n, t0))  # the old path holds every row
    df = schema.rows_to_df(rows, schema.BOOK_SCHEMA)
    writer.write_partitioned(out_root, schema.DS_BOOKS, df,
                             venue="bitstamp", channel="live_orders_btcusd")
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak


def _synth_book_rows_iter(n, t0):
    for i in range(n):
        side = "bid" if i % 2 == 0 else "ask"
        yield {
            "recv_ts_ns": t0 + i * 1000, "exch_ts_ns": None, "venue": "bitstamp",
            "product": "BTC/USD", "channel": "live_orders_btcusd", "snap_id": i // 2 + 1,
            "synced": False, "side": side, "level": 0,
            "price": Decimal(f"{27000 + (i % 7)}.{i % 100:02d}"),
            "size": Decimal(f"{1 + (i % 5)}.{i % 10}"),
        }


def test_peak_memory_bounded_when_streaming(tmp_path):
    small, big, chunk = 20_000, 80_000, 5_000

    s_small = _peak_streaming(tmp_path / "s1", small, chunk, T0)
    s_big = _peak_streaming(tmp_path / "s2", big, chunk, T0)
    b_small = _peak_buffered(tmp_path / "b1", small, T0)
    b_big = _peak_buffered(tmp_path / "b2", big, T0)

    # Streaming peak is FLAT: 4x the rows must not meaningfully grow peak (bounded by chunk size).
    assert s_big < 1.5 * s_small
    # Buffering peak GROWS with total rows (the pathology): 4x rows ⇒ clearly higher peak.
    assert b_big > 2.0 * b_small
    # At scale, streaming peak is a small fraction of the buffered peak.
    assert s_big < 0.5 * b_big


# ---------------------------------------------------------------------------------------
# Atomicity: the glob-visible part.parquet appears ONLY on clean completion (no partial files).
# ---------------------------------------------------------------------------------------
class _Boom(RuntimeError):
    pass


def _crashing_crypto_raw(root: Path, n_good: int, t0: int) -> None:
    """Write a Bitstamp live_orders recording whose Nth frame is malformed to blow up mid-replay."""
    import zstandard as zstd

    # A plausible-enough bitstamp live_orders stream; the processor applies order events. The
    # crash is injected at the CLI level (below) rather than via a corrupt frame, so we keep the
    # raw valid and drive a raising generator through the same streaming loop.
    _write_raw(root, "bitstamp", "live_orders_btcusd",
               [{"event": "order_created",
                 "data": {"id": i, "order_type": 0, "price": f"{27000 + i}.00",
                          "amount": "1.0"}} for i in range(n_good)])


def test_partition_write_is_atomic_on_crash(tmp_path, monkeypatch):
    """A crash mid-stream must NOT leave a glob-visible part.parquet (only a .tmp), and a prior
    complete part.parquet must survive a crashed re-run untouched."""
    out = tmp_path / "clean"
    pdir = (out / "books" / "venue=bitstamp" / "channel=live_orders_btcusd" / "date=2025-10-09")

    rows = _synth_book_rows(60, T0)

    def _raising_stream():
        # Emit enough to open the writer + flush at least one row group, then crash.
        for i, r in enumerate(rows):
            if i == 40:
                raise _Boom("simulated mid-stream failure")
            yield (pipeline.BOOK, r)

    # Drive the real CLI streaming loop with a tiny chunk so a row group is flushed before the crash.
    monkeypatch.setattr(cli, "CHUNK_ROWS", 5)
    monkeypatch.setattr(pipeline, "stream_crypto",
                        lambda *a, **k: _raising_stream())
    root = tmp_path / "raw"
    _crashing_crypto_raw(root, 3, T0)  # one real raw file so _process_feed reaches the stream loop

    import pytest
    with pytest.raises(_Boom):
        cli._process_feed(str(root), str(out), "bitstamp", "live_orders_btcusd", 10, None)

    # (a) No complete-looking part.parquet for the in-flight partition — the loader's glob sees
    # nothing. (_process_feed's finally runs abort(), which also removes the in-flight .tmp, so a
    # crashed run leaves zero cruft; a hard kill with no finally would leave only the .tmp, covered
    # by test_writer_hard_kill_leaves_only_tmp.)
    assert not (pdir / "part.parquet").exists()
    assert list(pdir.glob("*.parquet")) == []          # the loader's glob sees nothing
    assert not (pdir / "part.parquet.tmp").exists()     # graceful abort cleaned the tmp

    # (b) Now a clean run publishes a complete part.parquet (stale .tmp cleared on reopen).
    monkeypatch.setattr(pipeline, "stream_crypto",
                        lambda v, c, recs, rep, levels=10: iter((pipeline.BOOK, r) for r in rows))
    cli._process_feed(str(root), str(out), "bitstamp", "live_orders_btcusd", 10, None)
    good = pl.read_parquet(pdir / "part.parquet")
    assert good.height == len(rows)
    assert not (pdir / "part.parquet.tmp").exists()     # stale tmp cleaned up

    # (c) A crashed RE-RUN must leave the good part.parquet intact (not truncated).
    before = (pdir / "part.parquet").read_bytes()
    monkeypatch.setattr(pipeline, "stream_crypto", lambda *a, **k: _raising_stream())
    with pytest.raises(_Boom):
        cli._process_feed(str(root), str(out), "bitstamp", "live_orders_btcusd", 10, None)
    assert (pdir / "part.parquet").read_bytes() == before
    assert pl.read_parquet(pdir / "part.parquet").height == len(rows)


def test_writer_abort_leaves_no_glob_visible_file(tmp_path):
    """Direct-writer check: write_df then abort() publishes nothing the loader can see."""
    w = writer.PartitionedParquetWriter(tmp_path, schema.DS_BOOKS,
                                        venue="bitstamp", channel="live_orders_btcusd")
    w.write_df(schema.rows_to_df(_synth_book_rows(30, T0), schema.BOOK_SCHEMA))
    pdir = (tmp_path / "books" / "venue=bitstamp" / "channel=live_orders_btcusd"
            / "date=2025-10-09")
    assert (pdir / "part.parquet.tmp").exists()     # streamed to tmp
    assert not (pdir / "part.parquet").exists()      # not yet published
    w.abort()
    assert list(pdir.glob("*.parquet")) == []        # nothing glob-visible
    assert not (pdir / "part.parquet.tmp").exists()  # tmp cleaned up


def test_writer_hard_kill_leaves_only_tmp(tmp_path):
    """Simulate a hard kill (no close/abort ever runs): in-flight data remains only as a .tmp the
    loader's glob won't match, and the next (re)open clears that stale tmp before writing."""
    pdir = (tmp_path / "books" / "venue=bitstamp" / "channel=live_orders_btcusd"
            / "date=2025-10-09")
    w = writer.PartitionedParquetWriter(tmp_path, schema.DS_BOOKS,
                                        venue="bitstamp", channel="live_orders_btcusd")
    w.write_df(schema.rows_to_df(_synth_book_rows(30, T0), schema.BOOK_SCHEMA))
    del w  # process "killed" — neither close() nor abort() runs
    assert (pdir / "part.parquet.tmp").exists()
    assert list(pdir.glob("*.parquet")) == []        # never glob-visible

    # A fresh run reuses the partition: the stale .tmp is cleared on reopen, then a clean publish.
    w2 = writer.PartitionedParquetWriter(tmp_path, schema.DS_BOOKS,
                                         venue="bitstamp", channel="live_orders_btcusd")
    w2.write_df(schema.rows_to_df(_synth_book_rows(12, T0), schema.BOOK_SCHEMA))
    w2.close()
    assert (pdir / "part.parquet").exists()
    assert not (pdir / "part.parquet.tmp").exists()
    assert pl.read_parquet(pdir / "part.parquet").height == 12
