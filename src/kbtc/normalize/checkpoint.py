"""Checkpoint validation — the ROADMAP 2.1 acceptance criterion ("reconstructed L2 matches
venue snapshots at checkpoints"). Deviations are REPORTED, never silent.

Per-venue checkpoint oracles, strongest first:

* **Kraken** — the per-update **CRC32 ``checksum``** is a continuous checkpoint. After the book
  processor applies each ``level3`` snapshot/update frame we INDEPENDENTLY recompute the CRC32 over
  the reconstructed top-10/side (``base.kraken_crc32``) and compare to the frame's ``checksum``;
  exact match is required, a mismatch is a deviation.
  Two levels of validation (red-team #1/#3):
    - The fixture ``level3.jsonl`` checksums are SELF-GENERATED (produced by ``kraken_crc32`` when
      the fixture was authored), so the fixture checkpoint is a REGRESSION / self-consistency check
      — it guards against reconstruction drift, it is NOT an external oracle.
    - The CRC32 algorithm itself — hence everything the normalizer checksums — is EXTERNALLY
      validated against Kraken's published worked example (checksum ``1063832831`` from the real
      21-bid/14-ask input in Kraken's L3 guide): ``tests/test_recorders_kraken.py`` asserts the
      algorithm, and the normalizer's own emit path is checked end-to-end against that same real
      input in ``tests/test_normalize_books.py`` (the genuine acceptance demonstration).
* **Bybit** — every mid-stream ``type:"snapshot"`` frame is a checkpoint. We compare the
  delta-reconstructed top-N book *just before* the snapshot to the snapshot's own levels and
  report the top-of-book price deviation (dollars). NOTE: a Bybit snapshot is a *reset* that
  carries the latest data, so it generally reflects newer market state than the reconstructed
  book — the deviation is INFORMATIONAL (reported, not an error). The first snapshot is the
  initial book build, not a checkpoint.
* **Coinbase / Bitstamp / Binance** — checkpointed against in-stream REST reseed snapshots
  (Coinbase/Bitstamp ``book?level=...``; Binance ``lastUpdateId``). None are present in the
  offline fixtures, so this is DEFERRED to the live VPS run (REST snapshot vs. reconstruction);
  the report states so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from kbtc.recorders.base import ASK, BID, PriceLevelBook, as_dict, kraken_crc32

METHOD_KRAKEN_CRC = "kraken_crc32"
METHOD_BYBIT_SNAPSHOT = "bybit_snapshot"
METHOD_DEFERRED = "deferred_rest_snapshot"


@dataclass
class CheckpointReport:
    """Per-feed checkpoint-validation result.

    ``deviations`` holds a per-checkpoint magnitude in the venue's price units (0.0 for an exact
    match). For Kraken CRC the magnitude has no meaning, so a match contributes 0.0 and a mismatch
    is counted in ``n_mismatches`` only. ``n_matches + n_mismatches == n_checkpoints``.
    """

    venue: str
    channel: str
    method: str
    n_checkpoints: int = 0
    n_matches: int = 0
    n_mismatches: int = 0
    deviations: list[float] = field(default_factory=list)
    notes: str = ""

    def record(self, matched: bool, deviation: float = 0.0) -> None:
        self.n_checkpoints += 1
        if matched:
            self.n_matches += 1
        else:
            self.n_mismatches += 1
        self.deviations.append(deviation)

    @staticmethod
    def _pct(xs: list[float], q: float) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        return s[idx]

    @property
    def median_deviation(self) -> float:
        return self._pct(self.deviations, 0.5)

    @property
    def p95_deviation(self) -> float:
        return self._pct(self.deviations, 0.95)

    @property
    def max_deviation(self) -> float:
        return max(self.deviations) if self.deviations else 0.0

    def to_dict(self) -> dict:
        return {
            "venue": self.venue, "channel": self.channel, "method": self.method,
            "n_checkpoints": self.n_checkpoints, "n_matches": self.n_matches,
            "n_mismatches": self.n_mismatches,
            "median_deviation": self.median_deviation,
            "p95_deviation": self.p95_deviation, "max_deviation": self.max_deviation,
            "notes": self.notes,
        }


def new_report(venue: str, channel: str) -> CheckpointReport:
    v = venue.lower()
    if v == "kraken":
        return CheckpointReport(venue, channel, METHOD_KRAKEN_CRC)
    if v == "bybit":
        return CheckpointReport(venue, channel, METHOD_BYBIT_SNAPSHOT)
    return CheckpointReport(
        venue, channel, METHOD_DEFERRED,
        notes="no in-stream REST snapshot; REST-vs-reconstruction checkpointing deferred to the "
              "live VPS run",
    )


# ---------------------------------------------------------------------------------------
# Kraken: independent CRC32 recompute vs the frame checksum, after the processor applies.
# ---------------------------------------------------------------------------------------
def kraken_checkpoint(report: CheckpointReport, proc, raw) -> None:
    """Call AFTER ``proc.process(raw, ...)`` for a Kraken level3 frame. Independently recompute
    the top-10 CRC32 of the reconstructed book and compare to the frame's ``checksum``."""
    msg = as_dict(raw)
    if not isinstance(msg, dict) or msg.get("channel") != "level3":
        return
    if msg.get("type") not in {"snapshot", "update"}:
        return
    data = msg.get("data") or []
    if not data or not isinstance(data[-1], dict):
        return
    want = data[-1].get("checksum")
    if want is None:
        report.record(False, 0.0)
        return
    got = kraken_crc32(proc.book)
    report.record(int(want) == got, 0.0)


