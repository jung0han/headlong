#!/usr/bin/env bash
# test_mem_frontmatter.sh — tests for bin/mem frontmatter_field()
#
# frontmatter_field() extracts a YAML frontmatter value from a memory file.
# It should strip surrounding quotes so summary: "foo" returns foo, not "foo".
# This test documents that expected behavior and catches regressions if the
# quote-stripping sed is removed.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s%s\n' "$1" "${2:+ — $2}"; }

# Extract frontmatter_field() from bin/mem without running main.
# bin/mem calls main "$@" at the end, so sourcing it would exit.
eval "$(sed -n '/^frontmatter_field()/,/^}/p' "$REPO/bin/mem")"

# --- fixtures: quoted and unquoted frontmatter ---
mkdir -p "$WORK/mem"
cat > "$WORK/mem/quoted.md" <<'EOF'
---
summary: "A quoted summary"
importance: "high"
type: "lesson"
tags: ["t1", "t2"]
---

Body.
EOF

cat > "$WORK/mem/unquoted.md" <<'EOF'
---
summary: An unquoted summary
importance: medium
type: lesson
tags: [t1, t2]
---

Body.
EOF

# --- assertions: quoted values should be stripped ---
val=$(frontmatter_field "$WORK/mem/quoted.md" "summary")
[ "$val" = "A quoted summary" ] \
  && ok "quoted summary stripped" \
  || bad "quoted summary stripped" "got: [$val]"

val=$(frontmatter_field "$WORK/mem/quoted.md" "importance")
[ "$val" = "high" ] \
  && ok "quoted importance stripped" \
  || bad "quoted importance stripped" "got: [$val]"

val=$(frontmatter_field "$WORK/mem/quoted.md" "type")
[ "$val" = "lesson" ] \
  && ok "quoted type stripped" \
  || bad "quoted type stripped" "got: [$val]"

# --- assertions: unquoted values pass through unchanged ---
val=$(frontmatter_field "$WORK/mem/unquoted.md" "summary")
[ "$val" = "An unquoted summary" ] \
  && ok "unquoted summary unchanged" \
  || bad "unquoted summary unchanged" "got: [$val]"

val=$(frontmatter_field "$WORK/mem/unquoted.md" "importance")
[ "$val" = "medium" ] \
  && ok "unquoted importance unchanged" \
  || bad "unquoted importance unchanged" "got: [$val]"

# --- missing field returns empty ---
val=$(frontmatter_field "$WORK/mem/quoted.md" "nonexistent")
[ -z "$val" ] \
  && ok "missing field returns empty" \
  || bad "missing field returns empty" "got: [$val]"

# --- native learning context is written and survives edits ---
context_file=$(MEM_DIR="$WORK/contextual" \
  MEM_KNOWLEDGE_SCOPE="project:project-1" \
  MEM_EVIDENCE_LOCATORS='[{"kind":"codex_event","sha256":"abc"}]' \
  "$REPO/bin/mem" add --type decision "Keep this project decision.")
context_path="$WORK/contextual/$context_file.md"
scope=$(frontmatter_field "$context_path" "knowledge_scope")
evidence=$(frontmatter_field "$context_path" "evidence_locators")
if [[ "$scope" = "project:project-1" ]] \
    && [[ "$evidence" = '[{"kind":"codex_event","sha256":"abc"}]' ]]; then
  ok "contextual add preserves Knowledge Scope and evidence"
else
  bad "contextual add preserves Knowledge Scope and evidence" "scope=[$scope] evidence=[$evidence]"
fi

MEM_DIR="$WORK/contextual" "$REPO/bin/mem" edit "$context_file" \
  "Keep the revised project decision." >/dev/null 2>&1
context_path=$(find "$WORK/contextual" -name '*.md' -print -quit)
scope=$(frontmatter_field "$context_path" "knowledge_scope")
evidence=$(frontmatter_field "$context_path" "evidence_locators")
if [[ "$scope" = "project:project-1" ]] \
    && [[ "$evidence" = '[{"kind":"codex_event","sha256":"abc"}]' ]]; then
  ok "contextual edit preserves Knowledge Scope and evidence"
else
  bad "contextual edit preserves Knowledge Scope and evidence" "scope=[$scope] evidence=[$evidence]"
fi

# --- summaries never split a multibyte UTF-8 character ---
utf8_add=$(printf '가%.0s' {1..90})
utf8_file=$(LC_ALL=C.UTF-8 MEM_DIR="$WORK/utf8" \
  "$REPO/bin/mem" add "$utf8_add")
utf8_path="$WORK/utf8/$utf8_file.md"
utf8_summary=$(frontmatter_field "$utf8_path" "summary")
if iconv -f UTF-8 -t UTF-8 "$utf8_path" >/dev/null 2>&1 \
    && [[ $(LC_ALL=C.UTF-8 awk -v text="$utf8_summary" 'BEGIN { print length(text) }') -eq 80 ]]; then
  ok "multibyte add summary is valid UTF-8 and limited to 80 characters"
else
  bad "multibyte add summary is valid UTF-8 and limited to 80 characters"
fi

utf8_edit=$(printf '나%.0s' {1..90})
LC_ALL=C.UTF-8 MEM_DIR="$WORK/utf8" "$REPO/bin/mem" edit "$utf8_file" \
  "$utf8_edit" >/dev/null 2>&1
utf8_path=$(find "$WORK/utf8" -name '*.md' -print -quit)
utf8_summary=$(frontmatter_field "$utf8_path" "summary")
if iconv -f UTF-8 -t UTF-8 "$utf8_path" >/dev/null 2>&1 \
    && [[ $(LC_ALL=C.UTF-8 awk -v text="$utf8_summary" 'BEGIN { print length(text) }') -eq 80 ]]; then
  ok "multibyte edit summary is valid UTF-8 and limited to 80 characters"
else
  bad "multibyte edit summary is valid UTF-8 and limited to 80 characters"
fi

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
