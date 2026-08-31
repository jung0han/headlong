#!/usr/bin/env bash
# shellm run summaries use Structured Model Results and never append bad steps.

set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
pass=0 fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s%s\n' "$1" "${2:+ — $2}"; }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
check_not() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$label"; else ok "$label"; fi; }

cp -R "$REPO/bin" "$WORK/bin"
cp "$REPO/tools/headlong-run-summary" "$WORK/bin/"
cat > "$WORK/bin/llm" <<'EOF'
#!/usr/bin/env bash
for arg in "$@"; do
    [[ "$arg" == --thinking ]] && main=1
    [[ "$arg" == --structured-result ]] && summary=1
done
if [[ "${main:-0}" -eq 1 ]]; then
    printf '```bash\nFINAL=done\n```\n'
elif [[ "${summary:-0}" -eq 1 ]]; then
    printf '%s\n' "$*" >> "$SUMMARY_ARGS"
    [[ "${SUMMARY_FAIL:-0}" -eq 1 ]] && { printf 'schema failure\n' >&2; exit 1; }
    printf '{"tldr":"inspect the requested files","full_summary":null}\n'
else
    printf '{}\n'
fi
EOF
chmod +x "$WORK/bin/llm"
export PATH="$WORK/bin:$PATH" HOME="$WORK/home" HEADLONG_HOME="$WORK/home/.headlong"
export SHELLM_MODEL=test-model ANTHROPIC_API_KEY=test-key SHELLM_ENV=local
export SUMMARY_ARGS="$WORK/summary-args"

run_shellm() {
    local name="$1"
    mkdir -p "$WORK/$name"
    (cd "$WORK/$name" && "$WORK/bin/shellm" --max-iterations 1 "$name") >"$WORK/$name.out" 2>"$WORK/$name.err"
}

run_shellm success
SUCCESS_TRAJ=$(find "$HEADLONG_HOME/trajectories" -name trajectory.jsonl | head -1)
check "successful summary appends trajectory step" jq -e -s 'any(.[]; .type == "run-summary" and .tldr == "inspect the requested files" and .full_summary == "")' "$SUCCESS_TRAJ"
check "run summary uses its structured schema" grep -q -- '--structured-result shellm_run_summary {' "$SUMMARY_ARGS"

rm -rf "$HEADLONG_HOME/trajectories"
SUMMARY_FAIL=1 run_shellm failure
FAILED_TRAJ=$(find "$HEADLONG_HOME/trajectories" -name trajectory.jsonl | head -1)
check_not "failed summary appends no run-summary step" jq -e 'select(.type == "run-summary")' "$FAILED_TRAJ"
check "failed summary is observable" grep -q 'run summary generation failed' "$WORK/failure.err"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
