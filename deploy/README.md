# kbtc recorders — VPS deployment runbook (ROADMAP 1.3)

What this box does: **records free order-level crypto + Kalshi market data 24/7** to
`data/raw/{venue}/{channel}/date=YYYY-MM-DD/HH_*.jsonl.zst`, captures KXBTC15M settlement ground
truth, runs a health report, backs the data up off-site, and alarms on disk usage.

> **PAPER TRADING ONLY (CLAUDE.md hard rule 1).** The recorders do **market-data READS** only —
> there is **no order path anywhere** in `kbtc.recorders`. Kalshi *production* is used for reads;
> the paper trader (later phase) talks to Kalshi **demo** only, through `kbtc.execution.guard`.
> Nothing in this runbook places an order. Never put order/trade credentials on this box for the
> recorder's sake.

The 72-hour unattended RUN itself waits for Berk's VPS; this document + the units/scripts are the
reviewable code prep.

---

## 0. Assumptions

- Ubuntu LTS, small VPS (2 vCPU / 4 GB / 80+ GB disk).
- Install root: `/opt/kbtc` (the cloned repo). Data: `/opt/kbtc/data/raw`. Secrets: `/etc/kbtc/`.
- Service user: `kbtc` (no login shell, no sudo).

## 1. Create the service user and directories

```bash
sudo useradd --system --home /opt/kbtc --shell /usr/sbin/nologin kbtc
sudo mkdir -p /opt/kbtc /opt/kbtc/data/raw /etc/kbtc
sudo chown -R kbtc:kbtc /opt/kbtc
```

## 2. Install uv and clone the repo

```bash
# uv (pinned installer); then put it on root's PATH for the setup steps.
curl -LsSf https://astral.sh/uv/install.sh | sudo sh
sudo -u kbtc git clone <repo-url> /opt/kbtc
cd /opt/kbtc
sudo -u kbtc uv sync            # creates /opt/kbtc/.venv from uv.lock (incl. cryptography for Kalshi auth)
```

Smoke-test a feed build without connecting (no secrets needed to parse args):

```bash
sudo -u kbtc /opt/kbtc/.venv/bin/python -m kbtc.recorders.run --help
```

## 3. Create the secrets file `/etc/kbtc/env` (NEVER committed)

```bash
sudo cp /opt/kbtc/deploy/env.example /etc/kbtc/env
sudo chmod 600 /etc/kbtc/env
sudo chown root:root /etc/kbtc/env
sudoedit /etc/kbtc/env          # fill in the real values
```

Required variables (names only — values live only in this file; code reads them from the
environment and never prints them):

| Feed | Variables |
|---|---|
| `coinbase-full`, `coinbase-level3` | `COINBASE_API_KEY`, `COINBASE_API_SECRET`, `COINBASE_API_PASSPHRASE` |
| `kraken-level3` | `KRAKEN_API_KEY`, `KRAKEN_API_SECRET` |
| `bitstamp-live_orders`, `binance-perp`, `bybit-perp` | *(none — public)* |
| `kalshi` | `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` |
| backup | `KBTC_BACKUP_REMOTE` |

Kalshi private key:

```bash
sudo install -m 600 -o root -g root your-kalshi-key.pem /etc/kbtc/kalshi-private-key.pem
# set KALSHI_PRIVATE_KEY_PATH=/etc/kbtc/kalshi-private-key.pem in /etc/kbtc/env
```

A missing required variable makes the runner **exit non-zero** with a message that NAMES the
variable (never its value), so systemd `Restart=always` will not mask a misconfiguration silently —
check `journalctl -u 'kbtc-recorder@<feed>'`.

## 4. Set up the rclone backup remote (target UNDECIDED → remote-agnostic)

```bash
sudo apt-get install -y rclone
sudo RCLONE_CONFIG=/etc/kbtc/rclone.conf rclone config   # add a remote: b2 / s3 / r2 / ...
# The backup unit runs as the `kbtc` user, so the config must be kbtc-READABLE (640 root:kbtc),
# NOT root-only 600 — else rclone fails "permission denied" and the nightly backup silently fails.
sudo chown root:kbtc /etc/kbtc/rclone.conf
sudo chmod 640 /etc/kbtc/rclone.conf
# set KBTC_BACKUP_REMOTE=<remote>:<bucket>/<path> in /etc/kbtc/env, e.g. b2:my-bucket/kbtc
```

