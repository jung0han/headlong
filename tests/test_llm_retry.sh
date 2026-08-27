#!/usr/bin/env bash
# test_llm_retry.sh — transient-retry behavior of bin/llm
#
# Usage: tests/test_llm_retry.sh
#
# curl is stubbed (mode file drives per-attempt behavior; every call is
# counted), so this exercises the real retry loops: retry on pre-output
# provider errors, no retry after partial streamed output, no retry on 4xx,
# LLM_RETRIES=0 opting out.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/tmp"
export TMPDIR="$WORK/tmp"

pass=0
fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s%s\n' "$1" "${2:+ — $2}"; }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
check_not() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$label"; else ok "$label"; fi; }

# --- curl stub ---------------------------------------------------------------
# $CURL_MODE_FILE holds one mode per line; line N applies to call N (last
# line repeats). Modes: err-body | sse-ok | sse-midstream-fail |
# sse-finish-length | http-500 | http-400 | http-200 | finish-error |
# finish-length
mkdir -p "$WORK/bin"
cat > "$WORK/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "$CURL_ARGS_FILE"
n=$(( $(cat "$CURL_COUNT" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$n" > "$CURL_COUNT"
mode=$(sed -n "${n}p" "$CURL_MODE_FILE")
[[ -z "$mode" ]] && mode=$(tail -1 "$CURL_MODE_FILE")

# detect non-streaming invocation (-o <file> present)
out_file=""
prev=""
for a in "$@"; do
    [[ "$prev" == "-o" ]] && out_file="$a"
    if [[ "$prev" == "--config" ]]; then
        cp "$a" "$CURL_CONFIG_COPY"
        stat -c '%a' "$a" > "$CURL_CONFIG_MODE" 2>/dev/null \
            || stat -f '%Lp' "$a" > "$CURL_CONFIG_MODE"
    fi
    prev="$a"
done

case "$mode" in
    err-body)   # HTTP 200 with an error JSON body, no SSE data (OpenRouter style)
        if [[ -n "$out_file" ]]; then
            printf '{"error":{"message":"Provider returned error","code":502}}' > "$out_file"
            printf '200'
        else
            printf '{"error":{"message":"Provider returned error","code":502}}\n'
        fi
        ;;
    sse-ok)
        printf 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n'
        ;;
    sse-ok-usage)   # streamed success with the trailing usage chunk
        printf 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        printf 'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":7,"completion_tokens_details":{"reasoning_tokens":3}}}\n\ndata: [DONE]\n'
        ;;
    http-200-usage)
        printf '{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":50,"completion_tokens":4}}' > "$out_file"
        printf '200'
        ;;
    ws-body)    # HTTP 200 whose body is only whitespace keep-alive padding
        if [[ -n "$out_file" ]]; then
            printf '   \n\n  \n' > "$out_file"
            printf '200'
        else
            printf '   \n\n  \n'
        fi
        ;;
    empty-body) # HTTP 200 with a zero-byte body
        if [[ -n "$out_file" ]]; then
            : > "$out_file"
            printf '200'
        fi
        ;;
    sse-midstream-fail)
        printf 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        echo "curl: (18) transfer closed with outstanding read data remaining" >&2
        exit 18
        ;;
    http-500)
        printf '{"error":{"message":"upstream exploded"}}' > "$out_file"
        printf '500'
        ;;
    http-402)   # OpenRouter-style insufficient credits
        printf '{"error":{"message":"Insufficient credits. Add more using https://openrouter.ai/settings/credits","code":402}}' > "$out_file"
        printf '402'
        ;;
    http-401)
        printf '{"error":{"message":"No auth credentials found","code":401}}' > "$out_file"
        printf '401'
        ;;
    http-429-quota)   # Gemini-style: rate limit whose message says "quota"
        printf '{"error":{"message":"You exceeded your current quota, please check your plan and billing details.","code":429}}' > "$out_file"
        printf '429'
        ;;
    err-body-credit)   # streaming: HTTP 200 whose body is a credit error, no SSE data
        printf '{"error":{"message":"Insufficient credits. Add more using https://openrouter.ai/settings/credits","code":402}}\n'
        ;;
    http-400)
        printf '{"error":{"message":"bad request"}}' > "$out_file"
        printf '400'
        ;;
    http-200)
        printf '{"choices":[{"message":{"content":"ok"}}]}' > "$out_file"
        printf '200'
        ;;
    finish-error)   # HTTP 200, partial text, provider died mid-generation
        printf '{"choices":[{"message":{"content":"partial judgm"},"finish_reason":"error"}]}' > "$out_file"
        printf '200'
        ;;
    finish-length)  # HTTP 200, output capped by max_tokens
        printf '{"choices":[{"message":{"content":"truncated tex"},"finish_reason":"length"}]}' > "$out_file"
        printf '200'
        ;;
    sse-finish-length)  # streamed output whose final chunk reports the cap
        printf 'data: {"choices":[{"delta":{"content":"truncated te"},"finish_reason":null}]}\n\n'
        printf 'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'
        printf 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
        printf 'data: [DONE]\n'
        ;;
