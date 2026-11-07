"""Candidate-2 downloader: pull settled single-name/mention markets + full trade tapes
from the PUBLIC Kalshi REST API (read-only; no auth; no order path).

Usage:
    uv run python notes/_candidate2_scripts/download.py KXTRUMPMENTION [KXELONMENTION ...]

Saves (gitignored) under notes/_candidate2/:
    markets/<SERIES>.jsonl   one market dict per line (ALL statuses; filter later)
    trades/<SERIES>.jsonl    one trade dict per line, newest-first per market, with `ticker`
    done/<SERIES>.trades_done one ticker per line = markets whose trades are fully pulled (resume)

Politeness: small sleep between calls; exponential backoff on HTTP 429/5xx.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
ROOT = os.path.join(os.path.dirname(__file__), "..", "_candidate2")
ROOT = os.path.abspath(ROOT)
SLEEP = 0.06  # polite gap between calls


def _get(path: str, tries: int = 6) -> dict:
    url = BASE + path
    delay = 0.5
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "kbtc-research/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 16)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:  # transient network
            if attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 16)
                continue
            raise
    raise RuntimeError("unreachable")


def _paged(path_base: str, key: str):
    cursor = None
    while True:
        sep = "&" if "?" in path_base else "?"
        path = path_base + (f"{sep}cursor={cursor}" if cursor else "")
        d = _get(path)
        items = d.get(key, []) or []
        for it in items:
            yield it
        cursor = d.get("cursor")
        time.sleep(SLEEP)
        if not cursor or not items:
            break


def download_series(series: str) -> None:
    os.makedirs(os.path.join(ROOT, "markets"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "trades"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "done"), exist_ok=True)
    mpath = os.path.join(ROOT, "markets", f"{series}.jsonl")
    tpath = os.path.join(ROOT, "trades", f"{series}.jsonl")
    dpath = os.path.join(ROOT, "done", f"{series}.trades_done")

    # 1) markets (all statuses) -> file (overwrite; cheap).
    markets = list(_paged(f"/markets?series_ticker={series}&limit=200", "markets"))
    with open(mpath, "w") as f:
        for m in markets:
            f.write(json.dumps(m) + "\n")
    print(f"[{series}] markets={len(markets)}", flush=True)

    # 2) resume: which tickers already have trades pulled.
    done: set[str] = set()
    if os.path.exists(dpath):
        with open(dpath) as f:
            done = {ln.strip() for ln in f if ln.strip()}

    # Only settled/finalized/closed markets have a terminal result worth simulating; but pull trades
    # for any market that has traded. We simulate settled ones later; pull trades for all non-active
    # terminal markets to keep the tape complete.
    targets = [m for m in markets if m.get("status") in ("finalized", "settled", "closed")
               and m["ticker"] not in done]
    print(f"[{series}] trade-targets remaining={len(targets)} (already done={len(done)})", flush=True)

    tf = open(tpath, "a")
    df = open(dpath, "a")
    try:
        for i, m in enumerate(targets):
            tk = m["ticker"]
            n = 0
            for tr in _paged(f"/markets/trades?ticker={tk}&limit=1000", "trades"):
                tr["ticker"] = tk
                tf.write(json.dumps(tr) + "\n")
                n += 1
            df.write(tk + "\n")
            df.flush()
            if (i + 1) % 50 == 0:
                tf.flush()
                print(f"[{series}] {i+1}/{len(targets)} markets, last {tk} trades={n}", flush=True)
    finally:
        tf.close()
        df.close()
    print(f"[{series}] DONE", flush=True)


if __name__ == "__main__":
    serieslist = sys.argv[1:] or ["KXTRUMPMENTION"]
    for s in serieslist:
        download_series(s)
