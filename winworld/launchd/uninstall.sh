#!/usr/bin/env bash
# Uninstall the WinWorld daily pipeline LaunchAgent.
#
#   ./winworld/launchd/uninstall.sh
#
# Idempotent: works whether or not the agent is currently loaded.
# Leaves log files in ~/Library/Logs/ alone; remove them manually if desired.

set -euo pipefail

LABEL="com.danifunker.winworld-pipeline"
DEST_PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
TARGET="${DOMAIN}/${LABEL}"

if launchctl print "$TARGET" >/dev/null 2>&1; then
    echo "→ booting out $TARGET"
    launchctl bootout "$TARGET" || true
else
    echo "→ $TARGET not loaded (skipping bootout)"
fi

if [[ -f "$DEST_PLIST" ]]; then
    echo "→ removing $DEST_PLIST"
    rm "$DEST_PLIST"
else
    echo "→ $DEST_PLIST not present (already gone)"
fi

echo
echo "Uninstalled. Log files (kept):"
echo "  ~/Library/Logs/winworld-pipeline.out.log"
echo "  ~/Library/Logs/winworld-pipeline.err.log"
echo "Remove them with:  rm ~/Library/Logs/winworld-pipeline.{out,err}.log"
