#!/usr/bin/env bash
# Install the WinWorld daily pipeline LaunchAgent.
#
#   ./winworld/launchd/install.sh
#
# Copies the plist into ~/Library/LaunchAgents/, bootstraps it with launchctl,
# and enables it. Safe to re-run (will replace an existing copy).

set -euo pipefail

LABEL="com.danifunker.winworld-pipeline"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_PLIST="$SRC_DIR/${LABEL}.plist"
DEST_DIR="$HOME/Library/LaunchAgents"
DEST_PLIST="$DEST_DIR/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
TARGET="${DOMAIN}/${LABEL}"

if [[ ! -f "$SRC_PLIST" ]]; then
    echo "error: source plist missing: $SRC_PLIST" >&2
    exit 1
fi

mkdir -p "$DEST_DIR"

# If already loaded, bootout first so the new copy takes effect.
if launchctl print "$TARGET" >/dev/null 2>&1; then
    echo "→ unloading existing $LABEL"
    launchctl bootout "$TARGET" || true
fi

echo "→ copying plist to $DEST_PLIST"
cp "$SRC_PLIST" "$DEST_PLIST"

echo "→ bootstrap + enable"
launchctl bootstrap "$DOMAIN" "$DEST_PLIST"
launchctl enable "$TARGET"

echo
echo "Installed. Verify:"
echo "  launchctl print $TARGET | grep -E 'state|next firing'"
echo "Trigger now:"
echo "  launchctl kickstart $TARGET"
echo "Logs:"
echo "  tail -f ~/Library/Logs/winworld-pipeline.out.log"
echo "  tail -f ~/Library/Logs/winworld-pipeline.err.log"
