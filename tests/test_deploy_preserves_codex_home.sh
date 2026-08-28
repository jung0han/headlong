#!/usr/bin/env bash
# Regression: deploy scripts must not take ownership of an existing external
# Codex home. The command stubs keep this test hermetic while still executing
# deploy/update.sh through its real archive-service branch.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

APP="$WORK/app"
STUB_BIN="$WORK/bin"
CODEX_HOME="$WORK/operator/.codex"
mkdir -p "$APP/deploy" "$APP/web/src/headlong_web/static" \
    "$STUB_BIN" "$CODEX_HOME"
cp "$REPO/deploy/update.sh" "$APP/deploy/"
cp "$REPO/deploy/headlong-archive.service" "$APP/deploy/"
chmod 0750 "$CODEX_HOME"

cat >"$STUB_BIN/sudo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-u" ]]; then
    shift 2
fi
case "${1:-}" in
    git|bash|systemctl|rm|visudo|augenrules|apt-get|useradd|usermod|chmod|chown)
        exit 0
        ;;
    tee)
        cat >/dev/null
        exit 0
        ;;
    cmp)
        exit 1
        ;;
    install)
        shift
        args=()
        while (($#)); do
            case "$1" in
                -o|-g|-m)
                    shift 2
                    ;;
                -d)
                    args+=("-d")
                    shift
                    ;;
                *)
                    args+=("$1")
                    shift
                    ;;
            esac
        done
        /usr/bin/install -m 0700 "${args[@]}"
        ;;
    *)
        exit 0
        ;;
esac
EOF

cat >"$STUB_BIN/curl" <<'EOF'
#!/usr/bin/env bash
printf '{"status":"ok"}\n'
EOF

chmod +x "$STUB_BIN/sudo" "$STUB_BIN/curl"

before=$(stat -c '%u:%g:%a' "$CODEX_HOME")
PATH="$STUB_BIN:$PATH" APP_DIR="$APP" UNIT_DST="$WORK/headlong-web.service" \
    CODEX_HOME="$CODEX_HOME" bash "$APP/deploy/update.sh" >/dev/null
after=$(stat -c '%u:%g:%a' "$CODEX_HOME")

if [[ "$after" != "$before" ]]; then
    printf 'FAIL existing external CODEX_HOME metadata changed: %s -> %s\n' \
        "$before" "$after" >&2
    exit 1
fi

printf 'ok   existing external CODEX_HOME metadata is preserved\n'
