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

# Per-feed health over a rolling 5-minute window (HEALTHY / msgs-s / seq-gaps / gap-time).
# NOTE: the health CLI exits non-zero when ANY feed is flagged unhealthy, so we key the
# "unavailable" message on EMPTY output, not on the exit code (a flagged feed still prints a table).
start="$(date -u -d '-5 min' '+%Y-%m-%dT%H:%M:%SZ')"
health_out="$(sudo -u kbtc "$VENV" -m kbtc.recorders.health --root "$ROOT" --start "$start" 2>/dev/null)"
if [ -n "$health_out" ]; then echo "$health_out"; else echo "(health report unavailable)"; fi
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
