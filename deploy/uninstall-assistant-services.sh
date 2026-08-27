#!/usr/bin/env bash
set -euo pipefail

# Remove only Personal Assistant supervision. Identity state, the dashboard,
# and existing thinker units are deliberately preserved.

[[ "$(id -u)" -eq 0 ]] \
    || { echo "Run as root (sudo bash deploy/uninstall-assistant-services.sh)" >&2; exit 1; }

mapfile -t targets < <(
    systemctl list-unit-files --no-legend 'headlong-assistant@*.target' 2>/dev/null \
        | awk '{print $1}' \
        | grep -E '^headlong-assistant@[a-z0-9][a-z0-9-]{0,62}\.target$' \
        || true
)
for target in "${targets[@]+"${targets[@]}"}"; do
    systemctl disable --now "$target" >/dev/null 2>&1 || true
done
systemctl disable --now headlong-archive.service >/dev/null 2>&1 || true

for path in \
    /etc/systemd/system/headlong-assistant-codex@.service \
    /etc/systemd/system/headlong-assistant-web@.service \
    /etc/systemd/system/headlong-assistant-alert@.service \
    /etc/systemd/system/headlong-assistant@.target \
    /etc/systemd/system/headlong-archive.service; do
    rm -f "$path"
done
systemctl daemon-reload
systemctl reset-failed 'headlong-assistant-codex@*.service' \
    'headlong-assistant-web@*.service' 'headlong-assistant-alert@*.service' \
    'headlong-assistant@*.target' \
    >/dev/null 2>&1 || true

echo "Personal Assistant services removed; Observer Identity state was preserved."
