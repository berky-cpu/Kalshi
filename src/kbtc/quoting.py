"""Baseline Avellaneda–Stoikov quoting adapted to a binary contract.

Vanilla A–S uses the price variance sigma^2 * h of the asset itself. For a binary, the
value variance over a short horizon h is approximately (dV/dX)^2 * sigma^2 * h, where
dV/dX = phi(z) * (m/n) / sd (see pricing.digital_up). That term peaks at the money, so
inventory risk and the risk component of the spread are largest exactly where the
contract trades near 50c.

    reservation = fair - q * gamma * var_h
    half_spread = 0.5 * gamma * var_h + (1/gamma) * ln(1 + gamma / k)
    var_h       = delta_x^2 * sigma^2 * h

Units: prices in dollars, q in contracts (+ = long YES), gamma in 1/(dollar^2 * contract)
scaled to taste, k = fill-intensity decay per dollar of distance from fair.
All parameters are placeholders to be calibrated in Phase 4.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class QuoteParams:
    gamma: float = 0.1
    k: float = 150.0
    horizon_s: float = 30.0
    tick: float = 0.01
    min_price: float = 0.01
    max_price: float = 0.99


@dataclass(frozen=True)
class Quote:
    bid_cents: int | None
    ask_cents: int | None
    reservation: float
    half_spread: float


def value_variance(delta_x: float, sigma: float, horizon_s: float) -> float:
    return delta_x * delta_x * sigma * sigma * max(horizon_s, 0.0)


def quote(fair: float, delta_x: float, sigma: float, inventory: int, p: QuoteParams = QuoteParams()) -> Quote:
    if p.gamma <= 0 or p.k <= 0:
        raise ValueError("gamma and k must be positive")
    var_h = value_variance(delta_x, sigma, p.horizon_s)
    reservation = fair - inventory * p.gamma * var_h
    half = 0.5 * p.gamma * var_h + math.log(1.0 + p.gamma / p.k) / p.gamma

    ticks = round(1 / p.tick)
    bid = math.floor((reservation - half) * ticks + 1e-9) / ticks
    ask = math.ceil((reservation + half) * ticks - 1e-9) / ticks
    if ask - bid < p.tick - 1e-12:
        ask = bid + p.tick

    bid_c = round(bid * 100)
    ask_c = round(ask * 100)
    lo, hi = round(p.min_price * 100), round(p.max_price * 100)
    bid_out = bid_c if lo <= bid_c <= hi else None
    ask_out = ask_c if lo <= ask_c <= hi else None
    return Quote(bid_cents=bid_out, ask_cents=ask_out, reservation=reservation, half_spread=half)


def near_strike_endgame(tau_s: float, z: float, tau_cut_s: float = 90.0, z_cut: float = 1.0) -> bool:
    """True when quotes should be pulled or shrunk: little time left and price near the strike.

    Motivated by binary delta blow-up near expiry and settlement-window manipulation
    evidence (Dai, Jia & Yu 2026). Thresholds are placeholders for Phase 4.
    """
    return tau_s <= tau_cut_s and abs(z) <= z_cut
