#!/usr/bin/env bash
# DONGWOO-918: disposable service rendering and environment-coupling smoke.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s%s\n' "$1" "${2:+ — $2}"; }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }

APP="$WORK/app"
mkdir -p "$APP/deploy" "$APP/tools" "$APP/.identities/observer" "$WORK/systemd"
cp "$REPO/deploy/assistant-service.sh" "$APP/deploy/"
cat >"$APP/.env" <<'EOF'
LLM_PROVIDER=openai
LLM_API_URL=https://root.invalid/v1/chat/completions
OPENAI_API_KEY=root-secret
EOF
cat >"$APP/.identities/observer/.env" <<'EOF'
SHELLM_MODEL=deepseek-test-route
OPENAI_API_KEY=identity-secret
EOF
cat >"$APP/.identities/observer/activate" <<EOF
export IDENTITY_NAME=observer
export IDENTITY_DIR=$APP/.identities/observer
EOF
cat >"$APP/tools/headlong-model-probe" <<'EOF'
#!/usr/bin/env bash
[[ "$LLM_PROVIDER" == openai ]]
[[ "$LLM_API_URL" == https://root.invalid/v1/chat/completions ]]
[[ "$SHELLM_MODEL" == deepseek-test-route ]]
[[ "$OPENAI_API_KEY" == identity-secret ]]
printf 'probe-ok\n' >>"$SMOKE_RECORD"
EOF
cat >"$APP/tools/headlong-assistant" <<'EOF'
#!/usr/bin/env bash
printf '%s|%s|%s|%s\n' "$*" "$LLM_PROVIDER" "$SHELLM_MODEL" "$OPENAI_API_KEY" >>"$SMOKE_RECORD"
EOF
chmod +x "$APP/tools/headlong-model-probe" "$APP/tools/headlong-assistant"

export SMOKE_RECORD="$WORK/record"
if bash "$APP/deploy/assistant-service.sh" "$APP" observer codex >/dev/null 2>"$WORK/codex.err"; then
    ok "Codex service wrapper loads root and identity model route"
else
    bad "Codex service wrapper loads root and identity model route" "$(tail -1 "$WORK/codex.err")"
fi
if bash "$APP/deploy/assistant-service.sh" "$APP" observer web >/dev/null 2>"$WORK/web.err"; then
    ok "Web service wrapper loads root and identity model route"
else
    bad "Web service wrapper loads root and identity model route" "$(tail -1 "$WORK/web.err")"
fi
check "Codex wrapper invokes continuous public command" \
    grep -q '^--identity observer run-codex-bridge|openai|deepseek-test-route|identity-secret$' "$SMOKE_RECORD"
check "Web wrapper invokes continuous public command" \
    grep -q '^--identity observer run-web-bridge|openai|deepseek-test-route|identity-secret$' "$SMOKE_RECORD"
check "Codex failure restarts only its supervised component" \
    grep -q '^Restart=on-failure$' "$REPO/deploy/headlong-assistant-codex@.service"
check "Web failure restarts only its supervised component" \
    grep -q '^Restart=on-failure$' "$REPO/deploy/headlong-assistant-web@.service"
check "bridge units do not place durable state in RuntimeDirectory" \
    bash -c '! grep -q ^RuntimeDirectory= "$1" "$2"' _ \
        "$REPO/deploy/headlong-assistant-codex@.service" \
        "$REPO/deploy/headlong-assistant-web@.service"

for unit in headlong-thinkers@.service headlong-assistant-codex@.service headlong-assistant-web@.service; do
    sed "s|@SHELLM_HOME@|$WORK/deploy-home|g" "$REPO/deploy/$unit" >"$WORK/systemd/$unit"
done
cp "$REPO/deploy/headlong-assistant@.target" "$WORK/systemd/"
if command -v systemd-analyze >/dev/null 2>&1; then
    if systemd-analyze verify "$WORK/systemd"/* >"$WORK/verify.out" 2>&1; then
        ok "rendered Personal Assistant units pass systemd-analyze verify"
    else
        bad "rendered Personal Assistant units pass systemd-analyze verify" "$(tail -3 "$WORK/verify.out" | tr '\n' ' ')"
    fi
else
    ok "systemd-analyze unavailable; static service assertions completed"
fi

check "setup installs source units and target" \
    grep -q 'headlong-assistant-codex@ headlong-assistant-web@' "$REPO/deploy/setup.sh"
check "update restarts only active source bridge instances" \
    grep -q "try-restart 'headlong-assistant-codex@\*.service'" "$REPO/deploy/update.sh"
check "assistant uninstall removes units without deleting identity state" \
    bash -c 'grep -q headlong-assistant-codex@.service "$1" && ! grep -q "rm .*identit" "$1"' \
        _ "$REPO/deploy/uninstall-assistant-services.sh"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
