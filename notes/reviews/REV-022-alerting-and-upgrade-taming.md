# REV-022 — Red-team review: failure alerting (#3b) + auto-upgrade taming (#4)

- **Date:** 2026-10-18
- **Reviewer:** red-team-reviewer (alerting #3b); upgrade-taming #4 applied as box ops + repo config.
- **Scope reviewed (#3b):** `src/kbtc/recorders/liveness.py`, `deploy/alert.sh`,
  `deploy/systemd/{kbtc-alert@.service, kbtc-liveness.service, kbtc-liveness.timer}`, OnFailure wiring on
  `kbtc-{backup,disk-alarm,settlement}.service`, `deploy/{env.example,README.md}`, `tests/test_liveness.py`.
- **Verdict:** **PASS — deploy, with F1 live-tested before trusting it.** `uv run pytest -q` → 323 passed
  (+13). Reads-only; no order path; no new dep; `guard.py`/`.claude/`/`writer.py`/`execution/` untouched.

## #3b — alerting backstop
The catch-all if the in-process self-heal watchdog (REV-021) ever fails: ntfy push on clear unit failures
(backup/disk/settlement via `OnFailure`) + an out-of-process liveness check (5-min timer) that pages on a
genuinely-stale feed.

**The key correctness point — DATA-age, not mtime — CONFIRMED + tested.** The recorder writes heartbeats to
disk too, so file mtime stays fresh during a heartbeat-only stall (the exact 11h incident). `liveness.py`
instead tracks the newest DATA frame's `recv_ts_ns` per feed (via `health.data_classifier`, heartbeats/acks
excluded); a test drives a real `RotatingZstdWriter` file (data 300s ago + heartbeats since, newest frame
1s old) and asserts STALE. The mtime trap is avoided.

**No false-paging (CONFIRMED):** just-started feed uses the earliest-record silence baseline (not flagged
until genuinely silent past threshold); `kalshi/settlement` excluded (batch timer); feeds with no files
skipped (e.g. the perp-only EU box). **Thresholds above the watchdog** (crypto 180s>120s, kalshi 420s>300s)
so a normal watchdog reconnect never double-pages. **alert.sh fail-safe:** no-op+exit0 when `KBTC_ALERT_URL`
unset, exit0 on curl failure (never cascades), never echoes the URL (tested). Health deliberately has NO
OnFailure (its late-start exit-nonzero would false-page every restart). Newest-file scan is chronological
with an hour-boundary fallback; a stall spanning files still decides STALE.

### Findings — applied
- **F1 [MEDIUM — applied + must live-test]:** `RestrictAddressFamilies=AF_INET AF_INET6` can block glibc
  getaddrinfo (needs AF_NETLINK/AF_UNIX) → curl DNS fails → alert silently dropped. **Fixed:** added
  `AF_NETLINK AF_UNIX` to both `kbtc-alert@.service` and `kbtc-liveness.service`. Still MUST confirm a real
  ntfy message arrives on deploy (`systemctl start kbtc-alert@test.service` with KBTC_ALERT_URL set).
- **F2 [LOW — applied]:** liveness checker crash would page nothing. **Fixed:** added
  `OnFailure=kbtc-alert@%n.service` to `kbtc-liveness.service` — it still alerts directly + exits 0 on
  normal stale detection (no double-page), and OnFailure fires only on a genuine checker crash.
- **F3 [LOW — accepted]:** total box death (power/kernel/network) isn't covered by any on-box timer; the
  complement is an external dead-man's-switch (uptime monitor / ntfy scheduled check). Accepted scope for now.
- **F4 [cosmetic]:** `%n` in the page subject. Harmless; the failed unit is identifiable.

## #4 — auto-upgrade taming (applied on both boxes; repo config added)
Root trigger of the incident: `unattended-upgrades` + needrestart auto-restarted all recorders at 06:34.
**Applied live on both boxes** and captured in the repo for reproducibility:
- `deploy/needrestart-kbtc.conf` → `/etc/needrestart/conf.d/zz-kbtc-no-restart.conf`: `$nrconf{restart}='l'`
  (list-only — security patches still install, services are NOT auto-restarted).
- `deploy/apt-99kbtc-no-reboot.conf` → `/etc/apt/apt.conf.d/99kbtc-no-reboot`: `Automatic-Reboot "false"`.
Defense in depth with the watchdog (which would self-heal a bad restart anyway). The incident class is now
defended three ways: won't restart → self-heals if it does → alerting backstop.