esac
EOF
chmod +x "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH"
export OPENROUTER_API_KEY="test-key"
export HEADLONG_HOME="$WORK/home"   # bin/llm writes run/llm_health.json here
mkdir -p "$HEADLONG_HOME"
export CURL_COUNT="$WORK/count" CURL_MODE_FILE="$WORK/modes"
export CURL_ARGS_FILE="$WORK/curl-args" CURL_CONFIG_COPY="$WORK/curl-config"
export CURL_CONFIG_MODE="$WORK/curl-config-mode"
export LLM_RETRY_BACKOFF=0

LLM="$REPO/bin/llm"
MODEL="openai/gpt-oss-120b"

run_llm() { "$LLM" -m "$MODEL" "$@" "say ok" 2>"$WORK/stderr"; }
calls() { cat "$CURL_COUNT"; }
reset() { printf '0' > "$CURL_COUNT"; printf '%s\n' "$1" > "$CURL_MODE_FILE"; }

# ---------------------------------------------------------------------------
# Streaming: two provider errors then success -> retried, output intact
# ---------------------------------------------------------------------------

reset $'err-body\nerr-body\nsse-ok'
out=$(LLM_RETRIES=2 run_llm)
check "stream retry succeeds"      test "$?" -eq 0
check "stream output correct"      test "$out" = "ok"
check "three curl calls"           test "$(calls)" = "3"
check "retry noted on stderr"      grep -q "transient API failure (attempt 1/3)" "$WORK/stderr"
check_not "provider key stays out of curl argv" grep -qF "$OPENROUTER_API_KEY" "$CURL_ARGS_FILE"
check "provider key reaches curl through its config" grep -qF "Authorization: Bearer $OPENROUTER_API_KEY" "$CURL_CONFIG_COPY"
check "curl header config is owner-only" test "$(cat "$CURL_CONFIG_MODE")" = 600

# ---------------------------------------------------------------------------
# Streaming: persistent failure -> exhausts retries, fails with orig message
# ---------------------------------------------------------------------------

reset "err-body"
out=$(LLM_RETRIES=2 run_llm)
rc=$?
check "persistent failure fails"     test "$rc" -ne 0
check "no output on failure"         test -z "$out"
check "three attempts then give up"  test "$(calls)" = "3"
check "original error preserved"     grep -q "llm: error: API error: Provider returned error" "$WORK/stderr"

# ---------------------------------------------------------------------------
# LLM_RETRIES=0 restores single-shot behavior
# ---------------------------------------------------------------------------

reset "err-body"
LLM_RETRIES=0 run_llm >/dev/null
check_not "retries=0 fails immediately" test "$(calls)" != "1"
check "single call with retries=0"      test "$(calls)" = "1"

# ---------------------------------------------------------------------------
# Mid-stream failure after output -> NO retry (would duplicate text)
# ---------------------------------------------------------------------------

reset "sse-midstream-fail"
out=$(LLM_RETRIES=2 run_llm)
rc=$?
check "midstream failure is fatal"    test "$rc" -ne 0
check "partial output delivered"      test "$out" = "partial"
check "no retry after emission"       test "$(calls)" = "1"

# ---------------------------------------------------------------------------
# Non-streaming: 5xx retried to success; 400 not retried
# ---------------------------------------------------------------------------

reset $'http-500\nhttp-200'
out=$(LLM_RETRIES=2 run_llm --no-stream)
check "non-stream retry succeeds"     test "$out" = "ok"
check "two calls (500 then 200)"      test "$(calls)" = "2"

reset "http-400"
LLM_RETRIES=2 run_llm --no-stream >/dev/null
rc=$?
check "400 fails"                     test "$rc" -ne 0
check "400 not retried"               test "$(calls)" = "1"
check "400 message surfaced"          grep -q "bad request" "$WORK/stderr"

