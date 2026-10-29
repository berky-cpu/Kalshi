"""Policy interface (engine is policy-agnostic) + a thin adapter over ``kbtc.quoting``.

The engine hands the policy an immutable :class:`MarketState` built ONLY from events with
``recv_ts_ns <= t`` (no look-ahead) and the policy returns desired resting orders as
:class:`QuoteIntent`s. The engine diffs intents against live orders to decide submits/cancels, so
a policy never talks to any order path — it is a pure ``state -> intents`` function.

Prices in intents are exact ``Decimal`` dollars on a Kalshi book side (``yes``/``no``), i.e. the
resting-bid representation the queue model uses: buy YES at ``price`` -> ``side="yes"``; sell YES
(= buy NO) at ``1 - price`` -> ``side="no"``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

CENT = Decimal("0.01")


@dataclass(frozen=True)
class MarketState:
    """What a policy may see at decision time ``t`` (strictly causal; built from events <= t)."""

    recv_ts_ns: int
    ticker: str
    tau_s: float  # seconds to close
    best_yes_price: Decimal | None  # highest resting YES bid (dollars)
    best_yes_size: Decimal | None
    best_no_price: Decimal | None  # highest resting NO bid (dollars)
    best_no_size: Decimal | None
    index_level: float | None  # latest index/fair-value input as-of t (LOCF), or None
    fair_prob_up: float | None  # model P(up) if the harness supplied one, else None
    delta_x: float | None  # binary local delta (dV/dlogS) if supplied
    sigma: float | None  # per-sqrt-second vol if supplied
    inventory: int  # net YES contracts (+ long YES, - short YES)

    @property
    def yes_mid(self) -> Decimal | None:
        """YES-terms mid: (best YES bid + YES ask)/2, where YES ask = 1 - best NO bid."""
        if self.best_yes_price is None or self.best_no_price is None:
            return None
        yes_ask = Decimal(1) - self.best_no_price
        return (self.best_yes_price + yes_ask) / 2


@dataclass(frozen=True)
class QuoteIntent:
    """A desired resting order. ``side`` is the Kalshi book side; ``price`` dollars; ``size`` >= 1."""

    side: str  # "yes" | "no"
    price: Decimal
    size: Decimal
    is_taker: bool = False


class Policy(Protocol):
    def quotes(self, state: MarketState) -> list[QuoteIntent]:
        ...


@dataclass
class QuotingPolicy:
    """Adapter wrapping ``kbtc.quoting.quote`` (Avellaneda-Stoikov adapted to binaries).

    Requires the harness to supply ``fair_prob_up``, ``delta_x`` and ``sigma`` on the state (from
    the index replica + 3.1 vol + 3.2 fair value). When any is missing, or the near-strike endgame
    rule fires, it quotes nothing (pulls). It maps the quoter's YES bid/ask (cents) to a YES-side
    buy intent and a NO-side buy intent (= YES sell), both at ``size`` contracts.

    This is intentionally thin: all the modelling lives in ``quoting.py``; the acceptance study
    (4.2) will calibrate ``QuoteParams`` and the pull rules. Kept out of the tested hot path so the
    engine's correctness tests use a trivial deterministic policy instead.
    """

    size: Decimal = Decimal(1)
    params: object = None  # kbtc.quoting.QuoteParams; default built lazily
    use_endgame_pull: bool = True

    def quotes(self, state: MarketState) -> list[QuoteIntent]:
        from kbtc.quoting import QuoteParams, near_strike_endgame, quote

        if state.fair_prob_up is None or state.delta_x is None or state.sigma is None:
            return []
        params = self.params or QuoteParams()
        if self.use_endgame_pull and state.delta_x is not None and state.sigma is not None:
            # z ~ standardized distance; reuse the quoter's near-strike rule via the fair prob.
            # We approximate |z| from fair_prob_up only to decide the pull; the quoter owns pricing.
            import math
            p = min(max(state.fair_prob_up, 1e-9), 1 - 1e-9)
            z = math.sqrt(2) * _erfinv(2 * p - 1)
            if near_strike_endgame(state.tau_s, z):
                return []
        q = quote(state.fair_prob_up, state.delta_x, state.sigma, state.inventory, params)
        out: list[QuoteIntent] = []
        if q.bid_cents is not None:
            out.append(QuoteIntent(side="yes", price=Decimal(q.bid_cents) * CENT, size=self.size))
        if q.ask_cents is not None:
            # Sell YES at ask == buy NO at (1 - ask).
            no_price = (Decimal(100 - q.ask_cents)) * CENT
            out.append(QuoteIntent(side="no", price=no_price, size=self.size))
        return out


def _erfinv(x: float) -> float:
    """Inverse error function (Winitzki approximation) — only for the endgame |z| estimate."""
    import math

    a = 0.147
    ln = math.log(1 - x * x) if abs(x) < 1 else -float("inf")
    t1 = 2 / (math.pi * a) + ln / 2
    return math.copysign(math.sqrt(max(math.sqrt(t1 * t1 - ln / a) - t1, 0.0)), x)


@dataclass
class FixedQuotePolicy:
    """Deterministic test/diagnostic policy: always wants the given intents (independent of state).

    Used by the engine's unit tests to exercise queue/latency/fill/fee math without pulling in the
    quoting/index/vol stack. Not for research use.
    """

    intents: list[QuoteIntent] = field(default_factory=list)

    def quotes(self, state: MarketState) -> list[QuoteIntent]:
        return list(self.intents)
