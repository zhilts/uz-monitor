#!/bin/zsh
set -eu

project_dir="${0:A:h}"
label="local.uz-monitor.telegram"
launch_agents_dir="${HOME}/Library/LaunchAgents"
plist_path="${launch_agents_dir}/${label}.plist"
log_path="${project_dir}/data/telegram-commands.log"
uid="$(id -u)"

mkdir -p "$launch_agents_dir" "${project_dir}/data"
cat > "$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array><string>${project_dir}/telegram-commands.sh</string></array>
    <key>WorkingDirectory</key>
    <string>${project_dir}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>${log_path}</string>
    <key>StandardErrorPath</key><string>${log_path}</string>
</dict>
</plist>
EOF

launchctl bootout "gui/${uid}/${label}" 2>/dev/null || true
launchctl bootstrap "gui/${uid}" "$plist_path"
echo "Telegram listener installed: ${label}"