# --- health marker: failures and successes leave run/llm_health.json -----------
HF="$HEADLONG_HOME/run/llm_health.json"
check "marker written on failure"     test -s "$HF"
check "marker: ok=false, http 400"    bash -c 'jq -e ".ok == false and .http_code == 400 and .provider == \"openrouter\"" "$1" >/dev/null' _ "$HF"
check "marker: 400 classified other"  bash -c 'jq -e ".kind == \"other\"" "$1" >/dev/null' _ "$HF"
reset "http-402"
LLM_RETRIES=2 run_llm --no-stream >/dev/null
check "402 fails"                     test "$?" -ne 0
check "marker: 402 -> kind credit"    bash -c 'jq -e ".ok == false and .kind == \"credit\" and .http_code == 402" "$1" >/dev/null' _ "$HF"
check "marker: message kept"          bash -c 'jq -e ".message | test(\"credit\")" "$1" >/dev/null' _ "$HF"
reset "http-401"
LLM_RETRIES=2 run_llm --no-stream >/dev/null
check "marker: 401 -> kind auth"      bash -c 'jq -e ".kind == \"auth\"" "$1" >/dev/null' _ "$HF"
reset "http-429-quota"
LLM_RETRIES=0 run_llm --no-stream >/dev/null
check "marker: 429 'quota' -> kind rate (code beats words)" bash -c 'jq -e ".kind == \"rate\" and .http_code == 429" "$1" >/dev/null' _ "$HF"
reset "http-200"
out=$(LLM_RETRIES=2 run_llm --no-stream)
check "marker: success -> ok=true"    bash -c 'jq -e ".ok == true and (has(\"kind\") | not)" "$1" >/dev/null' _ "$HF"
reset "err-body-credit"
LLM_RETRIES=1 run_llm >/dev/null
check "marker: streamed credit error -> kind credit (no http code)" bash -c 'jq -e ".ok == false and .kind == \"credit\" and .http_code == null" "$1" >/dev/null' _ "$HF"
reset "sse-ok"
out=$(LLM_RETRIES=1 run_llm)
check "marker: streamed success -> ok=true" bash -c 'jq -e ".ok == true" "$1" >/dev/null' _ "$HF"

# ---------------------------------------------------------------------------
# Non-streaming: embedded failures inside a 200 body are not silent successes
# ---------------------------------------------------------------------------

reset $'err-body\nhttp-200'
out=$(LLM_RETRIES=2 run_llm --no-stream)
check "200-with-error retried"        test "$out" = "ok"
check "two calls (error then ok)"     test "$(calls)" = "2"

reset "finish-error"
out=$(LLM_RETRIES=1 run_llm --no-stream)
rc=$?
check "finish_reason error fails"     test "$rc" -ne 0
check "partial text not printed"      test -z "$out"
check "finish_reason error retried"   test "$(calls)" = "2"
check "finish_reason msg surfaced"    grep -q 'finish_reason "error"' "$WORK/stderr"

# ---------------------------------------------------------------------------
# Non-streaming: max_tokens truncation is delivered but warned about
# ---------------------------------------------------------------------------

reset "finish-length"
out=$(LLM_RETRIES=2 run_llm --no-stream)
rc=$?
check "truncated output succeeds"     test "$rc" -eq 0
check "truncated text delivered"      test "$out" = "truncated tex"
check "truncation not retried"        test "$(calls)" = "1"
check "truncation warned on stderr"   grep -q "output truncated at max_tokens" "$WORK/stderr"

# ---------------------------------------------------------------------------
# Streaming: max_tokens truncation is delivered but warned about
# ---------------------------------------------------------------------------

reset "sse-finish-length"
out=$(LLM_RETRIES=2 run_llm)
rc=$?
check "stream truncation succeeds"    test "$rc" -eq 0
check "stream truncated text intact"  test "$out" = "truncated tex"
check "stream truncation not retried" test "$(calls)" = "1"
check "stream truncation warned"      grep -q "output truncated at max_tokens" "$WORK/stderr"

# ---------------------------------------------------------------------------
# Whitespace-only 200 body (provider died, keep-alive padding) is an error,
# not an empty success — retried, and fatal when persistent
# ---------------------------------------------------------------------------

reset $'ws-body\nhttp-200'
out=$(LLM_RETRIES=2 run_llm --no-stream)
check "non-stream ws-body retried"    test "$out" = "ok"
check "two calls (ws then ok)"        test "$(calls)" = "2"

