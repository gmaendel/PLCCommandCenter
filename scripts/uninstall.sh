#!/bin/bash
# Stop and remove every PLCCommandCenter LaunchAgent (v2 and v1) whose plist points into THIS folder.
# Leaves venv/, data/, logs/, backups/ and .env untouched. To go back to v1 use scripts/rollback.sh instead.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
env_get() { { grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$PROJECT_DIR/.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '"'"'" ; }
PREFIX="${PLCCC_LABEL_PREFIX:-$(env_get PLCCC_LABEL_PREFIX)}"; PREFIX="${PREFIX:-com.plccommandcenter}"
for L in $PREFIX.poller $PREFIX.web $PREFIX.legacy.pump_dashboard $PREFIX.legacy.pump_monitor $PREFIX.legacy.modbus-monitor $PREFIX.legacy.siemens_monitor $PREFIX.legacy.ab-monitor; do
  DEST="$LA_DIR/$L.plist"
  if [ -f "$DEST" ] && grep -q "$PROJECT_DIR/" "$DEST"; then
    launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
    rm -f "$DEST"; echo "removed $L"
  elif [ -f "$DEST" ]; then
    echo "skipped $L (plist points elsewhere)"
  fi
done
