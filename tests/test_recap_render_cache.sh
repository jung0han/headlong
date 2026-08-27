#!/usr/bin/env bash
# test_recap_render_cache.sh — the persistent render cache behind --context
#
# Usage: tests/test_recap_render_cache.sh
#
# Rendering the raw trajectory into the filtered TSV is the dominant
# --context cost on a long life (jq parses every byte of the log, most of
# which it discards), and the original build re-rendered from scratch on
# every call. These tests pin the cache: the TSV persists next to the
# blocks, later calls render only lines past the recorded high-water mark,
# stale rows from a crashed append are dropped, and a version/keep-set/
# truncation mismatch falls back to a full rebuild. Output must be
# byte-identical with and without a warm cache.
#
# The `llm` CLI is stubbed (canned rollup JSON, calls logged): no network.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s%s\n' "$1" "${2:+ — $2}"; }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
check_not() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$label"; else ok "$label"; fi; }

# --- stub llm: every call logged, canned rollup JSON back -------------------
mkdir -p "$WORK/bin"
cat > "$WORK/bin/llm" <<'EOF'
#!/usr/bin/env bash
input=$(cat)
printf '%s\n' "$*" >> "$LLM_ARG_LOG"
printf 'CALL\n%s\n---\n' "$input" >> "$LLM_LOG"
n=$(printf '%s' "$input" | wc -l | tr -d ' ')
printf '{"summary":"rollup of %s lines","themes":["testing"],"step_ids":["st000001"]}' "$n"
EOF
chmod +x "$WORK/bin/llm"
export PATH="$WORK/bin:$REPO/bin:$PATH"
export LLM_LOG="$WORK/llm.log"
export LLM_ARG_LOG="$WORK/llm-args.log"
unset TRAJ_DIR TRAJ_ID RECAP_MODEL SHELLM_FAST_MODEL SHELLM_MODEL 2>/dev/null || true

TRAJ_ROOT="$WORK/trajectories"

mk_traj() { # mk_traj <run-name> <n-signal-steps>
    local run="$TRAJ_ROOT/$1" i
    mkdir -p "$run"
    printf '{"type":"trajectory","step_id":"%s-0000-4000-8000-000000000000","ts":"t0"}\n' \
        "${1%%-*}" > "$run/trajectory.jsonl"
    for (( i = 1; i <= $2; i++ )); do
        printf '{"type":"thought","step_id":"st%06d","ts":"2026-07-17T10:%02d:00","source":"tester","content":"thinking about topic %d"}\n' \
            "$i" $((i % 60)) "$i" >> "$run/trajectory.jsonl"
    done
}

add_steps() { # add_steps <run-name> <from> <to> — append signal steps, plus one noise line
    local run="$TRAJ_ROOT/$1" i
    printf '{"type":"reasoning","step_id":"nz%06d","ts":"2026-07-17T11:00:00","content":"noise between appends"}\n' \
        "$2" >> "$run/trajectory.jsonl"
    for (( i = $2; i <= $3; i++ )); do
        printf '{"type":"thought","step_id":"st%06d","ts":"2026-07-17T10:%02d:00","source":"tester","content":"thinking about topic %d"}\n' \
            "$i" $((i % 60)) "$i" >> "$run/trajectory.jsonl"
    done
}

RUN="feed1111-root"
ROLLUPS="$TRAJ_ROOT/$RUN/rollups"
TSV="$ROLLUPS/rendered.tsv"
RMETA="$ROLLUPS/rendered.meta.json"

# ---------------------------------------------------------------------------
# 1. First --context call writes the cache: TSV rows = signal steps, meta
#    records the raw line count and format version.
# ---------------------------------------------------------------------------
mk_traj "$RUN" 25
recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 >/dev/null 2>&1
rc=$?
check "first: exits 0"              test "$rc" -eq 0
check "first: rendered.tsv exists"  test -f "$TSV"
check "first: meta exists"          test -f "$RMETA"
check "first: 25 signal rows"       test "$(wc -l < "$TSV" | tr -d ' ')" = "25"
check "first: meta raw_lines = 26"  test "$(jq -r .raw_lines "$RMETA")" = "26"

