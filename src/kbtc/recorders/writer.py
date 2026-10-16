"""Raw message storage: hourly-rotating, zstd-compressed JSON Lines.

Layout: {root}/{venue}/{channel}/date=YYYY-MM-DD/HH_{open_ts_ns}.jsonl.zst
Each line: {"recv_ts_ns": int, "raw": str}  — the payload exactly as received.

A new file is opened per (hour, process start), so restarts never append to a
half-written zstd frame. Blocks are flushed every `flush_interval_s` so a crash
loses at most that much data; `read_records` tolerates a truncated tail.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import zstandard as zstd


class RotatingZstdWriter:
    def __init__(
        self,
        root: str | Path,
        venue: str,
        channel: str,
        level: int = 3,
        flush_interval_s: float = 5.0,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.base = Path(root) / venue / channel
        self.level = level
        self.flush_interval_ns = int(flush_interval_s * 1e9)
        self.clock_ns = clock_ns
        self._hour_key: str | None = None
        self._fh = None
        self._zw = None
        self._last_flush_ns = 0
        self.path: Path | None = None

    def _open(self, ts_ns: int) -> None:
        self.close()
        dt = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC)
        d = self.base / f"date={dt:%Y-%m-%d}"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{dt:%H}_{ts_ns}.jsonl.zst"
        self._fh = open(self.path, "wb")
        self._zw = zstd.ZstdCompressor(level=self.level).stream_writer(self._fh)
        self._hour_key = f"{dt:%Y-%m-%d-%H}"
        self._last_flush_ns = ts_ns

    def write(self, raw: str | bytes, recv_ts_ns: int | None = None) -> None:
        ts = recv_ts_ns if recv_ts_ns is not None else self.clock_ns()
        key = datetime.fromtimestamp(ts / 1e9, tz=UTC).strftime("%Y-%m-%d-%H")
        if key != self._hour_key:
            self._open(ts)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        line = json.dumps({"recv_ts_ns": ts, "raw": raw}, separators=(",", ":")) + "\n"
        self._zw.write(line.encode("utf-8"))
        if ts - self._last_flush_ns >= self.flush_interval_ns:
            self._zw.flush(zstd.FLUSH_BLOCK)
            self._fh.flush()
            self._last_flush_ns = ts

    def close(self) -> None:
        if self._zw is not None:
            self._zw.flush(zstd.FLUSH_FRAME)
            self._zw.close()  # also closes the underlying file
        self._zw = None
        self._fh = None
        self._hour_key = None

    def __enter__(self) -> RotatingZstdWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_records(path: str | Path) -> Iterator[dict]:
    """Yield records from a file; stops cleanly at a truncated tail."""
    with open(path, "rb") as fh:
        reader = zstd.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        buf = b""
        while True:
            try:
                chunk = reader.read(1 << 16)
            except zstd.ZstdError:
                break
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                if ln:
                    yield json.loads(ln)
