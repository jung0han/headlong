#!/usr/bin/env bash
set -uo pipefail

# Alert on a Codex or Web bridge entering failed state. The systemd instance is
# codex-<identity> or web-<identity>; no model input or source transcript is read.

APP_DIR="${1:?usage: assistant-failure-alert.sh APP_DIR codex-IDENTITY|web-IDENTITY}"
INSTANCE="${2:?bridge instance required}"
case "$INSTANCE" in
    codex-*) component=codex; identity="${INSTANCE#codex-}" ;;
    web-*) component=web; identity="${INSTANCE#web-}" ;;
    *) exit 0 ;;
esac
[[ "$identity" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || exit 0

if [[ -r "$APP_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$APP_DIR/.env" 2>/dev/null || true
    set +a
fi

unit="headlong-assistant-${component}@${identity}.service"
summary=$(systemctl show "$unit" \
    -p Result,ExecMainCode,ExecMainStatus,ActiveState 2>/dev/null || true)
message=":rotating_light: *${unit} failed* — systemd will retry the ${component} bridge independently.
```
${summary}
```
Inspect bounded health: `headlong-assistant --identity ${identity} status`."
channel="${HEADLONG_ALERT_CHANNEL:-${SHELLM_ALERT_CHANNEL:-}}"
fallback="/var/tmp/headlong-assistant-alert.log"

if [[ -z "${SLACK_BOT_TOKEN:-}" || -z "$channel" ]]; then
    printf '%s [assistant-alert] %s failed; Slack is not configured\n' \
        "$(date -u +%FT%TZ)" "$unit" >>"$fallback"
    exit 0
fi

payload=$(jq -nc --arg ch "$channel" --arg text "$message" \
    '{channel: $ch, text: $text}')
response=$(curl -sS -m 15 -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-Type: application/json; charset=utf-8" \
    --data "$payload" 2>&1 || true)
if ! jq -e '.ok == true' >/dev/null 2>&1 <<<"$response"; then
    printf '%s [assistant-alert] Slack post for %s failed\n' \
        "$(date -u +%FT%TZ)" "$unit" >>"$fallback"
fi
