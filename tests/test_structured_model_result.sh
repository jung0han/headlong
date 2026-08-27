#!/usr/bin/env bash
# Shared Structured Model Result contract at the public llm route.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

pass=0 fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s%s\n' "$1" "${2:+ — $2}"; }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
check_not() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$label"; else ok "$label"; fi; }

cat > "$WORK/schema.json" <<'EOF'
{"type":"object","additionalProperties":false,"properties":{"answer":{"type":"string","minLength":1}},"required":["answer"]}
EOF

# curl returns queued OpenAI-compatible responses and records request bodies.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<'EOF'
#!/usr/bin/env bash
out="" data=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -d) data="${2#@}"; shift 2 ;;
        -w) shift 2 ;;
        *) shift ;;
    esac
done
cp "$data" "$CALLS/$(printf '%03d' "$(( $(find "$CALLS" -type f | wc -l) + 1 ))").json"
response=$(head -1 "$RESPONSES")
sed '1d' "$RESPONSES" > "$RESPONSES.tmp" && mv "$RESPONSES.tmp" "$RESPONSES"
printf '{"choices":[{"message":{"content":%s},"finish_reason":"stop"}],"usage":{}}' "$(printf '%s' "$response" | jq -Rs .)" > "$out"
printf 200
EOF
chmod +x "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH" CALLS="$WORK/calls" RESPONSES="$WORK/responses"
export HOME="$WORK/home" HEADLONG_HOME="$WORK/home/.headlong"
export LLM_PROVIDER=openai OPENAI_API_KEY=test-key SHELLM_MODEL=test-model
export LLM_RETRIES=0
mkdir -p "$CALLS"

run_result() {
    printf 'give an answer' | "$REPO/bin/llm" --structured-result test_answer "$(cat "$WORK/schema.json")" \
        --no-stream -m test-model -t 100 -s 'Answer briefly.'
}

printf '%s\n' '{"answer":"strict result"}' > "$RESPONSES"
out=$(LLM_STRUCTURED_OUTPUT_MODE=strict run_result 2>"$WORK/strict.err")
check "strict result succeeds" test "$out" = '{"answer":"strict result"}'
check "strict route sends schema" jq -e '.response_format.type == "json_schema" and .response_format.json_schema.name == "test_answer"' "$CALLS/001.json"
check "strict route calls once" test "$(find "$CALLS" -type f | wc -l | tr -d ' ')" = 1

rm -f "$CALLS"/*
printf '%s\n' '{"wrong":"first"}' '{"answer":"fallback result"}' > "$RESPONSES"
out=$(LLM_STRUCTURED_OUTPUT_MODE=json_object run_result 2>"$WORK/fallback.err")
check "fallback recovers after invalid object" test "$out" = '{"answer":"fallback result"}'
check "fallback retries once" test "$(find "$CALLS" -type f | wc -l | tr -d ' ')" = 2
check "fallback route sends object mode" bash -c 'jq -e '\''.response_format.type == "json_object"'\'' "$1" && jq -e '\''.response_format.type == "json_object"'\'' "$2"' _ "$CALLS/001.json" "$CALLS/002.json"

rm -f "$CALLS"/*
printf '%s\n' '{"wrong":"first"}' '{"answer":"ok","unknown":true}' > "$RESPONSES"
LLM_STRUCTURED_OUTPUT_MODE=json_object run_result >"$WORK/invalid.out" 2>"$WORK/invalid.err"
check_not "two invalid objects fail" test $? -eq 0
check "invalid objects are not emitted" test ! -s "$WORK/invalid.out"
check "schema failure is observable" grep -q 'did not match schema' "$WORK/invalid.err"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
