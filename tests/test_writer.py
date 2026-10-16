from kbtc.recorders.writer import RotatingZstdWriter, read_records

HOUR_NS = 3600 * 10**9
T0 = 1_760_000_000 * 10**9  # fixed timestamp


def test_roundtrip_and_hourly_rotation(tmp_path):
    with RotatingZstdWriter(tmp_path, "coinbase", "level3") as w:
        w.write('{"type":"open","seq":1}', recv_ts_ns=T0)
        w.write(b'{"type":"done","seq":2}', recv_ts_ns=T0 + 1)
        w.write('{"type":"match","seq":3}', recv_ts_ns=T0 + HOUR_NS)

    files = sorted((tmp_path / "coinbase" / "level3").rglob("*.jsonl.zst"))
    assert len(files) == 2
    recs = [r for f in files for r in read_records(f)]
    assert [r["recv_ts_ns"] for r in recs] == [T0, T0 + 1, T0 + HOUR_NS]
    assert recs[1]["raw"] == '{"type":"done","seq":2}'


def test_truncated_tail_is_tolerated(tmp_path):
    w = RotatingZstdWriter(tmp_path, "kalshi", "trade", flush_interval_s=0)
    for i in range(50):
        w.write(f'{{"i":{i}}}', recv_ts_ns=T0 + i)
    path = w.path
    w._fh.flush()  # simulate crash: block-flushed but no frame end
    data = path.read_bytes()
    recs = list(read_records(path))
    assert len(recs) == 50 and len(data) > 0
    w.close()
