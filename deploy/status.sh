#!/usr/bin/env bash
# kbtc live data-collection status — a compact at-a-glance dashboard.
#
# Keep a live window open (refreshes every 30 s) with:
#   ssh -t -i ~/.ssh/kbtc_hetzner root@<box-ip> 'watch -n 30 kbtc-status'
# or run once over SSH:  ssh -i ~/.ssh/kbtc_hetzner root@<box-ip> kbtc-status
#
# READS ONLY: inspects data/raw + the health report + systemd timer state. No order path, no secrets.
set -u
ROOT="${KBTC_DATA_ROOT:-/opt/kbtc/data/raw}"
VENV="/opt/kbtc/.venv/bin/python"

echo "== kbtc recorders @ $(hostname) == $(date -u '+%Y-%m-%d %H:%M:%SZ') =="
echo

# Per-feed liveness: newest-DATA-frame age + recent msgs/s (FAST — reads ONE newest file per feed,
# a few seconds regardless of total data size, so `watch -n 30` can't stack). DATA-age (not file
# mtime) so a heartbeat-only stall shows STALE. The full gap/CRC health report is expensive and is
# NOT run every tick; run it on demand (now windowed/fast too):
#   sudo -u kbtc $VENV -m kbtc.recorders.health --root $ROOT --start "$(date -u -d '-15 min' +%Y-%m-%dT%H:%M:%SZ)"
status_out="$(sudo -u kbtc "$VENV" -m kbtc.recorders.liveness --root "$ROOT" --print-status 2>/dev/null)"
if [ -n "$status_out" ]; then echo "$status_out"; else echo "(feed status unavailable)"; fi
echo

# Raw data on disk: total + per venue/channel with newest-write age (is it still being written NOW?).
echo "raw data on disk (newest write age = is it live?):"
now="$(date +%s)"
find "$ROOT" -mindepth 2 -maxdepth 2 -type d 2>/dev/null | sort | while read -r d; do
  feed="$(basename "$(dirname "$d")")/$(basename "$d")"
  sz="$(du -sh "$d" 2>/dev/null | cut -f1)"
  newest="$(find "$d" -name '*.jsonl.zst' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)"
  if [ -n "$newest" ]; then age="$(( now - ${newest%.*} ))s ago"; else age="no files"; fi
  printf "  %-34s %7s  (newest %s)\n" "$feed" "$sz" "$age"
done
printf "  %-34s %7s\n" "TOTAL" "$(du -sh "$ROOT" 2>/dev/null | cut -f1)"
echo

# Off-site backup + disk headroom.
bk_ts="$(systemctl show kbtc-backup.service -p ActiveEnterTimestamp 2>/dev/null | cut -d= -f2-)"
bk_res="$(systemctl show kbtc-backup.service -p Result 2>/dev/null | cut -d= -f2-)"
echo "last B2 backup run: ${bk_ts:-never}  (result: ${bk_res:-n/a})"
echo "disk: $(df -h "$ROOT" 2>/dev/null | awk 'NR==2{print $4" free of "$2" ("$5" used)"}')"
