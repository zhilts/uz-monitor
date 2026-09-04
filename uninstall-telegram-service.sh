#!/bin/zsh
set -eu

label="local.uz-monitor.telegram"
plist_path="${HOME}/Library/LaunchAgents/${label}.plist"
uid="$(id -u)"

launchctl bootout "gui/${uid}/${label}" 2>/dev/null || true
if [[ -e "$plist_path" ]]; then
    rm "$plist_path"
fi
echo "Telegram listener removed: ${label}"
