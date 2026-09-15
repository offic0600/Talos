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
RUN_SCRIPT_DST="${TALOS_HOME:-$HOME/.hermes/talos}/run-executor.sh"

# Install run-executor.sh from repo
SCRIPT_SRC="$(dirname "$0")/run-executor.sh"
cp "$SCRIPT_SRC" "$RUN_SCRIPT_DST"
chmod +x "$RUN_SCRIPT_DST"
echo "[talos] Installed run-executor.sh to $RUN_SCRIPT_DST"

cat > "$PLIST_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.talos.executor</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$RUN_SCRIPT_DST</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$(cd "$(dirname "$0")/.." && pwd)</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/Docker.app/Contents/Resources/bin</string>
    </dict>

    <!-- KeepAlive: restart on crash, kill -9, or unexpected exit -->
    <key>KeepAlive</key>
    <true/>

    <!-- Restart after 5 seconds (matches systemd RestartSec=5) -->
    <key>ThrottleInterval</key>
    <integer>5</integer>

    <!-- Run at load -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Standard output/error paths -->
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
