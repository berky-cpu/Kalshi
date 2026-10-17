"""BRTI replica parameters and constituent membership (ROADMAP 2.2).

Every constant here is transcribed from ``docs/brti-spec.md`` (CME CF Cryptocurrency Real
Time Indices Methodology Guide v17.0, 21 Sep 2026) with its spec reference. Each open-item
choice that the spec leaves ambiguous is exposed as a **parameter** (never hard-coded) so the
replica's reading can be swapped and closed empirically against published BRTI:

* ``cap_mean``        — O-4: which mean enters the order-size cap ``C_T = s̄ + 5σ`` (§3.7 Eq 5).
                        Literal/default = TRIMMED mean (Eq 4e, bare symbol). "winsorized" is
                        the documented alternative.
* ``cap_pre_uncross`` — F4: compute the cap from the raw merged book BEFORE cross-venue
                        uncrossing (§3.2-2a). Default True, matching the §3.8 pseudocode.
* ``kde_grid_points`` — O-3: resolution of the ``argmax_s f_KDE(s)`` search grid (§3.6). The
                        methodology gives no grid; pick a resolution immaterial to the output.
* ``window_endpoint`` — O-5: the KALSHI 60 s settlement window placement, ``[T-59,T]`` vs
                        ``[T-60,T-1]`` (§5). Not defined by the RTI methodology; resolve by
                        matching published KXBTC15M settlements (the tracking harness does both).

PRECISION POLICY (CLAUDE.md: never float at a price boundary).
Prices stay exact ``Decimal`` through every BOOK operation — consolidation, per-level capping,
uncrossing, and PV-curve marginal-price selection are all exact (comparisons / min / subtract).
Only quantities that are *inherently* transcendental are float64: the exponential depth weights
``λ·e^(−λv)`` (§3.5), the Gaussian KDE and Silverman bandwidth (§3.6), the order-size cap mean
and std (§3.7), and the BTC *volume* axis (``s``, ``v``, cumulative sizes — a BTC quantity, not a
money boundary). The final index is the depth-weighted mean of the exact Decimal mid prices with
float weights, rounded to the $0.01 dissemination precision (§2) — exactly where the methodology
rounds. No fee/Kalshi-cent money boundary is ever crossed in float.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

NS_PER_S = 1_000_000_000

# Dissemination precision: $0.01 (brti-spec §2, VERIFIED).
CENT = Decimal("0.01")

# ---------------------------------------------------------------------------------------
# Constituents (brti-spec §1, VERIFIED). itBit was suspended from BRTI on 2026-07-27, so the
# active set changed in July 2026 — the replica must honour the as-of date. We key constituents
# by OUR recorder venue tokens (what data/clean carries), not CF's display names.
# ---------------------------------------------------------------------------------------
ITBIT_SUSPENDED_NS = int(datetime(2026, 7, 27, tzinfo=UTC).timestamp()) * NS_PER_S

# BRTI constituents as of list v13.7 (post-itBit-suspension), by recorder venue token.
# "coinbase" needs the 1.1d adapter; gemini/lmax/bullish/cryptocom are NOT recorded (O-8) —
# listed so the replica *knows* what full membership is and can report the missing-venue gap.
BRTI_CONSTITUENTS = frozenset(
    {"bitstamp", "coinbase", "kraken", "gemini", "lmax", "bullish", "cryptocom"}
)
# itBit, by its recorder token, was a constituent only before the suspension date.
_ITBIT_TOKEN = "itbit"


def active_constituents(effective_time_ns: int) -> frozenset[str]:
    """The set of BRTI constituent venue tokens active at ``effective_time_ns`` (§1).

    itBit is included only for times strictly before the 2026-07-27 suspension; on/after it is
    excluded. (We do not record itBit, so in practice this only documents the membership change.)
    """
    if effective_time_ns < ITBIT_SUSPENDED_NS:
        return BRTI_CONSTITUENTS | {_ITBIT_TOKEN}
    return BRTI_CONSTITUENTS


@dataclass(frozen=True)
class BrtiParams:
    """All BRTI aggregation parameters. Defaults are the literal v17.0 / O-resolution readings."""

    # §6.2 / §6.1 BRTI constants (VERIFIED).
    deviation_from_mid: float = 0.005          # D = 0.5% — utilized-depth cutoff (§3.4)
    ped: float = 0.05                           # Potentially-Erroneous-Data param = 5% (§5.3)
    ped_reinstate: float = 0.025                # hysteresis: reinstate < 50% of PED = 2.5% (§5.3.4)
    target_levels: int = 100                    # τ for dynamic spacing (§3.6 Eq 6a)
    retrieval_lag_threshold_ns: int = 10 * NS_PER_S  # 10 s staleness cutoff (§5.1)
    lam_factor: float = 0.3                     # λ = 1/(lam_factor·v_T); 0.3 ⇒ λ=1/(0.3·v_T) (§6.1, O-2)
    cap_sigma_mult: float = 5.0                 # C_T = mean + 5σ (§3.7 Eq 5)
    cap_near_top_pct: float = 0.05              # ±5% "near-top" band for the cap pool (§3.7 Eq 4a/4b)
    cap_min_levels: int = 50                    # at least top-50 per side in the cap pool (§3.7)
    kde_silverman_c: float = 1.06               # Silverman bandwidth constant (§3.6 Eq 6f)
    kde_min_samples: int = 4                    # n_T ≥ 4 ⇒ KDE; else smallest sampled size (§3.6)

    # Ambiguity knobs (each an open item; defaults = the literal/documented reading). ALL of
    # these are "close-empirically-against-published-BRTI" levers (like O-4/O-5) and must be
    # TUNED on a train split and REPORTED only out-of-sample (tracking.evaluate_oos):
    #   * cap_mean     (O-4)  — which mean enters C_T.
    #   * median_base  (O-9)  — the §5.3 deviation-filter median base. The spec literally says
    #                           "median of ALL venues' mids", so the default is "all"; "active"
    #                           (non-dropped only) is the alternative. After any drop the two
    #                           differ and change which venues drop/reinstate → change the index.
    cap_mean: str = "trimmed"                   # O-4: "trimmed" (literal Eq 4e) | "winsorized"
    median_base: str = "all"                    # O-9: "all" (literal §5.3) | "active"
    cap_pre_uncross: bool = True                # F4: cap pool from the pre-uncross merged book
    kde_grid_points: int = 4096                 # O-3: argmax search-grid resolution
    stale_from_last_update: bool = True         # staleness proxy: lag since last snapshot (see note)

    # Safety bound on grid length (pathological tiny-s guard); never hit in the utilized region
    # because the curve walk stops at the first D-breach (§3.4). Not a methodology parameter.
    max_grid_points: int = 2_000_000

    def __post_init__(self) -> None:
        if self.cap_mean not in ("trimmed", "winsorized"):
            raise ValueError(f"cap_mean must be 'trimmed' or 'winsorized', got {self.cap_mean!r}")
        if self.median_base not in ("all", "active"):
            raise ValueError(f"median_base must be 'all' or 'active', got {self.median_base!r}")
        if self.lam_factor <= 0:
            raise ValueError("lam_factor must be > 0")
        if self.target_levels <= 0:
            raise ValueError("target_levels must be > 0")


# Settlement-window endpoint conventions (Kalshi construct, §5 / facts.md 0.1). Both are computed
# by the tracking harness; the better-matching one is reported. Offsets are in SECONDS relative to
# the reference second ``T`` (close_time or open_time snapped to the integer second), inclusive.
WINDOW_ENDPOINTS: dict[str, tuple[int, int]] = {
    "T-59..T": (-59, 0),     # includes the reference-second print
    "T-60..T-1": (-60, -1),  # the 60 s strictly before the reference second
}
