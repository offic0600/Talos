#!/bin/bash
# Install the Talos executor as a launchd service on macOS (v2.3 §18.2 #6).
# KeepAlive=true means kill -9 → automatic restart within ThrottleInterval (5s).

set -euo pipefail

PLIST_SRC="$(dirname "$0")/com.talos.executor.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.talos.executor.plist"

echo "[talos] Installing launchd plist..."

# Source talos.envenv to resolve env vars for the plist template
if [ -f "$HOME/.hermes/talos.env" ]; then
    set -a; source "$HOME/.hermes/talos.env"; set +a
fi

# Generate plist with resolved paths
cat > "$PLIST_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.talos.executor</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>PYTHONPATH=${HERMES_HOME:-$HOME/.hermes}/hermes-agent</string>
        <string>TALOS_HOME=${TALOS_HOME:-$HOME/.hermes/talos}</string>
        <string>TALOS_WORKER_IMAGE=${TALOS_WORKER_IMAGE:-hermes-worker:latest}</string>
        <string>TALOS_GITLAB_URL=${TALOS_GITLAB_URL:-https://hgit.haier.net}</string>
        <string>TALOS_GITLAB_ADMIN_TOKEN=${TALOS_GITLAB_ADMIN_TOKEN:-}</string>
        <string>TALOS_ES_URL=${TALOS_ES_URL:-}</string>
        <string>HERMES_KANBAN_DB=${HERMES_KANBAN_DB:-$HOME/.hermes/kanban/kanban.db}</string>
        <string>python3</string>
        <string>-m</string>
        <string>talos.executor.main</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$(cd "$(dirname "$0")/.." && pwd)</string>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>5</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${TALOS_HOME:-$HOME/.hermes/talos}/executor.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${TALOS_HOME:-$HOME/.hermes/talos}/executor.stderr.log</string>
</dict>
</plist>
EOF

echo "[talos] Installed to $PLIST_DST"
echo "[talos] To load:   launchctl load $PLIST_DST"
echo "[talos] To unload: launchctl unload $PLIST_DST"
echo "[talos] KeepAlive=true: executor will restart within 5s of kill -9"