# ---------------------------------------------------------------------------------------
# Bybit: compare pre-snapshot reconstruction to each mid-stream snapshot.
# ---------------------------------------------------------------------------------------
def _snapshot_book(data: dict) -> PriceLevelBook:
    b = PriceLevelBook()
    for p, q in data.get("b", []):
        b.set(BID, p, q)
    for p, q in data.get("a", []):
        b.set(ASK, p, q)
    return b


def _best_dev(recon: PriceLevelBook, snap: PriceLevelBook) -> tuple[bool, float]:
    """Top-of-book deviation (dollars) between two books; (exact_top_and_levels, magnitude)."""
    def side_levels(bk, side):
        return [(str(p), str(q)) for p, q in bk.levels(side)]
    levels_equal = (side_levels(recon, BID) == side_levels(snap, BID)
                    and side_levels(recon, ASK) == side_levels(snap, ASK))
    devs = []
    for getter in (lambda bk: bk.best_bid(), lambda bk: bk.best_ask()):
        rv, sv = getter(recon), getter(snap)
        if rv is not None and sv is not None:
            devs.append(abs(float(rv - sv)))
        elif rv is None and sv is None:
            devs.append(0.0)
        else:
            devs.append(float("inf"))
    dev = max(devs) if devs else 0.0
    return levels_equal and dev == 0.0, dev


def bybit_checkpoint(report: CheckpointReport, proc, raw) -> bool:
    """Call BEFORE feeding a Bybit orderbook frame to the processor. If the frame is a mid-stream
    snapshot (processor already synced), compare the current reconstruction to it and record a
    checkpoint. Returns True if the frame WAS a mid-stream snapshot checkpoint (for logging)."""
    msg = as_dict(raw)
    if not isinstance(msg, dict) or "data" not in msg:
        return False
    if not str(msg.get("topic", "")).startswith("orderbook."):
        return False
    if msg.get("type") != "snapshot":
        return False
    if not proc.synced:
        return False  # first snapshot = initial build, not a checkpoint
    snap = _snapshot_book(msg["data"])
    matched, dev = _best_dev(proc.book, snap)
    report.record(matched, dev)
    return True
