# REV-031 — WS protocol keepalive + bounded self-heal force-close (shared recorder core)

- Reviewer: red-team-reviewer
- Date: 2026-10-22
- Scope: uncommitted diff to src/kbtc/recorders/base.py, tests/test_recorders_base.py, docs/feeds.md
- Incident under remediation: Kalshi recorder sat on a half-closed CLOSE-WAIT socket ~50 min, never reconnected; self-heal did not fire. During a 0-open-markets maintenance window.
- Blast radius: this code rides in EVERY recorder (shared run_recorder). Top concern.

## Verdict: PASS-WITH-CAVEATS

Safe to deploy. Safe to ride in ALL feeds via a rolling restart (NOT kalshi-only-first). The change is behaviour-preserving for the installed library, the force-close hardening is strictly additive, and the full suite is green (400 passed). Caveats are about what the change does NOT prove (incident root cause) and one quiet-window interaction, not about correctness of the code.

## Claims verified

1. 20/20/10 ARE the installed library defaults. websockets==17.2 (pinned >=13, lock resolves 17.2). websockets.connect resolves to websockets.asyncio.client.connect; introspected defaults are ping_interval=20, ping_timeout=20, close_timeout=10. Before this diff, connect() passed NO ping kwargs, so every feed already ran under exactly these implicit values. Passing them explicitly is a behavioural no-op on 17.2 and removes a silent version-dependency. CONFIRMED.

2. IoConfig.ping_interval_s only drives the app-level JSON ping_message in _heartbeat() (base.py ~713-718: if not (config.ping_message and config.ping_interval_s): return), fully orthogonal to the library protocol PING/PONG now set via ws_ping_interval_s. CONFIRMED. No venue sets ws_ping_*/ws_close_*, so all feeds take 20/20/10. App-level pings per venue: kraken 30s, bybit 20s, bullish custom; binance/bitstamp/cryptocom/gemini/coinbase_advanced/kalshi = None. None conflict with the protocol keepalive.

3. No reconnect storm on a long idle window. The library keepalive keeps a HEALTHY idle peer up (it answers PING with PONG); only a dead/half-closed peer fails. This was already the behaviour under the implicit default, so no change. CONFIRMED.

4. Dynamic discovery survives reconnect. run_recorder re-invokes subscribe_factory() and on_connect(...) on every (re)connect inside the while loop (base.py ~690-694, ~764-765), and the finally cancels hb/wd/bg_tasks so a fresh poller is launched each time. The new test_run_recorder_reconnects_and_rediscovers_after_connection_closed asserts 2 connects, 2 discovery calls, 2 poller launches, and distinct per-connect subscribe payloads. CONFIRMED.

5. _force_close_ws correctness. asyncio.wait_for(ws.close(), bound) with bound = close_timeout + 0.5; on timeout OR any close error it hard-abort()s ws.transport. Verified against the real library: Connection.transport is assigned in connection_made (asyncio protocol callback), so a live post-handshake ws always has .transport, and .abort() is a real asyncio.Transport method — websockets itself uses self.transport.abort() internally. except Exception correctly does NOT swallow asyncio.CancelledError (BaseException in 3.12), so watchdog cancellation still propagates. No task leak: wait_for awaits cancellation of the inner close() before raising. Abort of a still-good transport cannot happen in practice: the watchdog only fires after >= stale_timeout of no data, and a healthy graceful close completes well within the 10.5s bound (proven by test_force_close_ws_graceful_close_does_not_abort). CONFIRMED SAFE.

6. Watchdog still detects Kalshi staleness. DataStalenessMonitor unchanged; Kalshi stale_timeout_s=300.0 (run.py:76,352,385); is_stale measures from last DATA frame (venue classifier excludes heartbeats/acks) or from connect. The only change is that force-reconnect now routes through _force_close_ws instead of a bare ws.close(), so a stalled graceful close can no longer wedge the self-heal on the very dead peer it is healing. CONFIRMED and IMPROVED.