# ---------------------------------------------------------------------------
# 2. Incremental append: plant a sentinel in an early cached row, append new
#    steps (plus a noise line), rerun. The sentinel surviving proves the old
#    rows were NOT re-rendered; the new rows still appear, in order, with no
#    duplicate raw-line numbers, and the tail shows the newest step.
# ---------------------------------------------------------------------------
sed 's/thinking about topic 3/& CACHE-SENTINEL/' "$TSV" > "$TSV.sed" && mv "$TSV.sed" "$TSV"
add_steps "$RUN" 26 40
out2=$(recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 2>&1)
rc=$?
check "rollups use their structured schema" grep -q -- '--structured-result recap_rollup {' "$LLM_ARG_LOG"
check "append: exits 0"             test "$rc" -eq 0
check "append: old rows kept"       grep -q 'CACHE-SENTINEL' "$TSV"
check "append: 40 signal rows"      test "$(wc -l < "$TSV" | tr -d ' ')" = "40"
check "append: newest row present"  grep -q 'topic 40' "$TSV"
check "append: newest step in tail" grep -q 'topic 40' <<<"$out2"
check "append: no duplicate lines"  test -z "$(cut -f1 "$TSV" | sort | uniq -d)"
check "append: meta advanced to 42" test "$(jq -r .raw_lines "$RMETA")" = "42"

# ---------------------------------------------------------------------------
# 3. Crash leftover: rows above the recorded high-water mark (an append that
#    died before its meta write) are dropped before the next append, so
#    nothing duplicates.
# ---------------------------------------------------------------------------
jq '.raw_lines = 41' "$RMETA" > "$RMETA.tmp" && mv "$RMETA.tmp" "$RMETA"   # pretend the last row was never recorded
recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 >/dev/null 2>&1
rc=$?
check "crash: exits 0"              test "$rc" -eq 0
check "crash: still 40 signal rows" test "$(wc -l < "$TSV" | tr -d ' ')" = "40"
check "crash: no duplicate lines"   test -z "$(cut -f1 "$TSV" | sort | uniq -d)"
check "crash: old rows kept"        grep -q 'CACHE-SENTINEL' "$TSV"

# ---------------------------------------------------------------------------
# 4. Version mismatch: a stale format version forces a full rebuild (the
#    sentinel vanishes), and the row count is right afterwards.
# ---------------------------------------------------------------------------
jq '.version = 0' "$RMETA" > "$RMETA.tmp" && mv "$RMETA.tmp" "$RMETA"
recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 >/dev/null 2>&1
rc=$?
check "version: exits 0"            test "$rc" -eq 0
check_not "version: cache rebuilt"  grep -q 'CACHE-SENTINEL' "$TSV"
check "version: 40 signal rows"     test "$(wc -l < "$TSV" | tr -d ' ')" = "40"

# ---------------------------------------------------------------------------
# 5. Shrunken log (recorded lines > file lines — append-only says this
#    cannot happen): full rebuild rather than a corrupt incremental pass.
# ---------------------------------------------------------------------------
sed 's/thinking about topic 5/& CACHE-SENTINEL/' "$TSV" > "$TSV.sed" && mv "$TSV.sed" "$TSV"
jq '.raw_lines = 99999' "$RMETA" > "$RMETA.tmp" && mv "$RMETA.tmp" "$RMETA"
recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 >/dev/null 2>&1
rc=$?
check "shrink: exits 0"             test "$rc" -eq 0
check_not "shrink: cache rebuilt"   grep -q 'CACHE-SENTINEL' "$TSV"
check "shrink: 40 signal rows"      test "$(wc -l < "$TSV" | tr -d ' ')" = "40"

