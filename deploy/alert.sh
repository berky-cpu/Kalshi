#!/usr/bin/env bash
# kbtc failure notifier (alerting #3b). POSTs a short message to an ntfy topic in $KBTC_ALERT_URL.
#
# Usage: alert.sh "<subject>" "<body>"
#   - $KBTC_ALERT_URL is an ntfy topic URL (e.g. https://ntfy.sh/<unguessable-topic>), read from the
#     environment / systemd EnvironmentFile (/etc/kbtc/env) ONLY. It is NEVER echoed or logged here
#     (the topic string is the only "auth", so treat it like a secret).
#   - If $KBTC_ALERT_URL is unset/empty this is a NO-OP (log + exit 0), so a box without alerting
#     configured never errors and never breaks an OnFailure chain.
# READS ONLY. No order path. No secret is ever printed.
set -eu -o pipefail

subject="${1:-kbtc alert}"
body="${2:-}"
host="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown)"

if [ -z "${KBTC_ALERT_URL:-}" ]; then
  # Not configured on this box — do nothing (but say so, without naming the variable's value).
  echo "kbtc-alert: KBTC_ALERT_URL not set; skipping notification (subject: ${subject})" >&2
  exit 0
fi

message="${body}
host: ${host}"

# ntfy: title/priority/tags via headers; body is the POST data. --fail so a bad HTTP status is an
# error we can catch; we still exit 0 so alerting problems never cascade into the OnFailure chain.
if curl -fsS --max-time 15 \
    -H "Title: ${subject}" \
    -H "Priority: high" \
    -H "Tags: rotating_light,kbtc" \
    -d "${message}" \
    "${KBTC_ALERT_URL}" >/dev/null 2>&1; then
  echo "kbtc-alert: sent (subject: ${subject}) from host ${host}"
else
  echo "kbtc-alert: notification POST failed (subject: ${subject})" >&2
fi
exit 0
