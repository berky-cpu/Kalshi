#!/usr/bin/env bash
#
# Daily off-site backup of the raw-data tree via rclone (ROADMAP 1.3).
#
# Backs up the PREVIOUS UTC day's data/raw (that day's hourly zstd files are all closed by the
# 00:20 UTC run) to a configurable, remote-agnostic rclone destination, then VERIFIES the copy and
# exits non-zero on any failure (so the systemd OnFailure handler can alert).
#
# The backup target is UNDECIDED, so this is deliberately rclone-remote-agnostic: set
# KBTC_BACKUP_REMOTE to any rclone remote + path, e.g. b2:bucket/kbtc, r2:bucket/kbtc,
# s3:bucket/kbtc. Remote credentials live in the rclone config (RCLONE_CONFIG) on the box only and
# are NEVER committed. No secrets are echoed by this script.
#
# Config (environment):
#   KBTC_BACKUP_REMOTE   (required)  rclone remote + base path, e.g. "b2:my-bucket/kbtc"
#   KBTC_DATA_ROOT       (optional)  local data root; default /opt/kbtc/data/raw
#   KBTC_BACKUP_DATE     (optional)  YYYY-MM-DD to back up; default = yesterday (UTC)
#   RCLONE_CONFIG        (optional)  path to rclone.conf; default /etc/kbtc/rclone.conf
#
set -euo pipefail

DATA_ROOT="${KBTC_DATA_ROOT:-/opt/kbtc/data/raw}"
export RCLONE_CONFIG="${RCLONE_CONFIG:-/etc/kbtc/rclone.conf}"

log() { printf '%s backup.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

command -v rclone >/dev/null 2>&1 || die "rclone not installed"
[ -n "${KBTC_BACKUP_REMOTE:-}" ] || die "KBTC_BACKUP_REMOTE is not set"
[ -d "$DATA_ROOT" ] || die "data root not found: $DATA_ROOT"

# Previous UTC day by default (GNU date on Ubuntu).
BACKUP_DATE="${KBTC_BACKUP_DATE:-$(date -u -d 'yesterday' +%Y-%m-%d)}"

# Each venue/channel tree is laid out as {venue}/{channel}/date=YYYY-MM-DD/HH_*.jsonl.zst.
# rclone's --include selects just the chosen day's partitions across every feed in one pass.
REMOTE="${KBTC_BACKUP_REMOTE%/}"
INCLUDE="/**/date=${BACKUP_DATE}/**"

# Guard against a FALSE "backup OK": rclone copy + check both exit 0 when the --include matches
# nothing, so a wrong-but-existing KBTC_DATA_ROOT or silently-stopped recorders would otherwise
# report success after backing up zero bytes. Count the day's source files first and fail if none
# (set KBTC_BACKUP_ALLOW_EMPTY=1 to intentionally permit an empty day).
N_SRC=$(find "$DATA_ROOT" -type f -path "*/date=${BACKUP_DATE}/*" -name '*.jsonl.zst' 2>/dev/null | wc -l | tr -d ' ')
if [ "$N_SRC" -eq 0 ] && [ "${KBTC_BACKUP_ALLOW_EMPTY:-0}" != "1" ]; then
  die "no source files for date=${BACKUP_DATE} under ${DATA_ROOT} (recorders stopped or wrong KBTC_DATA_ROOT?); set KBTC_BACKUP_ALLOW_EMPTY=1 to override"
fi

log "syncing ${DATA_ROOT} [date=${BACKUP_DATE}, ${N_SRC} files] -> ${REMOTE}"

# --immutable: once uploaded a raw file must never change (recorders write new files, never mutate
# closed ones) -> a changed source file is an error, not a silent overwrite.
rclone copy "$DATA_ROOT" "$REMOTE" \
  --include "$INCLUDE" \
  --immutable \
  --transfers 4 --checkers 8 \
  --stats-one-line --stats 30s \
  --log-level INFO \
  || die "rclone copy failed"

# Verify: byte-for-byte presence/size/hash check of the same day's partitions. `check` exits
# non-zero if anything differs or is missing.
log "verifying copy (rclone check) for date=${BACKUP_DATE}"
rclone check "$DATA_ROOT" "$REMOTE" \
  --include "$INCLUDE" \
  --one-way \
  --log-level NOTICE \
  || die "rclone check failed (backup incomplete)"

log "backup OK for date=${BACKUP_DATE}"