7. Tests have teeth. Forwarding test KeyErrors if the three kwargs are dropped; defaults test AttributeErrors if the knobs are removed; both force-close tests ImportError if _force_close_ws is removed and assert the abort/no-abort branches directly. Reconnect test fails if the reconnect/rediscovery path breaks. All 5 pass; full suite 400 passed in ~6s.

8. Safety. No touch to the order-endpoint guard module, the hooks dir, the client settings file, secrets, env files, or any order path. Diff is base.py (+57), tests (+201), docs/feeds.md (+17). Only new import is a local stdlib import asyncio. No new dependency. CONFIRMED CLEAN.

## Caveats (ranked)

C1 (medium) — The fix does not, by itself, prove it prevents RECURRENCE of the exact incident, and the diff is honest that the on-box cause is UNVERIFIED. Analysis: a CLOSE-WAIT socket can still SEND (only the peer's send direction is FIN'd), so an ACTIVE library keepalive should have detected the dead peer in ~40s. The incident lasting ~50 min therefore implies that on the Oct-18/Oct-22 process EITHER the keepalive was not effectively active (older/implicit config, or a version where it differed) OR the data-staleness watchdog was not deployed / did not force-close. The in-repo code is sound REGARDLESS: two INDEPENDENT safeguards now exist — (a) explicit, version-pinned protocol keepalive (~40s) and (b) a transport/version-independent data-staleness watchdog (300s Kalshi) whose force-close is now bounded + hard-abort. Agreeing with the agent's two-independent-safeguards framing. The residual risk is purely deployment: the fix only takes effect after a recorder RESTART, and only if both safeguards are actually running on the box. Action: confirm post-restart that websockets==17.2 is the deployed version and that the staleness watchdog is live (log line data_stale ... forcing reconnect should appear if it ever trips).

C2 (low) — Quiet-window / maintenance interaction. The data-staleness watchdog cannot distinguish "dead socket" from "legitimately empty window" (e.g. a 0-open-markets Kalshi maintenance window where no DATA frames are expected). During such a window it will force-reconnect every ~300s until markets reopen. This is correct for the incident (dead socket) and self-limiting (not a storm), but it is low-frequency churn during legitimate maintenance. Acceptable; worth a one-line note so it is not mistaken for a fault later. Not a blocker.

C3 (low / nitpick) — test_ioconfig_ws_keepalive_defaults_match_library hardcodes 20/20/10 rather than introspecting the library, so if a future websockets bump changes the library defaults the "match library" claim would silently diverge while the test stays green. Functionally harmless because the values are now EXPLICIT (explicit wins regardless of library default). Consider asserting against the introspected library default if you want the "match" claim itself guarded.

C4 (low) — Causal link is simulated, not live-tested. run_recorder is # pragma: no cover (needs a live socket), so the reconnect test manually raises ConnectionClosedError to stand in for what the library keepalive would produce on a dead peer. The keepalive -> ConnectionClosed step is a library guarantee, not exercised here. This is an inherent and acceptable limitation of unit-testing a live-only loop; the simulation seam (fake websockets.connect) is faithful to the loop's usage.

## What would change my mind (to FAIL or kalshi-only-first)
- If any production feed were observed to reject client-initiated PINGs (would already have broken under the pre-existing implicit default — no evidence it does).
- If the deployed websockets were NOT 17.2 and its defaults differed such that making 20/20/10 explicit changed a feed's cadence materially (explicit here would then be the correction, still safe, but verify).
- If ws.transport.abort() were found to raise on a live 17.2 connection (it does not; library uses it internally).

## Deploy guidance
- Safe to deploy via a ROLLING RESTART across ALL feeds. The change is behaviour-preserving on the installed library and the self-heal hardening is additive; no reason to gate it kalshi-only-first.
- Requires a recorder restart to take effect (connect kwargs are read at connect time).
- Post-deploy, confirm deployed websockets==17.2 and watch for data_stale ... forcing reconnect log lines to confirm the watchdog backstop is live.
