#!/usr/bin/env bash
# DONGWOO-906: explicit LiteLLM/OpenAI-compatible routing and fail-fast health.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
WORK=$(mktemp -d)
server_pid=""
trap '[[ -z "$server_pid" ]] || kill "$server_pid" 2>/dev/null || true; cd /; rm -rf "$WORK"' EXIT

pass=0
fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s%s\n' "$1" "${2:+ — $2}"; }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
check_not() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$label"; else ok "$label"; fi; }

APP="$WORK/app"
mkdir -p "$APP"
ln -s "$REPO/bin" "$APP/bin"
ln -s "$REPO/tools" "$APP/tools"
ln -s "$REPO/thinkers" "$APP/thinkers"
ln -s "$REPO/skills" "$APP/skills"
ln -s "$REPO/deploy" "$APP/deploy"

# A real temporary identity is the product boundary. No model-name heuristic
# can classify this private catalog id; only explicit LLM_PROVIDER=openai can
# route it through the fake LiteLLM service.
export HOME="$WORK/home"
export HEADLONG_HOME="$WORK/state"
export PATH="$APP/bin:$APP/tools:$PATH"
mkdir -p "$HOME" "$HEADLONG_HOME"
cd "$APP" || exit 1
IDENTITY_DIR="$APP/.identities" identity new observer >/dev/null 2>&1 \
    || { bad "temporary Observer Identity created"; exit 1; }
check "temporary Observer Identity created" test -f "$APP/.identities/observer/activate"

cat >"$WORK/fake_litellm.py" <<'PY'
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port_file, calls_file, mode_file = sys.argv[1:]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        auth = self.headers.get("authorization", "")
        with open(calls_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": self.path, "auth": auth, "body": body}) + "\n")

        mode = open(mode_file, encoding="utf-8").read().strip()
        if mode == "fail":
            payload = json.dumps({"error": {"message": f"route rejected credential {auth}"}}).encode()
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        messages = body.get("messages", [])
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        if "ROUTE_ENV_CHECK" in prompt:
            content = """```bash
[[ "$LLM_PROVIDER" == "openai" ]]
[[ "$SHELLM_MODEL" == "deepseek-flash-v4-private" ]]
[[ "$LLM_API_URL" == http://127.0.0.1:* ]]
[[ -n "$OPENAI_API_KEY" ]]
FINAL=route-env-ok
```"""
        else:
            content = "route-ok"

        if body.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            payload = data.encode()
            content_type = "text/event-stream"
        else:
            payload = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(port_file, "w", encoding="utf-8") as fh:
    fh.write(str(server.server_port))
server.serve_forever()
PY

: >"$WORK/calls.jsonl"
printf 'ok\n' >"$WORK/mode"
python3 "$WORK/fake_litellm.py" "$WORK/port" "$WORK/calls.jsonl" "$WORK/mode" &
server_pid=$!
for _ in $(seq 1 100); do [[ -s "$WORK/port" ]] && break; sleep 0.02; done
PORT=$(cat "$WORK/port")

export LLM_PROVIDER=openai
export LLM_API_URL="http://127.0.0.1:$PORT/v1/chat/completions"
export SHELLM_MODEL=deepseek-flash-v4-private
export OPENAI_API_KEY="probe-secret-$RANDOM-$RANDOM"
export LLM_STRUCTURED_OUTPUT_MODE=strict

# shellcheck disable=SC1091
source "$APP/.identities/observer/activate"

if headlong-model-probe >"$WORK/probe.out" 2>"$WORK/probe.err"; then
    ok "Observer completes direct and shellm model probes"
else
    bad "Observer completes direct and shellm model probes" "$(tail -3 "$WORK/probe.err")"
fi

HEALTH="$IDENTITY_DIR/run/model_route_health.json"
check "health records both successful public paths" \
    jq -e '.ok == true and .paths.direct.status == "ok" and .paths.shellm.status == "ok"' "$HEALTH"
check "health names explicit route references without values" \
    jq -e '.route.provider == "openai" and .route.model == "deepseek-flash-v4-private" and .route.endpoint_ref == "LLM_API_URL" and .route.credential_ref == "OPENAI_API_KEY" and .route.structured_results == {"mode":"strict","source":"configured"}' "$HEALTH"
check_not "health contains no credential value" grep -qF "$OPENAI_API_KEY" "$HEALTH"
check_not "probe output contains no credential value" grep -qF "$OPENAI_API_KEY" "$WORK/probe.out" "$WORK/probe.err"

check "fake LiteLLM saw both route calls" test "$(wc -l <"$WORK/calls.jsonl" | tr -d ' ')" -eq 2
check "both calls used the arbitrary DeepSeek model" \
    jq -se 'length == 2 and all(.[]; .body.model == "deepseek-flash-v4-private")' "$WORK/calls.jsonl"
check "both calls used the same OpenAI credential reference" \
    jq -se --arg auth "Bearer $OPENAI_API_KEY" 'length == 2 and all(.[]; .auth == $auth)' "$WORK/calls.jsonl"
check "shellm path is distinguishable at the fake provider" \
    jq -se 'any(.[]; any(.body.messages[]; (.content // "") | contains("shellm model-route")))' "$WORK/calls.jsonl"

# A normal shellm turn executes generated code with the same explicit route.
# This covers nested local calls; Docker mode consumes the same env_vars array
# through name-only `docker exec -e NAME` flags.
mkdir -p "$WORK/actor"
out=$(SHELLM_ENV=local shellm --quiet --workdir "$WORK/actor" --max-iterations 1 ROUTE_ENV_CHECK 2>"$WORK/actor.err")
if [[ $? -eq 0 && "$out" == route-env-ok ]]; then
    ok "nested actor environment receives provider, endpoint, model, and credential"
else
    bad "nested actor environment receives provider, endpoint, model, and credential" "out=$out err=$(tail -2 "$WORK/actor.err")"
fi

# The systemd start wrapper must fail before it forks a persistent dispatcher.
SECRET="$OPENAI_API_KEY"
printf 'fail\n' >"$WORK/mode"
cat >"$APP/.env" <<EOF
LLM_PROVIDER=openai
LLM_API_URL=http://127.0.0.1:$PORT/v1/chat/completions
SHELLM_MODEL=deepseek-flash-v4-private
OPENAI_API_KEY=$SECRET
EOF
unset LLM_PROVIDER LLM_API_URL SHELLM_MODEL OPENAI_API_KEY
rm -f "$IDENTITY_DIR/run/dispatcher.pid"
HEADLONG_HOME="$HEADLONG_HOME" bash "$APP/deploy/thinkers-service.sh" "$APP" observer start \
    >"$WORK/start.out" 2>"$WORK/start.err"
rc=$?
check "failed route probe stops service startup" test "$rc" -ne 0
check_not "failed route never enters persistent loop" test -e "$IDENTITY_DIR/run/dispatcher.pid"
check "failed startup writes actionable route health" \
    jq -e '.ok == false and .paths.direct.status == "failed" and .paths.shellm.status == "failed" and .route.provider == "openai"' "$HEALTH"
check_not "failed health contains no credential value" grep -qF "$SECRET" "$HEALTH" "$HEADLONG_HOME/run/llm_health.json"
check_not "failed startup logs contain no credential value" grep -qF "$SECRET" "$WORK/start.out" "$WORK/start.err"
check "failed startup points to health result" grep -q 'health:' "$WORK/start.err"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
