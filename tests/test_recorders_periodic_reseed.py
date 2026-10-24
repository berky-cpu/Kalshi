"""Unit tests for the periodic forced-reseed decision logic (base.PeriodicReseedTimer) and its
Bitstamp-only gating.

The self-heal for the 2026-10-23 Bitstamp lost-delete defect: ``live_orders`` has no in-band
snapshot, so the book self-heals ONLY via a REST reseed; a lost ``order_deleted`` (a wire loss, no
chain break) is invisible to gap detection and poisoned the book for 2+ days because no reseed
fired. A timer-based forced reseed bounds that exposure. All pure / socket-free; the ``run_recorder``
wiring that flips ``needs_resnapshot`` and runs the inline fetch is the live loop (pragma-no-cover),
exactly like the DataStalenessMonitor split.
"""
from __future__ import annotations

from kbtc.recorders import run
from kbtc.recorders.base import PeriodicReseedTimer

S = 1_000_000_000  # ns per second


def test_not_due_before_baseline():
    """Before the first on_reseed (which the connect path calls after the initial seed), the timer
    has no baseline and must never be due — it can't fire before the book is even seeded."""
    t = PeriodicReseedTimer(interval_s=1200.0)
    assert t.last_reseed_ns == 0
    assert t.due(10_000 * S) is False


def test_due_after_interval_elapses():
    t = PeriodicReseedTimer(interval_s=1200.0)  # 20 min
    t0 = 1000 * S
    t.on_reseed(t0)  # baseline at connect/initial seed
    assert t.due(t0 + 1199 * S) is False      # just under the interval
    assert t.due(t0 + 1200 * S) is True        # exactly at the interval → due
    assert t.due(t0 + 5000 * S) is True        # and remains due until a reseed resets it


def test_reseed_resets_the_clock_defers_next():
    """A reseed (gap OR periodic) resets the clock, so the next forced reseed is a full interval
    out — this is what prevents a periodic reseed piling on right after a gap-triggered one."""
    t = PeriodicReseedTimer(interval_s=1200.0)
    t0 = 1000 * S
    t.on_reseed(t0)
    assert t.due(t0 + 1200 * S) is True
    # A reseed happens at +1200s (the read loop calls on_reseed on every reseed).
    t.on_reseed(t0 + 1200 * S)
    assert t.due(t0 + 1200 * S + 1199 * S) is False  # clock restarted → not due yet
    assert t.due(t0 + 1200 * S + 1200 * S) is True    # a full interval after the last reseed


def test_gap_reseed_defers_periodic_no_double_reseed():
    """Scenario: a gap reseed lands shortly before the periodic interval would elapse. Because the
    read loop resets the clock on the gap reseed, no periodic reseed fires back-to-back."""
    t = PeriodicReseedTimer(interval_s=1200.0)
    t0 = 1000 * S
    t.on_reseed(t0)
    # Gap reseed at +1100s (100s before the periodic would be due).
    t.on_reseed(t0 + 1100 * S)
    # At +1200s the OLD baseline would have been due, but the gap reset defers it.
    assert t.due(t0 + 1200 * S) is False
    assert t.due(t0 + 1100 * S + 1200 * S) is True  # a full interval past the gap reseed


def test_interval_none_disables():
    t = PeriodicReseedTimer(interval_s=None)
    t.on_reseed(1000 * S)
    assert t.due(1000 * S + 10_000 * S) is False  # never due when disabled


# ---------------------------------------------------------------------------------------
# Gating: ONLY Bitstamp opts into periodic reseeds. Every other feed leaves it disabled so its
# in-band / periodic-snapshot self-heal is untouched (no extra REST load, no resubscribe storms).
# ---------------------------------------------------------------------------------------
def _env_for_build(monkeypatch):
    for k, v in {
        "COINBASE_API_KEY": "cb-key",
        "COINBASE_API_SECRET": "Y2Itc2VjcmV0",
        "COINBASE_API_PASSPHRASE": "cb-pass",
        "KRAKEN_API_KEY": "kr-key",
        "KRAKEN_API_SECRET": "a3Itc2VjcmV0",
        "KALSHI_API_KEY_ID": "kalshi-key-id",
        "KALSHI_PRIVATE_KEY_PATH": "/etc/kbtc/kalshi.pem",
    }.items():
        monkeypatch.setenv(k, v)


def test_only_bitstamp_enables_periodic_reseed(monkeypatch):
    _env_for_build(monkeypatch)
    for name in sorted(run.FEEDS):
        feed = run.build_feed(
            name, root="/tmp/kbtc-data",
            token_fetch=lambda _e: "stub-token",
            signer_factory=lambda _e: (lambda m: b"sig"),
        )
        got = feed.io_config.periodic_reseed_s
        if name == "bitstamp-live_orders":
            assert got == 20 * 60, f"{name} should force a periodic reseed"
        else:
            assert got is None, f"{name} must NOT get periodic reseeds (got {got!r})"
