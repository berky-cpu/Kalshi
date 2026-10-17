# REV-016 — Red-team review: WebSocket `max_size` (Coinbase large-snapshot frame fix)

- **Date:** 2026-10-17
- **Reviewer:** red-team-reviewer
- **Scope reviewed:** `src/kbtc/recorders/base.py` (`IoConfig.max_size`, `run_recorder` connect),
  `src/kbtc/recorders/coinbase_advanced.py` (`io_config`), tests in `test_recorders_base.py` +
  `test_recorders_coinbase_advanced.py`.
- **Verdict:** **PASS.** `uv run pytest -q` → 254 passed. Safety clean: `guard.py`/`.claude/`/`writer.py`
  untouched; no order path; no new dep (websockets already present).

## Why (live-only bug found deploying 1.1d)
The Coinbase Advanced `run_recorder` loop (the `# pragma: no cover` live path) ran "active" but wrote
nothing and reconnect-looped. A direct wire probe from the Ashburn box found the cause: Coinbase's level2
opening `snapshot` is ONE frame with ~40,880 updates (~1.6 MiB), but `websockets.connect` was called with
no `max_size`, so the library default **1 MiB** cap rejected the frame → connection dropped → silent retry
loop. The synthetic fixture's tiny snapshot could never trigger this — a textbook live-only bug (the F1 risk
class).

## Fix
- `IoConfig.max_size: int | None = 2**20` (exactly the websockets library default), forwarded verbatim to
  `websockets.connect(max_size=config.max_size)`.
- Coinbase Advanced `io_config` sets `max_size=None` (unbounded) — the full-book L2 snapshot is legitimately
  large and must not be dropped.

## Verified
- **No-shrink (determinative):** websockets 17.2's own `connect` default max_size IS `1048576 == 2**20`.
  Before this change `run_recorder` passed nothing (library default applied); now `IoConfig` defaults to
  `2**20` and forwards it — byte-identical effective limit for bitstamp/binance/bybit/coinbase-Exchange/
  kraken/kalshi (all omit the field). The ONLY override is coinbase_advanced (`None`). Nothing silently
  shrunk or enlarged.
- **`None` semantics:** in websockets, `max_size=None` disables the limit (not "use default"), so the large
  snapshot is accepted.
- **The forwarding test is real:** `run_recorder` (otherwise live-only) is driven with a monkeypatched
  `websockets.connect` fake that records kwargs then raises, zero-delay backoff + stop_event + cancel
  (terminates three ways, can't loop). It runs twice and asserts captured `max_size` is `None` for the
  unbounded config AND `2**20` for the default — a hardcode couldn't satisfy both. Companion tests pin
  `IoConfig().max_size == 2**20` and Coinbase `io_config.max_size is None`.

## Residual (informational, non-blocking)
- `max_size=None` means no per-frame size ceiling on the Coinbase-Advanced connection — a buggy/hostile
  server could in principle drive memory on that one recorder process. Accepted: trusted exchange over TLS,
  blast radius isolated to that recorder (separate connection/process). Lead chose `None` over a finite cap
  deliberately — for a recorder, silently rejecting an even-larger future snapshot would reintroduce the
  exact silent-drop bug just fixed; completeness > frame-DoS defense on a trusted feed. A finite 8–16 MiB
  cap remains an option if defense-in-depth is later wanted.
- F1 (per-connection `sequence_num` / snapshot-on-resubscribe still synthetic-only) remains the open gate;
  this fix unblocks capturing the real snapshot to close it. The wire probe already confirmed F1's premise
  (seq +1 across l2_data + subscriptions, in-stream snapshot at seq 0).
