#!/bin/bash
# Stop and remove every PLCCommandCenter LaunchAgent (v2 and v1) whose plist points into THIS folder.
# Leaves venv/, data/, logs/, backups/ and .env untouched. To go back to v1 use scripts/rollback.sh instead.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
for L in com.plccommandcenter.poller com.plccommandcenter.web com.plccommandcenter.legacy.pump_dashboard com.plccommandcenter.legacy.pump_monitor com.plccommandcenter.legacy.modbus-monitor com.plccommandcenter.legacy.siemens_monitor com.plccommandcenter.legacy.ab-monitor; do
  DEST="$LA_DIR/$L.plist"
  if [ -f "$DEST" ] && grep -q "$PROJECT_DIR/" "$DEST"; then
    launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
    rm -f "$DEST"; echo "removed $L"
  elif [ -f "$DEST" ]; then
    echo "skipped $L (plist points elsewhere)"
  fi
done
