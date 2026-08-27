#!/usr/bin/env bash
set -euo pipefail

# ExecStart body for the identity-scoped Personal Assistant source bridges.
# Root env, identity env, and activate follow thinkers-service.sh precedence.

APP_DIR="${1:?usage: assistant-service.sh APP_DIR IDENTITY codex|web}"
IDENT="${2:?identity name required}"
BRIDGE="${3:?bridge required (codex|web)}"

cd "$APP_DIR"
export PATH="$APP_DIR/bin:$APP_DIR/tools:$PATH"

if [[ -f "$APP_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$APP_DIR/.env"
    set +a
fi

ID_DIR="$APP_DIR/.identities/$IDENT"
[[ -d "$ID_DIR" ]] || { echo "error: identity not found: $IDENT" >&2; exit 1; }

if [[ -f "$ID_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ID_DIR/.env"
    set +a
fi

set +eu
set +o pipefail
# shellcheck disable=SC1091
source "$ID_DIR/activate"
set -eu
set -o pipefail
[[ "${IDENTITY_NAME:-}" == "$IDENT" ]] \
    || { echo "error: activate did not select requested identity" >&2; exit 1; }

# Both bridge services can call the model. Refuse a persistent failure loop
# before entering either source cycle, using the same direct + shellm route.
"$APP_DIR/tools/headlong-model-probe"

case "$BRIDGE" in
    codex)
        exec "$APP_DIR/tools/headlong-assistant" --identity "$IDENT" run-codex-bridge
        ;;
    web)
        exec "$APP_DIR/tools/headlong-assistant" --identity "$IDENT" run-web-bridge
        ;;
    *)
        echo "error: unknown bridge: $BRIDGE (want codex|web)" >&2
        exit 2
        ;;
esac
