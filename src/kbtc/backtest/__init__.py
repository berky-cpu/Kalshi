"""Event-driven, queue-aware backtest simulator for KXBTC15M maker quoting (ROADMAP 4.1).

OFFLINE backtest only. This package NEVER places, amends, or cancels a live/demo order and
imports no order path; the demo paper trader (Phase 5) is a separate module. The only thing it
touches is recorded data (raw Kalshi ``orderbook_delta``/``trade`` + an index/fair-value series).

Design (see ``docs/`` once the derivation note lands; summarized here):

* **Book data source.** The simulator reconstructs the FULL Kalshi depth (size at every price,
  both ``yes`` and ``no`` resting-bid sides) by replaying the raw ``kalshi/market`` stream through
  the SAME ``kbtc.recorders.kalshi.KalshiProcessor`` the recorder/normalizer use — NOT the slim
  top-of-book ``kalshi_books`` parquet and NOT the slow full-depth ``stream_kalshi`` normalize
  path. Rationale: (1) queue-aware fills need per-price resting size over time, which the TOB
  parquet discards; (2) ``stream_kalshi`` emits only a deduped top-N and does not interleave the
  trade tape with book deltas at exact ``recv_ts_ns`` — the event ordering a queue model depends
  on; (3) the confirmed ``synced`` annotation bug in ``stream_kalshi`` (shared-seq-across-markets)
  does not affect the book DATA, and we need no ``synced`` filter here (``require_synced=False``
  semantics) because we track gaps/snapshots ourselves. Replaying raw keeps the exact
  book+trade interleave with no reimplementation of the verified delta/snapshot apply logic.

* **Queue model.** Our order joins the BACK of the resting size at its price when it becomes
  effective (after submit latency). The queue ahead shrinks only via OBSERVED trades at that
  price and (conservatively or pro-rata) cancels inferred from book deltas net of trades. We fill
  only once the queue ahead reaches zero and a trade crosses; a strictly-more-conservative
  trade-through mode fills only when the market trades through our price.

See the submodules: ``events`` (typed event stream + raw parsing + merge), ``queue`` (the pure
fill model), ``feeapply`` (exact Decimal fee boundary), ``policy`` (policy interface + the
``quoting.py`` adapter), ``engine`` (the event loop), ``markout``/``report`` (the 4.2 outputs).
"""
from __future__ import annotations
