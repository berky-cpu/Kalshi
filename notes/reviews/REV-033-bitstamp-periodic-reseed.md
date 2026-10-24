# REV-033 — Bitstamp periodic forced-reseed (lost-delete self-heal)

Date: 2026-10-23
Reviewer: red-team (adversarial)
Scope: uncommitted diff — `src/kbtc/recorders/base.py` (SHARED core), `src/kbtc/recorders/bitstamp.py`,
`tests/test_recorders_periodic_reseed.py` (new, 6), `tests/test_recorders_bitstamp.py` (+2).
Root cause under review: a lost `order_deleted` (wire loss, no `pre_event_id` chain break → no gap)
left a stale Bitstamp `live_orders` order resting for 2+ days (0 reseeds 10-23/24) → persistent gross
cross. Distinct from REV-026 (120s resurrection on a lagging REST snapshot).

## Verdict: PASS-WITH-CAVEATS
- Safe to COMMIT: yes.
- Safe to DEPLOY (one-feed Bitstamp restart): yes.
- base.py change safe to ride in ALL feeds: yes — strictly gated, proven no-op elsewhere.
- One must-do (monitor-after, not blocking): the post-deploy smoke check, because the live glue is
  pragma-no-cover and the shipped tests do NOT cover it (see Finding 1).

`uv run pytest -q` → **412 passed in 5.69s** (matches expectation).

## Findings (ranked)

### 1. [MEDIUM — monitor-after] The actual run_recorder glue is untested; the reproduction test would NOT fail if the wiring were reverted
`test_periodic_reseed_clears_lost_delete_stale_order` (tests/test_recorders_bitstamp.py:252+) is
faithful to the incident (create, NO delete, no gap, asserts `needs_resnapshot is False` and a gross
cross best_bid > best_ask), and it proves the two NEW pure pieces: `PeriodicReseedTimer.due()` fires
and `apply_snapshot` clear()+rebuild drops the dead order (`resurrections_suppressed == 0`, pure
clear). BUT the reseed in the test is performed by a direct `p.apply_snapshot(...)` call — it does NOT
exercise the `run_recorder` chain timer.due → `needs_resnapshot = True` → read-loop
`clear_resnapshot()` → `await resnapshot(p)` → `on_reseed()`. That chain lives in
`run_recorder` (base.py:848-876, 904-925) which is `# pragma: no cover - requires a live socket`.
So if the wiring (the part that is the fix) were reverted, these tests would STILL pass. This is the
same test/coverage split as the DataStalenessMonitor/REV-021 watchdog, and acceptable, but it means
the shipped automated tests do not protect the glue.
- Fix / mitigation: the author's recommended post-deploy smoke check IS the real verification and
  must be run — confirm the `event=periodic_reseed` INFO log fires ~every 20 min on the live
  Bitstamp feed and that `apply_snapshot` runs right after it. Treat as a release gate, not optional.
  (Optional hardening: a future test could drive the read loop with a fake ws/`resnapshot` seam like
  the resync unit tests do, to pin the glue.)

### 2. [LOW] Periodic reseed on a HEALTHY book re-incurs the REST-vs-WS lag transient 72x/day
Every reseed (gap or periodic) clear()s and rebuilds from a REST snapshot whose micro S lags the live
stream; orders CREATED in the (S, apply] lag window are not in the snapshot and are not replayed
(Bitstamp `order_data since_id` backfill is not implemented), so they transiently vanish from the
live book until the next reseed re-observes them. This was previously only paid on rare gap reseeds;
it is now paid 72x/day on otherwise-healthy books. Assessment: acceptable.
  - Direction is SAFE: the book under-reports (misses a real order), it does not cross → no false
    signal in the crossing-detector. `OrderIdBook.modify`/`delete` on a now-missing oid are no-ops
    (base.py:393 returns False; delete is equivalently guarded) → no crash from later deltas.
  - Data integrity preserved: the Bitstamp REST resyncer persists the snapshot into the recording
    (base.py:800-801), so an offline re-normalize applies the same snapshot and reconstructs
    correctly — the incident is fixed in the recorded data, not just the live book.
- Monitor-after: watch `resurrections_suppressed` and any crossing-after-periodic-reseed rate for a
  surprise. No code change required.

