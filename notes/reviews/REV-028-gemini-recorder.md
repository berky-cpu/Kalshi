# REV-028 — Gemini v2 `marketdata` `l2` recorder (BRTI constituent)

Reviewer: red-team (skeptical). Date: 2026-10-21. Gates: DEPLOY of a new additive live feed
`gemini-l2` to the Ashburn box.
Scope: `src/kbtc/recorders/gemini.py` (+ tests, fixture) and the shared wiring edits in
`resync.py`, `run.py`, `health.py`, `normalize/pipeline.py`, `normalize/extract.py`,
`docs/facts.md`, `docs/feeds.md`.

## Verdict: PASS-WITH-CAVEATS — safe to DEPLOY as an additive recorder, with monitoring.

`uv run pytest -q` = **376 passed** (matches the stated expectation). No order path, no secrets,
and no edits to the execution guard, the hooks, or the Claude settings. The feed is public /
no-auth and purely additive: it writes only to `gemini/l2_btcusd` and shares code paths with the
other four feeds whose tests remain green. Nothing here can touch the existing feeds' data or the
demo/production order path.

The adapter is a faithful clone of the `coinbase_advanced` in-band-snapshot pattern, correctly
specialised for a feed that has **no sequence number**. The honest limitation (non-crossing silent
gaps are undetectable in-band) is disclosed in `docs/facts.md` (F-11) and `docs/feeds.md` §5B. The
one real data-quality risk is the snapshot-vs-delta discriminator (F1 below); it is recoverable and
probe-supported, hence caveat-not-block.

---

## Findings (ranked by severity)

### F1 (MEDIUM — monitor before/early-after deploy). Snapshot discriminator keys on `trades`/`auction_events`; a mis-classified frame corrupts the book silently and is NOT caught by crossed-book or the staleness watchdog.
Evidence: `src/kbtc/recorders/gemini.py:93` (`if "trades" in msg or "auction_events" in msg:` ->
clear+rebuild) and the same rule mirrored in `src/kbtc/normalize/pipeline.py:67`.

This single key-presence test is the load-bearing assumption for the whole feed. Two failure
directions, both silent:
- A real **snapshot without** `trades`/`auction_events` (e.g. Gemini omits empty arrays on a quiet
  reseed) is treated as a delta -> on initial connect it hits `if not self.synced:`
  (`gemini.py:104`) and the book never seeds. It is NOT caught by the staleness watchdog, because
  `l2_updates` frames count as DATA (`health.py:228` `_is_data_gemini`) — the clock keeps resetting
  while the book stays empty.
- A **non-snapshot `l2_updates` carrying `auction_events`** (or `trades`) during a Gemini auction
  would be misread as a reset -> `book.clear()` + rebuild from a handful of changes -> a tiny but
  **`synced=True`** book. Crossed-book won't fire (one bid / one ask, far apart, not crossed), the
  watchdog won't fire (data is flowing), so it is silently wrong until the next natural reconnect
  reseeds.

The probe (40 frames, 2026-10-20) verified deltas never carry `trades`/`auction_events` and
snapshots always do, but it almost certainly did not span a Gemini auction. The crossed-book net
and reconnect-reseed bound the blast radius (a corrupt window self-heals at the next reseed, and it
is one of 3-4 constituents a tracking study would flag), which is why this is a caveat not a block.

Concrete fix / mitigation (pick one, order of preference):
1. Deploy-time monitor (cheap, do this regardless): alarm if the reconstructed top-of-book depth
   collapses from ~thousands of levels to a handful between consecutive `synced=True` snapshots, or
   if `synced` stays False for > ~30 s while `l2_updates` keep arriving. Neither is currently
   watched (the staleness watchdog is blind here).
2. Before relying on Gemini in the model: run a longer read-only probe spanning a Gemini BTCUSD
   auction window and confirm no mid-stream `l2_updates` ever carries `auction_events`/`trades`.
3. Optionally harden the discriminator to also require a large, both-sided `changes` array (a real
   snapshot is ~4.5k levels), so a one-level `auction_events` frame can't be mistaken for a reset.

### F2 (LOW). Health replay labels Gemini sequence-gap count `exact`, but it is an undercount between recorded reseeds.
Evidence: `health.py:164` `_UPPER_BOUND_GAP_VENUES = {"kraken"}` (Gemini absent -> `seq_exact=True`
at `health.py:712`), combined with the one-shot `_crossed_flagged` guard (`gemini.py:106-108`,
`117-120`) and the fact that `make_processor` returns `prime=None` for Gemini so the replay loop
never clears `needs_resnapshot`/re-anchors between recorded snapshots (`health.py:662`
`if proc.needs_resnapshot and prime is not None:` is skipped).

Consequence: multiple distinct missed-removal episodes inside one un-reseeded window collapse to a
single logged gap, so the replayed count is a **lower bound**, yet it is reported as exact. This is
the same structural caveat Kraken carries (opposite direction) and is harmless to data, but the
`exact` label is misleading for the operator. Fix: add `"gemini"` to a "best-effort gap count"
caveat set (or a new flag) and surface it like Kraken's `~`. Not deploy-blocking.

