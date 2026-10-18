# REV-018 — Red-team review: dynamic Kalshi ticker management (ROADMAP 1.3)

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** NEW `src/kbtc/recorders/kalshi_dynamic.py`; `src/kbtc/recorders/kalshi.py`
  (sid capture, `update_subscription_message`, error-frame logging, `reset_subscriptions`);
  `src/kbtc/recorders/run.py` (`_build_kalshi` modes + `on_connect`/`subscribe_factory` wiring);
  `src/kbtc/recorders/base.py` (`run_recorder` `on_connect` hook); NEW `tests/test_kalshi_dynamic.py`,
  `tests/test_run.py`; `docs/feeds.md`, `deploy/{env.example,README.md}`.
- **Verdict:** **PASS (provisional — cross-roll deployed run is the operational confirmation)**, with red-team
  MEDIUMs F1/F2 **fixed and re-confirmed**. `uv run pytest -q` → 287 passed (was 257; +30). Safety clean:
  `guard.py`/`.claude/`/`recorders/writer.py`/`execution/` untouched; reads-only; no order path; no new dep
  (reuses `settlement.py`'s public fetcher).

## Why / design
KXBTC15M rolls a new market every 15 min, so a static ticker list only covers one window. One stable authed
WS: on connect, a `subscribe_factory` (fires at connect, like Kraken's token fetch — never at build)
discovers open markets via the PUBLIC REST (`markets?series_ticker=KXBTC15M&status=open`, reusing
`settlement.enumerate_markets`), subscribes `orderbook_delta`+`trade`; `KalshiProcessor` captures per-channel
`sid` from the `subscribed` acks. A background poller (`on_connect` hook, `KBTC_KALSHI_POLL_S` default 45 s)
re-discovers, diffs, and sends `update_subscription` `add_markets`/`delete_markets` over the
connection-bound `_send` — same sid, no reconnect. Books are keyed per `market_ticker`, so multiple markets
on one sid work; a snapshot re-anchors that sid's seq. Modes: **dynamic** (`KBTC_KALSHI_SERIES`), **static**
(`KBTC_KALSHI_TICKERS`, wins if both), **legacy** (neither).

## Live wire validation (Ashburn, Kalshi PRODUCTION authed read session, 2026-10-17) — PASS
- subscribe → `subscribed` acks: orderbook sid=1, trade sid=2.
- `update_subscription get_snapshot` on sid=1 → fresh `orderbook_snapshot` (seq re-anchored, 263 levels).
- `update_subscription add_markets [next window]` on sid=1 → ack `{"type":"ok",...}` **and** an
  `orderbook_snapshot` for the ADDED market on the SAME sid=1. No error-26/27, no reconnect.
- **New wire facts** (→ feeds.md): the `update_subscription` ack type is **`ok`** (NOT `subscribed`) and
  creates NO new sid, so sids are captured only from the initial `subscribed` acks; an `initialized` (not
  yet open) market can be pre-added and returns an empty snapshot.

## Verified correct
- **Shared `on_connect` hook is opt-in / non-regressing:** default None ⇒ `bg_tasks=[]`; other feeds
  (bitstamp/kraken/binance/bybit/coinbase/coinbase_advanced) byte-identical; bg tasks + hb cancelled in the
  `finally` on every disconnect (no task leak).
- **Pure diff/message logic** idempotent (unchanged open-set ⇒ no frames, no thrash); verified envelope
  `{cmd:update_subscription, params:{sids:[sid], market_tickers, action}}`, one-element `sids`, monotonic
  ids, skips channels without an acked sid, `apply_update` only after send, `ready()` gates.
- **sid capture isolation:** sids captured ONLY from `type=="subscribed"`; a `type:"ok"` ack carrying a sid
  does NOT corrupt sid state.

## MEDIUM findings — fixed + re-confirmed
- **F1 (silent subscription-rejection) CLOSED:** `process` now logs a WARNING on `type=="error"` frames
  (parses code/detail from flat AND nested shapes; code 26 = per-sub market limit, 27 = rate limit), with NO
  `_emit_gap` (not mistaken for a book gap → no spurious resync) and no crash on a malformed/bare error frame.
  No over-match (keys strictly on top-level `type=="error"`). Satisfies CLAUDE.md "never silent".
- **F2 (stale-sid dead-target) CLOSED:** `reset_subscriptions()` clears `sid_by_channel`, called by the
  dynamic `subscribe_factory` on EVERY (re)connect before building the subscribe. After a reconnect where
  only one channel re-acks, `ready()` stays False (poller sends nothing) and a build can only target fresh
  sids — the dead-sid path is eliminated; the clear fires only at connect (can't wipe a valid mid-connection
  sid).
- F3 (LOW): hb/hook now launched inside the `try` so the `finally` always cancels them — strict improvement,
  no normal-path change. F4 (LOW): `run_poller` wraps the whole per-tick body in try/log/continue → a
  transient bug can't silently kill the poller.

## Residual (non-blocking, documented fast-follow in code + deploy/README.md)
Applied-state advances on a successful SEND, not on the server `ok`; an error-26/27 is not yet rolled
back/retried, and the F5 partial multi-channel-send duplicate edge self-corrects on the next tick. Both are
improbable at KXBTC15M's ~1–2 open markets and now LOUD via F1, with reconnect self-heal. The full
reconcile-against-`ok`/`error` is the accepted deferred item. The end-to-end 15-min roll was not observed
live (only the mechanism it relies on) — the deployed cross-roll run is the operational confirmation.

## Cross-roll validation + deploy (2026-10-18) — PASS
Live run from the new code spanning the 03:30Z roll: **242,872 records, 0 unparseable lines,
`subscribed_acks=2`** (one connection — ZERO reconnects over the 10-min window), `update_subscription`
**4 `ok` acks**, and TWO distinct markets in the orderbook stream:
`KXBTC15M-26OCT172330-30` (seen from run start) and `KXBTC15M-26OCT172345-45` **first seen 03:30:20Z** — the
poller auto-added the new 15-min market 20 s after the boundary (= the 20 s poll interval), getting its
snapshot on the SAME connection with no disruption to the continuing market. End-to-end roll confirmed.
Deployed: `kbtc-recorder@kalshi` enabled on Ashburn with `KBTC_KALSHI_SERIES=KXBTC15M`, recording 24/7
(485 msg/s, 0 gaps/0 gap-time). The deferred reconcile-on-`ok`/`error` (applied-state) remains the accepted
fast-follow.
