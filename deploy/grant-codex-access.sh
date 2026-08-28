#!/usr/bin/env bash
set -euo pipefail

# Explicitly delegate the Codex filesystem access required by the production
# Observer and archive boundary without changing ownership. This is separate
# from setup/update because an external CODEX_HOME belongs to its operator.

CODEX_HOME_PATH="${1:-}"
SERVICE_USER="${2:-shellm}"

[[ -n "$CODEX_HOME_PATH" ]] \
    || { echo "Usage: $0 /absolute/path/to/.codex [service-user]" >&2; exit 2; }
[[ "$CODEX_HOME_PATH" =~ ^/[A-Za-z0-9._/-]+$ \
   && "$CODEX_HOME_PATH" != *"/../"* && "$CODEX_HOME_PATH" != */.. \
   && "$CODEX_HOME_PATH" != *"/./"* && "$CODEX_HOME_PATH" != */. \
   && "$CODEX_HOME_PATH" != *"//"* ]] \
    || { echo "ERROR: CODEX_HOME must be a normalized absolute path" >&2; exit 1; }
[[ -d "$CODEX_HOME_PATH" ]] \
    || { echo "ERROR: CODEX_HOME is not a directory: $CODEX_HOME_PATH" >&2; exit 1; }
id "$SERVICE_USER" >/dev/null 2>&1 \
    || { echo "ERROR: service user does not exist: $SERVICE_USER" >&2; exit 1; }
command -v setfacl >/dev/null 2>&1 \
    || { echo "ERROR: setfacl is required (install the acl package)" >&2; exit 1; }

for session_root in sessions archived_sessions; do
    [[ -d "$CODEX_HOME_PATH/$session_root" ]] \
        || { echo "ERROR: missing Codex session root: $CODEX_HOME_PATH/$session_root" >&2; exit 1; }
done

owner_before=$(stat -c '%u:%g' "$CODEX_HOME_PATH")

# Root write access is required for SQLite WAL/lock creation. Do not install a
# default ACL here: future auth/config files at the Codex root must not
# automatically become visible to the service account.
setfacl -m "u:$SERVICE_USER:rwx" "$CODEX_HOME_PATH"

# Codex scans nested date directories and renames a selected JSONL into the
# archive root. Default ACLs on the two session trees keep future sessions
# accessible without periodically rewriting the whole Codex home.
for session_root in sessions archived_sessions; do
    find "$CODEX_HOME_PATH/$session_root" -type d \
        -exec setfacl -m "u:$SERVICE_USER:rwx,d:u:$SERVICE_USER:rwx" {} +
    find "$CODEX_HOME_PATH/$session_root" -type f \
        -exec setfacl -m "u:$SERVICE_USER:rw" {} +
done

# The current CLI updates its root state database during archive/unarchive.
# Versioned state files are deliberately selected; auth and config are not.
find "$CODEX_HOME_PATH" -maxdepth 1 -type f -name 'state_*.sqlite*' \
    -exec setfacl -m "u:$SERVICE_USER:rw" {} +

owner_after=$(stat -c '%u:%g' "$CODEX_HOME_PATH")
[[ "$owner_after" == "$owner_before" ]] \
    || { echo "ERROR: CODEX_HOME ownership changed unexpectedly" >&2; exit 1; }

printf 'Delegated Codex session/archive access to %s; ownership remains %s\n' \
    "$SERVICE_USER" "$owner_after"
