#!/usr/bin/env bash
# tests/test_unit_name_couplings.sh — guard the systemd unit-name couplings
# that fail SILENTLY.
#
# Renaming a unit is easy; the danger is the places that only *match a
# string* against the unit name and therefore keep running while quietly
# doing nothing. The 2026-08-12 self-kill happened through exactly such a
# blind spot, and the 2026-08-19 rename could have reopened it. See
# deploy/MIGRATIONS.md.
#
# These assertions are deliberately derived from the shipped unit FILES, so
# renaming a unit without updating its dependents fails here instead of in
# production.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pass=0
fail=0
ok()  { pass=$((pass+1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf 'FAIL %s%s\n' "$1" "${2:+ — $2}"; }

# --- what do we actually ship? -------------------------------------------
thinkers_unit=$(basename "$(ls "$REPO"/deploy/*-thinkers@.service 2>/dev/null | head -1)" .service)
if [[ -z "$thinkers_unit" ]]; then
    bad "found a *-thinkers@.service unit in deploy/"
    echo; echo "$pass passed, $((fail+1)) failed"; exit 1
fi
ok "found thinkers template: ${thinkers_unit}.service"
thinkers_prefix="${thinkers_unit%@}"   # basename already carries the trailing @

# --- 1. the cgroup self-stop guard must know this unit name --------------
# bin/thinkers greps /proc/*/cgroup for the unit name. If the unit renames
# and this does not, the guard stops recognizing its own cgroup and
# self-stop protection is silently gone.
# Look only at the actual cgroup COMPARISON expressions, not at comments or
# other mentions of the name — a grep over the whole file passes even when
# the comparison has been renamed out from under the guard (verified).
guard_cmp=$(grep -E '\$my_cg" == \*' "$REPO/bin/thinkers" || true)
if [[ -z "$guard_cmp" ]]; then
    bad "found the cgroup comparison in bin/thinkers" \
        "expected a [[ \"\$my_cg\" == *\"<unit>@...\"* ]] test"
elif grep -qF "${thinkers_prefix}@" <<< "$guard_cmp"; then
    ok "bin/thinkers cgroup comparison matches ${thinkers_prefix}@"
else
    bad "bin/thinkers cgroup comparison matches ${thinkers_prefix}@" \
        "the guard would no longer recognize its own cgroup"
fi

# --- 2. the thinkersctl wrapper must target a unit we ship ---------------
ctl=$(ls "$REPO"/deploy/*-thinkersctl 2>/dev/null | head -1)
if [[ -n "$ctl" ]]; then
    ok "found thinkersctl wrapper: $(basename "$ctl")"
    if grep -q "${thinkers_prefix}" "$ctl"; then
        ok "wrapper targets ${thinkers_prefix}"
    else
        bad "wrapper targets ${thinkers_prefix}" \
            "it would systemctl a unit that does not exist"
    fi
    # The sudo rule must cover the wrapper path, or the control plane
    # silently loses the ability to start/stop dispatchers.
    sudoers=$(ls "$REPO"/deploy/sudoers-*-thinkers 2>/dev/null | head -1)
    if [[ -n "$sudoers" ]] && grep -q "$(basename "$ctl")" "$sudoers"; then
        ok "sudoers covers $(basename "$ctl")"
    else
        bad "sudoers covers $(basename "$ctl")" "dash cannot manage dispatchers"
    fi
else
    bad "found a *-thinkersctl wrapper in deploy/"
fi

# --- 3. prompts the agent reads must name a wrapper we install -----------
# bin/thinkers quotes a `sudo <wrapper> restart` command in the wake-note
# and the stop-refusal. If that names a binary we do not install, the mind
# is told to run something that does not exist, mid-incident.
quoted=$(grep -oE 'sudo [a-z]+-thinkersctl' "$REPO/bin/thinkers" | awk '{print $2}' | sort -u)
if [[ -z "$quoted" ]]; then
    ok "no wrapper command quoted in bin/thinkers prompts (nothing to check)"
else
    for q in $quoted; do
        if grep -rqw "$q" "$REPO/deploy/update.sh" "$REPO/deploy/setup.sh" 2>/dev/null; then
            ok "prompt-quoted '$q' is installed by update.sh/setup.sh"
        else
            bad "prompt-quoted '$q' is installed by update.sh/setup.sh" \
                "the agent would be told to run a nonexistent command"
        fi
    done
fi

# --- 4. deploy scripts must reference unit files that exist --------------
# A typo'd or stale unit filename here means the deploy silently skips
# re-syncing that unit.
missing=0
for u in "$REPO"/deploy/*.service; do
    base=$(basename "$u")
    grep -rqF "$base" "$REPO/deploy/update.sh" "$REPO/deploy/setup.sh" \
        "$REPO/deploy/migrate-units.sh" 2>/dev/null && continue
    # templates are referenced by prefix, not full filename
    prefix="${base%.service}"
    grep -rqF "$prefix" "$REPO/deploy/update.sh" "$REPO/deploy/setup.sh" \
        "$REPO/deploy/migrate-units.sh" 2>/dev/null && continue
    bad "deploy scripts reference $base"; missing=1
done
[[ $missing -eq 0 ]] && ok "every shipped .service is referenced by a deploy script"

if grep -rqF "headlong-assistant@.target" "$REPO/deploy/setup.sh" "$REPO/deploy/update.sh" \
    && grep -qF "headlong-thinkers@%i.service" "$REPO/deploy/headlong-assistant@.target" \
    && grep -qF "headlong-assistant-codex@%i.service" "$REPO/deploy/headlong-assistant@.target" \
    && grep -qF "headlong-assistant-web@%i.service" "$REPO/deploy/headlong-assistant@.target"; then
    ok "Personal Assistant target groups the existing mind and both source bridges"
else
    bad "Personal Assistant target groups the existing mind and both source bridges"
fi

# --- 5. no stale references to a unit name we no longer ship -------------
# Catches a half-finished rename: code still naming the old unit.
shipped=$(cd "$REPO/deploy" && ls *.service 2>/dev/null | sed 's/\.service$//' | sed 's/@$//')
stale=0
for ref in $(grep -rhoE '\b(shellm|shelly|headlong)-(web|thinkers|thinkers-alert|slack-bridge|slack-agent|telegram-bridge)\b' \
        "$REPO/deploy"/*.sh "$REPO/deploy"/*.service "$REPO/deploy/scripts"/* 2>/dev/null \
        | sort -u); do
    printf '%s\n' "$shipped" | grep -qx "$ref" && continue
    # the compat paths intentionally name the old units
    grep -rqF "$ref" "$REPO/deploy/migrate-units.sh" && continue
    grep -qxF "$ref" <<< "shellm-thinkers" && continue   # legacy wrapper prefix
    bad "stale unit reference in deploy/: $ref"; stale=1
done
[[ $stale -eq 0 ]] && ok "no stale unit-name references in deploy/"

echo
echo "$pass passed, $fail failed"
[[ $fail -eq 0 ]]
