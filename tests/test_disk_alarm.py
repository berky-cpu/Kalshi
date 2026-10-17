"""Unit tests for the deploy disk-usage alarm (ROADMAP 1.3 Part B)."""
from __future__ import annotations

from kbtc.recorders import disk_alarm
from kbtc.recorders.disk_alarm import GiB, evaluate


def test_ok_below_warn():
    s = evaluate(total_bytes=100 * GiB, used_bytes=50 * GiB, free_bytes=50 * GiB)
    assert s.level == "ok" and s.exit_code == 0
    assert abs(s.pct_used - 0.5) < 1e-9


def test_warn_band():
    s = evaluate(total_bytes=100 * GiB, used_bytes=85 * GiB, free_bytes=15 * GiB)
    assert s.level == "warn" and s.exit_code == 1


def test_critical_on_pct():
    s = evaluate(total_bytes=100 * GiB, used_bytes=92 * GiB, free_bytes=8 * GiB)
    assert s.level == "critical" and s.exit_code == 2


def test_critical_on_min_free_even_when_pct_ok():
    # Big disk, low percentage, but dangerously small absolute free space -> critical.
    s = evaluate(total_bytes=1000 * GiB, used_bytes=996 * GiB, free_bytes=4 * GiB,
                 warn_pct=0.80, crit_pct=0.999, min_free_gib=5.0)
    assert s.level == "critical"


def test_boundaries_inclusive():
    # pct exactly at warn -> warn; exactly at crit -> critical.
    assert evaluate(100, 80, 20, warn_pct=0.80, crit_pct=0.90, min_free_gib=0).level == "warn"
    assert evaluate(100, 90, 10, warn_pct=0.80, crit_pct=0.90, min_free_gib=0).level == "critical"


def test_zero_total_is_critical():
    s = evaluate(total_bytes=0, used_bytes=0, free_bytes=0, min_free_gib=0)
    assert s.level == "critical"


def test_main_exit_code_matches_status(monkeypatch, capsys):
    monkeypatch.setattr(
        disk_alarm, "check_path",
        lambda path, **kw: disk_alarm.DiskStatus("warn", 0.85, 15 * GiB, 100 * GiB, "WARN: test"),
    )
    rc = disk_alarm.main(["--path", "/whatever"])
    assert rc == 1
    assert "WARN" in capsys.readouterr().err


def test_main_handles_missing_path():
    rc = disk_alarm.main(["--path", "/no/such/path/kbtc-xyz"])
    assert rc == 2