# ---------------------------------------------------------------------------
# 6. Lock discipline: a fresh lock (live builder) blocks a second run; a
#    stale one (>30min, dead builder) is broken and the run proceeds. A live
#    --backfill stays fresh via the per-seal keepalive touch, so only a
#    genuinely dead builder ever looks stale.
# ---------------------------------------------------------------------------
mkdir -p "$ROLLUPS/.lock"
out6=$(recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 2>&1)
rc=$?
check "lock: fresh lock blocks"     test "$rc" -ne 0
check "lock: says in progress"      grep -q 'in progress' <<<"$out6"
touch -t 202601010000 "$ROLLUPS/.lock"
recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 >/dev/null 2>&1
rc=$?
check "lock: stale lock broken"     test "$rc" -eq 0
check_not "lock: released after"    test -d "$ROLLUPS/.lock"

# ---------------------------------------------------------------------------
# 7. Equivalence: the staircase a warm cache produces is byte-identical to a
#    cold full render of the same trajectory.
# ---------------------------------------------------------------------------
warm=$(recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 2>&1)
rm -f "$TSV" "$RMETA"
cold=$(recap feed1111 --traj_dir "$TRAJ_ROOT" --context --raw-tail 5 2>&1)
check "equiv: warm == cold output"  test "$warm" = "$cold"

# ---------------------------------------------------------------------------
# 8. shellm-run rows render as wakeup markers, not launcher argv. The argv is
#    ~500 identical chars per wakeup (40% of the raw tail on a live mind); the
#    meaning is in launched_by/wake. A run the mind started itself has no
#    launched_by, and there the command IS the payload, so its tail is kept.
#    Row COUNT must not change: rollup blocks index rows positionally, so
#    dropping a row type would silently misalign every sealed block.
# ---------------------------------------------------------------------------
RUN2="cafe2222-root"
mkdir -p "$TRAJ_ROOT/$RUN2"
J2="$TRAJ_ROOT/$RUN2/trajectory.jsonl"
ARGV='shellm --model m/1 --traj cafe2222 --env id --workdir /w --var A=1 --var B=2 --var OPENROUTER_API_KEY=sk-secret'
{
  printf '{"type":"trajectory","step_id":"cafe2222-0000-4000-8000-000000000000","ts":"t0"}\n'
  printf '{"type":"thought","step_id":"st000001","ts":"2026-08-19T07:00:01","source":"monolith","content":"unchanged row"}\n'
  printf '{"type":"shellm-run","step_id":"st000002","ts":"2026-08-19T07:00:02","model":"m/1","launched_by":"monolith","wake":"spontaneous/timer","command":"%s"}\n' "$ARGV"
  printf '{"type":"shellm-run","step_id":"st000003","ts":"2026-08-19T07:00:03","model":"m/1","launched_by":"mind_wanderer","command":"%s"}\n' "$ARGV"
  jq -nc --arg c 'shellm --model m/1 --workdir /w "summarize the THREE study"' \
     '{type:"shellm-run",step_id:"st000004",ts:"2026-08-19T07:00:04",model:"m/1",command:$c}'
} > "$J2"
out8=$(recap cafe2222 --traj_dir "$TRAJ_ROOT" --context --raw-tail 50 2>&1)
TSV2="$TRAJ_ROOT/$RUN2/rollups/rendered.tsv"

check "run row: names the thinker"   grep -q 'shellm-run(monolith): wake spontaneous/timer, model m/1' <<<"$out8"
check "run row: wake omitted if absent" grep -q 'shellm-run(mind_wanderer): model m/1' <<<"$out8"
check_not "run row: no launcher flags" grep -q -- '--var' <<<"$out8"
check_not "run row: no key in prompt"  grep -q 'sk-secret' <<<"$out8"
check "run row: self-launched keeps cmd" grep -q 'summarize the THREE study' <<<"$out8"
check "run row: row count unchanged"   test "$(wc -l < "$TSV2" | tr -d ' ')" = "4"
check "run row: compact (<160 chars)"  test "$(awk -F"\t" '$2=="st000002"{print length($4)}' "$TSV2")" -lt 160

printf '\n%s passed, %s failed\n' "$pass" "$fail"
exit $(( fail > 0 ))
