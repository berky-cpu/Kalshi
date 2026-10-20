"""Tests for the feed-liveness backstop (alerting #3b) and the alert.sh notifier no-op path.

The liveness check keys off the age of the newest DATA frame (classified like health), NOT file
mtime — because heartbeats are written to disk too. Tests build real recorded files with
``RotatingZstdWriter`` and assert a heartbeat-only tail is still STALE, a fresh feed is not, and the
settlement batch dataset is excluded. ``alert.sh`` is tested only on its no-op (unset) path (no
network).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from kbtc.recorders import liveness
from kbtc.recorders.writer import RotatingZstdWriter

S = 1_000_000_000  # ns per second
NOW = 1_760_000_000 * S + 10_000 * S  # a fixed "now" comfortably inside an hour
REPO = Path(__file__).resolve().parent.parent
ALERT_SH = REPO / "deploy" / "alert.sh"

# Minimal raw frames per venue: a DATA frame and a control/heartbeat frame (health classifier).
CB_DATA = '{"type":"open","sequence":1,"order_id":"x"}'
CB_HEARTBEAT = '{"type":"heartbeat","sequence":2}'
KR_DATA = '{"channel":"level3","type":"update","data":[{"bids":[]}]}'
KALSHI_DATA = '{"type":"orderbook_delta","sid":1,"seq":2,"market_ticker":"K","price_dollars":"0.4","delta_fp":"1.0","side":"yes"}'


def _write(root, venue, channel, frames):
    """frames: list of (raw, recv_ts_ns). Writes them, then closes (flushes the zstd frame)."""
    w = RotatingZstdWriter(root, venue, channel)
    for raw, ts in frames:
        w.write(raw, recv_ts_ns=ts)
    w.close()


# --------------------------------------------------------------------------------------
# Pure threshold / classify logic.
# --------------------------------------------------------------------------------------
def test_threshold_for_per_venue_and_exclusion():
    assert liveness.threshold_for("coinbase", "full") == liveness.CRYPTO_STALE_S == 180.0
    assert liveness.threshold_for("binance", "perp") == 180.0
    assert liveness.threshold_for("kalshi", "market") == liveness.KALSHI_STALE_S == 420.0
    # Settlement is a batch timer, not a stream → excluded.
    assert liveness.threshold_for("kalshi", "settlement") is None


def test_classify_fresh_not_stale():
    r = liveness.classify("coinbase", "full", NOW - 10 * S, NOW)
    assert r is not None and r.stale is False and r.age_s < 180


def test_classify_old_data_is_stale():
    r = liveness.classify("coinbase", "full", NOW - 300 * S, NOW)
    assert r.stale is True and r.threshold_s == 180.0


def test_classify_kalshi_uses_higher_threshold():
    # 300 s old: stale for a crypto feed, but NOT for kalshi (420 s).
    assert liveness.classify("coinbase", "full", NOW - 300 * S, NOW).stale is True
    assert liveness.classify("kalshi", "market", NOW - 300 * S, NOW).stale is False
    assert liveness.classify("kalshi", "market", NOW - 500 * S, NOW).stale is True


def test_classify_excluded_returns_none():
    assert liveness.classify("kalshi", "settlement", NOW - 10_000 * S, NOW) is None


def test_classify_no_data_uses_silence_baseline():
    # No data frame: a feed silent only 30 s (just started) is NOT stale; one silent 1000 s is.
    assert liveness.classify("coinbase", "full", None, NOW,
                             silence_since_ns=NOW - 30 * S).stale is False
    assert liveness.classify("coinbase", "full", None, NOW,
                             silence_since_ns=NOW - 1000 * S).stale is True


# --------------------------------------------------------------------------------------
# Filesystem scan — heartbeats must NOT count as data.
# --------------------------------------------------------------------------------------
def test_scan_heartbeat_only_tail_is_stale(tmp_path):
    root = tmp_path / "raw"
    # A data frame 300 s ago, then only heartbeats since (newest frame is a heartbeat 1 s ago).
    _write(root, "coinbase", "full",
           [(CB_DATA, NOW - 300 * S), (CB_HEARTBEAT, NOW - 60 * S), (CB_HEARTBEAT, NOW - 1 * S)])
    r = liveness.scan_feed("coinbase", "full", root / "coinbase" / "full", NOW)
    assert r is not None
    # Newest DATA frame is 300 s old (heartbeats ignored) → stale, even though the file is fresh.
    assert r.last_data_ns == NOW - 300 * S
    assert r.stale is True


def test_scan_fresh_feed_not_stale(tmp_path):
    root = tmp_path / "raw"
    _write(root, "kraken", "level3", [(KR_DATA, NOW - 5 * S)])
    r = liveness.scan_feed("kraken", "level3", root / "kraken" / "level3", NOW)
    assert r is not None and r.stale is False


def test_scan_root_excludes_settlement_and_reports_stale(tmp_path):
    root = tmp_path / "raw"
    _write(root, "coinbase", "full", [(CB_DATA, NOW - 300 * S)])      # stale (300 > 180)
    _write(root, "kraken", "level3", [(KR_DATA, NOW - 5 * S)])        # fresh
    _write(root, "kalshi", "market", [(KALSHI_DATA, NOW - 100 * S)])  # fresh (100 < 420)
    _write(root, "kalshi", "settlement", [(CB_DATA, NOW - 99_999 * S)])  # excluded

    results = {r.feed: r for r in liveness.scan_root(root, NOW)}
    assert "kalshi/settlement" not in results          # excluded dataset
    assert results["coinbase/full"].stale is True
    assert results["kraken/level3"].stale is False
    assert results["kalshi/market"].stale is False


def test_scan_feed_no_files_returns_none(tmp_path):
    # A channel dir that exists but has no recorded files → not alarmed (feed not running here).
    d = tmp_path / "raw" / "coinbase" / "full"
    d.mkdir(parents=True)
    assert liveness.scan_feed("coinbase", "full", d, NOW) is None


# --------------------------------------------------------------------------------------
# CLI main(): alerts each stale feed via the injected notifier (no subprocess, no network).
# --------------------------------------------------------------------------------------
def test_main_alerts_only_stale_feeds(tmp_path):
    root = tmp_path / "raw"
    _write(root, "coinbase", "full", [(CB_DATA, NOW - 300 * S)])      # stale
    _write(root, "kraken", "level3", [(KR_DATA, NOW - 5 * S)])        # fresh
    _write(root, "kalshi", "settlement", [(CB_DATA, NOW - 99_999 * S)])  # excluded

    calls: list[tuple[str, str]] = []
    rc = liveness.main(["--root", str(root)], clock_ns=lambda: NOW,
                       notifier=lambda subj, body: calls.append((subj, body)))
    assert rc == 0
    subjects = " ".join(s for s, _ in calls)
    assert "coinbase/full" in subjects        # the stale feed paged
    assert "kraken" not in subjects           # fresh feed did not
    assert "settlement" not in subjects       # excluded dataset did not


# --------------------------------------------------------------------------------------
# FIX B — fast dashboard status (data-age + recent msgs/s), no alerts.
# --------------------------------------------------------------------------------------
def test_feed_status_data_age_and_rate(tmp_path):
    root = tmp_path / "raw"
    # 10 data frames at 1/s ending 3 s ago, then a heartbeat 1 s ago (must not reset data-age).
    frames = [(CB_DATA, NOW - (12 - k) * S) for k in range(10)]  # NOW-12s .. NOW-3s
    frames.append((CB_HEARTBEAT, NOW - 1 * S))
    _write(root, "coinbase", "full", frames)
    st = liveness.feed_status("coinbase", "full", root / "coinbase" / "full", NOW)
    assert st is not None
    assert st.last_data_ns == NOW - 3 * S          # newest DATA frame (heartbeat ignored)
    assert abs(st.age_s - 3.0) < 0.01
    assert st.data_per_s > 0.5                      # ~1/s over the trailing window
    assert st.stale is False


def test_scan_status_marks_stale_and_batch(tmp_path):
    root = tmp_path / "raw"
    _write(root, "coinbase", "full", [(CB_DATA, NOW - 300 * S)])       # stale (>180)
    _write(root, "kraken", "level3", [(KR_DATA, NOW - 5 * S)])         # live
    _write(root, "kalshi", "settlement", [(CB_DATA, NOW - 99_999 * S)])  # batch (no verdict)
    by = {s.feed: s for s in liveness.scan_status(root, NOW)}
    assert by["coinbase/full"].stale is True
    assert by["kraken/level3"].stale is False
    assert by["kalshi/settlement"].threshold_s is None and by["kalshi/settlement"].stale is False
    table = liveness.render_status(list(by.values()), NOW)
    assert "STALE" in table and "coinbase/full" in table and "batch" in table


def test_print_status_mode_never_alerts(tmp_path, capsys):
    root = tmp_path / "raw"
    _write(root, "coinbase", "full", [(CB_DATA, NOW - 300 * S)])  # stale — but dashboard must NOT page
    calls: list[tuple[str, str]] = []
    rc = liveness.main(["--root", str(root), "--print-status"], clock_ns=lambda: NOW,
                       notifier=lambda s, b: calls.append((s, b)))
    assert rc == 0
    assert calls == []                      # dashboard mode never notifies
    out = capsys.readouterr().out
    assert "coinbase/full" in out and "STALE" in out


# --------------------------------------------------------------------------------------
# alert.sh: no-op when KBTC_ALERT_URL is unset; never echoes the URL.
# --------------------------------------------------------------------------------------
def test_alert_sh_noops_when_url_unset():
    env = {k: v for k, v in os.environ.items() if k != "KBTC_ALERT_URL"}
    r = subprocess.run(["bash", str(ALERT_SH), "subj", "body"],
                       capture_output=True, text=True, env=env, timeout=15)
    assert r.returncode == 0
    assert "skipping" in r.stderr.lower()
    assert r.stdout.strip() == ""  # nothing posted


def test_alert_sh_never_echoes_the_url():
    text = ALERT_SH.read_text()
    # The URL is only ever used as a curl argument, never printed. (Mentioning the variable NAME in
    # a log line is fine; echoing its VALUE — $KBTC_ALERT_URL / ${KBTC_ALERT_URL} — is not.)
    assert "curl" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo"):
            assert "$KBTC_ALERT_URL" not in stripped, line
            assert "${KBTC_ALERT_URL" not in stripped, line
