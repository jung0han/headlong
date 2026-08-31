#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

SERVICE_USER=nobody
CODEX_HOME="$WORK/.codex"
mkdir -p "$CODEX_HOME/sessions/2026/08/28" "$CODEX_HOME/archived_sessions"
touch "$CODEX_HOME/sessions/2026/08/28/session.jsonl"
touch "$CODEX_HOME/state_5.sqlite" "$CODEX_HOME/auth.json"
chmod 0700 "$CODEX_HOME"
owner_before=$(stat -c '%u:%g' "$CODEX_HOME")

bash "$REPO/deploy/grant-codex-access.sh" "$CODEX_HOME" "$SERVICE_USER" >/dev/null

[[ "$(stat -c '%u:%g' "$CODEX_HOME")" == "$owner_before" ]]
getfacl -cp "$CODEX_HOME" | grep -q '^user:nobody:rwx$'
getfacl -cp "$CODEX_HOME/sessions/2026/08/28" \
    | grep -q '^default:user:nobody:rwx$'
getfacl -cp "$CODEX_HOME/sessions/2026/08/28/session.jsonl" \
    | grep -q '^user:nobody:rw-$'
getfacl -cp "$CODEX_HOME/state_5.sqlite" | grep -q '^user:nobody:rw-$'
if getfacl -cp "$CODEX_HOME/auth.json" | grep -q '^user:nobody:'; then
    echo "FAIL auth.json received service ACL" >&2
    exit 1
fi
if getfacl -cp "$CODEX_HOME" | grep -q '^default:user:nobody:'; then
    echo "FAIL Codex root received a default service ACL" >&2
    exit 1
fi

printf 'ok   Codex access delegation preserves ownership and excludes root secrets\n'
