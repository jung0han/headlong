#!/usr/bin/env bash
set -euo pipefail

# deploy/thinkers-service.sh — ExecStart/ExecStop body for
# headlong-thinkers@<identity>.service. Runs as the shellm user.
#
# Usage: thinkers-service.sh APP_DIR IDENTITY start|stop
#
# start: source the root .env and the identity, stop any stale dispatcher
# (wherever it lives — the ownership token makes old instances exit), then
# start every enabled thinker by name. Named start matters: the CLI's bare
# start only arms the dispatcher, while named start also kicks each thinker
# once so the mind actually wakes up (same reason the dash expands "start
# all" to explicit names, web/src/headlong_web/server.py).
#
# stop: drain stop, then wait for in-flight steps to finish so systemd's
# final cgroup sweep (KillMode=control-group) reaps only what refused to
# drain. Budget stays under the unit's TimeoutStopSec.

APP_DIR="${1:?usage: thinkers-service.sh APP_DIR IDENTITY start|stop}"
IDENT="${2:?identity name required}"
ACTION="${3:?action required (start|stop)}"

cd "$APP_DIR"
export PATH="$APP_DIR/bin:$APP_DIR/tools:$PATH"

# Same env layering as the web control plane's _ENV_WRAPPER: root .env
# first (API keys, SHELLM_MODEL), then the identity's own .env so
# identity-specific keys win, then activate for the identity paths.
if [[ -f "$APP_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$APP_DIR/.env"
    set +a
fi

ID_DIR="$APP_DIR/.identities/$IDENT"
[[ -d "$ID_DIR" ]] || { echo "error: identity not found: $ID_DIR" >&2; exit 1; }

if [[ -f "$ID_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ID_DIR/.env"
    set +a
fi

# activate is written for interactive shells: its internal greps legitimately
# fail, which is fatal under set -euo pipefail — relax the guards around the
# source (same dance as deploy/bootstrap-slack-identity.sh).
set +eu
set +o pipefail
# shellcheck disable=SC1091
source "$ID_DIR/activate"
set -eu
set -o pipefail
[[ -n "${IDENTITY_NAME:-}" ]] || { echo "error: activate did not set IDENTITY_NAME" >&2; exit 1; }

case "$ACTION" in
    start)
        # Refuse to enter the persistent loop on a stale key, wrong provider,
        # or endpoint mismatch. This probes both direct and shellm call paths
        # and writes a credential-free health result before the dispatcher is
        # forked.
        "$APP_DIR/tools/headlong-model-probe"

        # Always stop first so the dispatcher runs with the environment THIS
        # invocation sourced (see bootstrap-slack-identity.sh for the
        # stale-env incident that made this unconditional). --self: service
        # scripts are authorized stop paths — the guard in `thinkers stop`
        # exists to block in-flight mind steps, not systemd, and ExecStop
        # runs inside the unit's own cgroup where the guard would trip.
        thinkers stop --self || true

        # Reconcile this identity's thinkers with the bundled set BEFORE
        # enumerating the roster: bootstraps thinkers added upstream and
        # writes `disabled` markers into retired ones. Without this, an
        # existing identity never sees roster changes — nothing else on the
        # restart path calls the bootstrap (identity new/import/shell do,
        # none of which a deploy runs). Best-effort: a reconcile failure
        # must not keep the mind down.
        identity sync-thinkers "$IDENT" \
            || echo "warn: identity sync-thinkers failed — starting with the roster as-is" >&2

        names=()
        for tdir in "$ID_DIR/thinkers"/*/; do
            [[ -d "$tdir" ]] || continue
            tname=$(basename "$tdir")
            [[ "$tname" == _* ]] && continue
            [[ -f "$tdir/disabled" ]] && continue
            [[ -f "$tdir/step" && -f "$tdir/subscriptions.jsonl" ]] || continue
            names+=("$tname")
        done
        [[ ${#names[@]} -gt 0 ]] || { echo "error: no enabled thinkers for $IDENT" >&2; exit 1; }

        echo "==> Starting thinkers for $IDENT: ${names[*]}"
        exec thinkers start "${names[@]}"
        ;;
    stop)
        # --self: ExecStop runs in the unit's cgroup (same as the
        # dispatcher), which the guard in `thinkers stop` would refuse.
        thinkers stop --self || true

        # Wait for draining steps (CLI drain default 180s; unit
        # TimeoutStopSec=200 leaves headroom for the final sweep).
        step_pids_file="$ID_DIR/run/step_pids"
        deadline=$((SECONDS + 185))
        while (( SECONDS < deadline )); do
            alive=0
            if [[ -f "$step_pids_file" ]]; then
                while read -r spid _; do
                    if [[ -n "$spid" ]] && kill -0 "$spid" 2>/dev/null; then
                        alive=1
                        break
                    fi
                done < "$step_pids_file"
            fi
            [[ "$alive" -eq 0 ]] && break
            sleep 2
        done
        exit 0
        ;;
    *)
        echo "error: unknown action: $ACTION (want start|stop)" >&2
        exit 2
        ;;
esac