### 3. [INFO] Blast radius — proven no-op for every non-Bitstamp feed
`IoConfig.periodic_reseed_s` defaults `None` (base.py:691). `_periodic_reseed()` returns immediately
when it is None (base.py:857-858) → the task creates, hits that early return, and exits; no sleep
loop, no REST, no resubscribe, no behavior change. `PeriodicReseedTimer(None).due()` is always False
(base.py:720-726). Double-gated inside the task body it only flags `p.resync_on_gap` processors
(base.py:871-872), matching the connect gate (base.py:901-902), so trade/tape processors are never
touched. `test_only_bitstamp_enables_periodic_reseed` iterates ALL `run.FEEDS` and asserts every feed
except `bitstamp-live_orders` has `periodic_reseed_s is None` (and bitstamp == 1200). Full suite green
(412). Conclusion: zero extra REST load / resubscribe / behavior change on coinbase, kraken, gemini,
cryptocom, bullish, binance, bybit, coinbase_advanced, kalshi.

### 4. [INFO] Task lifecycle / no leak / separation from bg_tasks — OK
`pr` is a distinct variable created inside the per-connect `try` (base.py:883,888), separate from the
`bg_tasks` list that `on_connect` reassigns (base.py:894), and is `pr.cancel()`ed in the `finally`
(base.py:933-934) on every disconnect/reconnect — no task survives across reconnects. Note it is
cancelled but not awaited, identical to the pre-existing `hb`/`wd` pattern, so no new leak class.

### 5. [INFO] No double-reseed race; single-threaded check-and-act is atomic — OK
`reseed_timer.on_reseed()` is called (a) at connect after the initial seed (base.py:906), and (b) on
EVERY read-loop reseed, gap or periodic (base.py:925) — so a periodic reseed can never pile on right
after a gap reseed. The task itself also advances the clock immediately when it flags
(base.py:874-876) so it won't re-flag every check tick before the loop applies it. Within the task,
there is NO `await` between `due()` returning True and `needs_resnapshot = True` + `on_reseed()`
(base.py:862-876) — under single-threaded asyncio this is atomic, so no double-fire. Setting an
already-True `needs_resnapshot` is idempotent (the loop clears + reseeds once). Covered by
`test_reseed_resets_the_clock_defers_next` and `test_gap_reseed_defers_periodic_no_double_reseed`.

### 6. [INFO] Inline-fetch / reads-suspended invariant holds for the periodic trigger — OK
The periodic path only sets `needs_resnapshot`; the fetch+apply runs at base.py:920-922 via the
SAME `await resnapshot(p)` as the gap path, inside the `async for raw in ws` body, so reads are
suspended during the HTTP fetch exactly as for a gap. Therefore Bitstamp's post-snapshot floor and
the REV-026 resurrection-suppression guard apply unchanged; a slightly-lagging periodic snapshot
CANNOT reintroduce resurrection. `test_periodic_reseed_path_still_honours_rev026_suppression` asserts
a delete at micro >= S is still suppressed on the periodic path (`resurrections_suppressed == 1`,
uncrossed).

### 7. [INFO] REST-failure handling — OK, task cannot raise unhandled
`_periodic_reseed` does no network I/O (pure due()/flag/log), so it cannot raise from a REST failure.
The actual `await resnapshot(p)` is in the read loop inside the outer `try`; a failure propagates to
`except Exception` (base.py:937) → backoff reconnect → fresh reseed. No wedge, no crash of the task.
Edge: if the feed goes fully silent after a flag, the loop can't apply the reseed — but Bitstamp
inherits the default `stale_timeout_s = 120.0` (base.py:661; not overridden), so the staleness
watchdog force-closes and reconnects within ~120s, which itself reseeds. Bounded.

### 8. [INFO] N = 20 min — agree
Caps worst-case lost-delete staleness from indefinite/days to <= ~20 min + one check tick (check
cadence = min(N/4, 60s) = 60s, base.py:859). 72 fetches/day of the group=2 book, sub-second reads
suspended per fetch — negligible. Bitstamp per-IP HTTP ceiling is correctly flagged UNVERIFIED
(bitstamp.py:103-105); 20 min is orders of magnitude under any plausible limit, so the choice is
robust regardless. Sits in the sensible 15-30 min band.

### 9. [INFO] Safety — clean
No order path / `execution/guard.py` / `.claude/` / `settings.json` touched. No secrets read or
logged (the new INFO log carries venue/channel/interval only). No new dependencies. UNVERIFIED items
(Bitstamp HTTP rate limit; REST-vs-WS lag cause carried from REV-026) are marked as such in code.

## What would change my mind (toward FAIL / blocking)
- The post-deploy smoke check does NOT show `event=periodic_reseed` ~every 20 min on the live feed
  (would mean the untested glue is broken — Finding 1).
- Evidence the periodic reseed leaves the live book crossed, or that
  crossing-after-periodic-reseed > gap-reseed baseline (Finding 2 turning real).
- Any feed other than bitstamp showing `periodic_reseed_s != None` or an extra REST/resubscribe after
  deploy.
