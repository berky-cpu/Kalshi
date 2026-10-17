# Logging: journald, not logrotate

The recorders, settlement, health, backup, and disk-alarm units all log to **journald** via
`StandardOutput=journal` / `StandardError=journal` (each with its own `SyslogIdentifier`). The
application writes **no log files** of its own — the only files it creates are the raw-data
`*.jsonl.zst` recordings under `data/raw/`, which are rotated hourly by `recorders/writer.py` and
backed up/pruned by the backup job, not by logrotate.

So there is **no logrotate config to install**. Bound journald's disk use instead with the drop-in
in this directory:

```
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/logrotate/journald-kbtc.conf /etc/systemd/journald.conf.d/kbtc.conf
sudo systemctl restart systemd-journald
```

Inspect logs per unit, e.g.:

```
journalctl -u 'kbtc-recorder@coinbase-full' -f
journalctl -t kbtc-health --since -1h
journalctl -u 'kbtc-recorder@*' -p warning --since today   # all gap/reconnect warnings
```

If you later add file-based logging, add a real logrotate config here (`deploy/logrotate/kbtc`) and
reference it from the runbook.