## 5. Bound journald (no logrotate needed — see deploy/logrotate/README.md)

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp /opt/kbtc/deploy/logrotate/journald-kbtc.conf /etc/systemd/journald.conf.d/kbtc.conf
sudo systemctl restart systemd-journald
```

## 6. Install the units

```bash
sudo cp /opt/kbtc/deploy/systemd/*.service /opt/kbtc/deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Enable one recorder instance per feed:

```bash
for f in coinbase-full coinbase-level3 kraken-level3 bitstamp-live_orders \
         binance-perp bybit-perp kalshi; do
  sudo systemctl enable --now "kbtc-recorder@${f}"
done
```

Enable the periodic jobs (timers, not the services directly):

```bash
sudo systemctl enable --now kbtc-settlement.timer kbtc-health.timer \
                            kbtc-backup.timer kbtc-disk-alarm.timer
```

## 7. Verify

```bash
# All recorder instances up?
systemctl list-units 'kbtc-recorder@*' --no-pager
# Data landing?
sudo -u kbtc find /opt/kbtc/data/raw -name '*.jsonl.zst' -mmin -5
# Health report (exit 0 = all feeds healthy; non-zero = at least one unhealthy):
sudo -u kbtc /opt/kbtc/.venv/bin/python -m kbtc.recorders.health --root /opt/kbtc/data/raw ; echo "exit=$?"
# Timers scheduled?
systemctl list-timers 'kbtc-*' --no-pager
# Disk alarm (exit 0 ok / 1 warn / 2 critical):
sudo -u kbtc /opt/kbtc/.venv/bin/python -m kbtc.recorders.disk_alarm --path /opt/kbtc/data ; echo "exit=$?"
```

## 8. Alerting (wire up before the 72 h run)

The health, backup, and disk-alarm units **exit non-zero on a problem**, which marks the unit
failed. Attach an `OnFailure=` handler (email / ntfy / webhook) — see the commented lines in those
`.service` files — e.g. create `kbtc-alert@.service` that posts `%i` to your notifier.

## Graceful shutdown / restart

`systemctl stop kbtc-recorder@<feed>` sends `SIGTERM`; the runner stops its loop and flushes the
final zstd frame (writer `close()`), so no partial frame is left. `Restart=always` + `RestartSec=5`
covers crashes; the in-process WS reconnect (jittered backoff) covers transient disconnects without
a process restart.

## Operational notes / open items

- **Kraken token refresh:** the WS token (~900 s TTL) is re-fetched on every (re)connect, so a long
  in-process reconnect is covered. Confirm the key has **"WebSocket interface — On"** permission and
  read-only (no trade) scope.
- **Kalshi dynamic ticker capture (ROADMAP 1.3):** set `KBTC_KALSHI_SERIES=KXBTC15M` for 24/7
  capture — the recorder discovers open markets (public REST) and tracks the 15-min roll via
  `update_subscription` add/delete on ONE connection (VERIFIED on the wire 2026-10-17; see
  docs/feeds.md). `KBTC_KALSHI_TICKERS` (static, no poller) still exists for a one-off window and
  takes precedence if both are set. Gap **resync** remains a fail-safe no-op pending ROADMAP 1.1b
  sid-pruning (raw is always persisted). Tune `KBTC_KALSHI_POLL_S` / `KBTC_KALSHI_CHANNELS` if needed.
  WS error frames (code 26 = per-sub market limit, 27 = rate limit) now LOG a WARNING
  (`event=kalshi_ws_error`) instead of being silently dropped. **Known fast-follow** (improbable at
  KXBTC15M's ~1-2 open markets, now observable via that WARNING): the poller advances its applied
  state on a successful send, not on the server `ok`, and does not yet roll back / retry on an
  error-26/27 reject or a partial multi-channel send — a deeper reconcile-against-ack fix is deferred.
- **Backup target** (`KBTC_BACKUP_REMOTE`) is Berk's decision; the script is remote-agnostic.
- **Retention/pruning** of old local days after a verified backup is intentionally NOT automated
  here (no destructive deletes until the backup story is signed off); add a prune step later.