### F3 (LOW / informational). Live vs offline book state diverges during the crossed-book window (but the emitted data product stays honest).
Evidence: live resync clears the book and drops deltas (`gemini.py:begin_resync` 138-148, driven by
`resync.py:ResubscribeResyncer.__call__` 193-197); offline normalize does NOT call `begin_resync`
— it only clears `needs_resnapshot` (`pipeline.py:208-217`) and keeps applying the diverging
deltas, emitting them as `synced=False`. So the processor's in-memory book differs between live and
replay during that window. This is immaterial because (a) live only persists raw frames — the
parquet from `normalize` is the product, and (b) both paths agree again at the next
`trades`-bearing reseed, and the diverged window is honestly flagged `synced=False`
(`pipeline.py:219`, verified by `test_normalizer_completeness_and_trade_tape`). No fix needed; noted
so the "Bitstamp lesson" (live vs normalized must match) is explicitly checked — it matches on the
emitted rows and on the post-reseed book.

---

## Checklist resolution for the caller's specific questions

1a. reconnect=reseed wiring — CORRECT. `gemini_resync_builder` (`resync.py:304`) ->
    `ResubscribeResyncer(unsubscribe+subscribe, send)`, `resync_on_connect=False`
    (`resync.py:183`). On connect `run_recorder` relies on `subscribe_messages` for the first
    snapshot and does NOT fire the resyncer (`base.py:697`), so no double-subscribe; a crossed-book
    gap sends unsubscribe-then-subscribe (`gemini.py:164-169`) so no second concurrent sub.
1b. snapshot-vs-delta discriminator + awaiting-snapshot race — logic is CORRECT given the
    assumption in F1. A stale delta during `awaiting_snapshot` is dropped without re-arming
    (`gemini.py:102-103`), and a delta can never be mistaken for the rebuild snapshot because it
    lacks `trades`. Covered by `test_awaiting_snapshot_drops_stale_deltas_and_sustained_cross_logs_once`.
    The soundness is only as strong as F1's assumption.
1c. crossed-book false positive/negative — No false positive from real data (a single venue never
    rests bid>ask; our book only crosses on a missed removal). The one-shot `_crossed_flagged`
    guard prevents a storm. False-negative (locked book `bid==ask`, or any non-crossing miss) is
    the disclosed limitation (F-11), acceptable and bounded by reseeds.
1d. honest limitation — Disclosed correctly in `docs/facts.md` F-11 and `docs/feeds.md` §5B, and
    marked PARTIAL. Acceptable for an additive constituent.
2.  Book correctness — `PriceLevelBook.set` SET/0-removes semantics match Gemini's absolute-qty
    changes (`gemini.py:127-136`, `base.py:207-214`); `best_bid`/`best_ask` correct;
    prices/qtys stay `Decimal` from transmitted strings (no float at boundary).
3.  Normalizer path — `_is_recorded_complete_snapshot` gemini branch (`pipeline.py:64-67`) mirrors
    the processor; offline reconstruction matches live on emitted rows and at reseed (F3).
    `exch_ts_ns` correctly None (`extract.exch_ts_ns` has no gemini branch -> None); None is
    already a supported downstream value (coinbase level3 positional rows also emit None), nothing
    breaks. Trade tape (`_gemini_trades` `extract.py:202-217`) emits only standalone `type:"trade"`
    frames; the snapshot's embedded last-50 `trades` backfill is correctly NOT re-emitted.
4.  health.py classifier `_is_data_gemini` (`health.py:224-228`) counts `l2_updates`+`trade` as
    data, excludes `heartbeat`/acks — correct for the staleness watchdog, EXCEPT it cannot catch
    the F1 "data flowing but book broken" case (that is F1, not a classifier bug).
5.  Safety / blast radius — public/no-auth (`run.py:409-413` `required_env=()`), no credentials, no
    order path, no safety-file edits, no new dependencies, additive `gemini/l2_btcusd` writer path.
    Other venues' tests remain green (376 passed).
6.  Deprecation (F-10) — Recording off an archived-but-live endpoint is acceptable for now and
    honestly flagged. A mid-run retirement surfaces as disconnect -> reconnect -> (failed resub) ->
    the staleness/health gap alarm, i.e. caught as missing data rather than silent corruption.
    Acceptable-to-monitor.
7.  Tests have teeth — Yes. The 7 tests fail if the discriminator, the crossed-book detector, the
    one-shot guard, or the awaiting-snapshot drop are broken (each asserts the specific state).
    The fixture matches the verified wire schema (snapshot carries `trades`/`auction_events`;
    deltas carry only `changes`; a `"0.0"` removal; heartbeat with `socket_sequence`; standalone
    trade). Gap in coverage: no test exercises F1's mis-classification failure modes, and none
    asserts live-vs-offline book parity across the gap (F3) — add a mis-classification regression
    test if the discriminator is hardened.

---

## What would change my mind (to full PASS)
- A longer read-only probe spanning a Gemini BTCUSD auction confirming `auction_events`/`trades`
  never ride a non-snapshot `l2_updates` (closes F1 direction B), AND confirmation that every
  snapshot always carries the key even when empty (closes F1 direction A); OR a hardened
  discriminator + a regression test for both directions.
- A deploy-time monitor that alarms on book-depth collapse / prolonged `synced=False` while
  `l2_updates` flow (the watchdog blind spot).

## What would change it to FAIL
- Evidence that Gemini BTCUSD emits mid-stream `auction_events`/`trades`-bearing `l2_updates`
  (silent book corruption with no in-band detector), with no monitor in place.