reset "ws-body"
out=$(LLM_RETRIES=1 run_llm --no-stream)
rc=$?
check "persistent ws-body fails"      test "$rc" -ne 0
check "ws-body no output"             test -z "$out"
check "ws-body msg surfaced"          grep -q "whitespace-only response body" "$WORK/stderr"

reset $'ws-body\nsse-ok'
out=$(LLM_RETRIES=2 run_llm)
check "stream ws-body retried"        test "$out" = "ok"
check "stream two calls (ws then ok)" test "$(calls)" = "2"

reset "ws-body"
out=$(LLM_RETRIES=1 run_llm)
rc=$?
check "persistent stream ws fails"    test "$rc" -ne 0
check "stream ws no output"           test -z "$out"
check "stream ws is an API error"     grep -q "llm: error: API error" "$WORK/stderr"

reset "empty-body"
out=$(LLM_RETRIES=1 run_llm)
rc=$?
check "empty stream body fails"       test "$rc" -ne 0
check "empty stream no output"        test -z "$out"
check "empty stream retried"          test "$(calls)" = "2"
check "empty stream msg surfaced"     grep -q "stream ended without emitting" "$WORK/stderr"

reset $'empty-body\nhttp-200'
out=$(LLM_RETRIES=2 run_llm --no-stream)
check "non-stream empty retried"      test "$out" = "ok"
check "empty is whitespace error"     test "$(calls)" = "2"

# ---------------------------------------------------------------------------
# Usage ledger: every successful call appends one line, with or without a
# caller-provided LLM_USAGE_FILE; failures append nothing
# ---------------------------------------------------------------------------

LEDGER="$HEADLONG_HOME/usage/llm.jsonl"
rm -f "$LEDGER"
reset "sse-ok-usage"
out=$(LLM_RETRIES=0 run_llm)
check "ledger: streamed call appends"   test "$(wc -l < "$LEDGER" 2>/dev/null | tr -d ' ')" = "1"
check "ledger: tokens + model + provider" bash -c 'tail -1 "$1" | jq -e ".in_tok == 120 and .out_tok == 7 and .think_tok == 3 and .model == \"openai/gpt-oss-120b\" and .provider == \"openrouter\" and (.ts | test(\"^20[0-9][0-9]-\")) and (has(\"identity\") | not)" >/dev/null' _ "$LEDGER"
check "ledger: own usage temp removed"  test -z "$(ls "${TMPDIR:-/tmp}"/llm-usage.* 2>/dev/null)"

reset "http-200-usage"
out=$(LLM_RETRIES=0 LLM_USAGE_FILE="$WORK/usage.json" IDENTITY_DIR="$WORK/ident" IDENTITY_NAME=ada run_llm --no-stream)
check "ledger: caller usage file still written" bash -c 'jq -e ".in_tok == 50 and .out_tok == 4" "$1" >/dev/null' _ "$WORK/usage.json"
check "ledger: identity dir gets its own ledger" test "$(wc -l < "$WORK/ident/usage/llm.jsonl" | tr -d ' ')" = "1"
check "ledger: identity stamped"        bash -c 'tail -1 "$1" | jq -e ".identity == \"ada\" and .in_tok == 50 and (has(\"run_id\") | not)" >/dev/null' _ "$WORK/ident/usage/llm.jsonl"
check "ledger: home ledger untouched"   test "$(wc -l < "$LEDGER" | tr -d ' ')" = "1"

reset "http-200-usage"
out=$(LLM_RETRIES=0 LLM_RUN_ID=run-77 run_llm --no-stream)
check "ledger: LLM_RUN_ID recorded"     bash -c 'tail -1 "$1" | jq -e ".run_id == \"run-77\"" >/dev/null' _ "$LEDGER"

reset "http-400"
LLM_RETRIES=0 run_llm --no-stream >/dev/null
check "ledger: failure appends nothing" test "$(wc -l < "$LEDGER" | tr -d ' ')" = "2"

reset "sse-ok"
out=$(LLM_RETRIES=0 run_llm)
check "ledger: no usage chunk -> no line" test "$(wc -l < "$LEDGER" | tr -d ' ')" = "2"

reset "http-200-usage"
out=$(LLM_RETRIES=0 LLM_USAGE_LEDGER=/dev/null run_llm --no-stream)
check "ledger: LLM_USAGE_LEDGER=/dev/null disables" test "$(wc -l < "$LEDGER" | tr -d ' ')" = "2"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
