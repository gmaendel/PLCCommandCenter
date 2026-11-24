#!/bin/bash
# Roll back from v2 to the five v1 agents (pump_dashboard, pump_monitor, modbus-monitor,
# siemens_monitor, ab-monitor). Safe to run repeatedly.
#   1. unloads + removes com.plccommandcenter.poller / com.plccommandcenter.web
#   2. restores the v1 plists saved by install.sh --replace (backups/LAST_CUTOVER), or - if
#      none - the legacy plists rendered into launchd/rendered/, and loads them.
# The database needs no rollback: v2 only added tables/columns, which v1 ignores.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
OLD_LABELS="com.plccommandcenter.legacy.pump_dashboard com.plccommandcenter.legacy.pump_monitor com.plccommandcenter.legacy.modbus-monitor com.plccommandcenter.legacy.siemens_monitor com.plccommandcenter.legacy.ab-monitor"

for L in com.plccommandcenter.poller com.plccommandcenter.web; do
  launchctl bootout "$DOMAIN/$L" 2>/dev/null && echo "stopped $L" || true
  rm -f "$LA_DIR/$L.plist"
done
sleep 2

SRC=""
[ -f "$PROJECT_DIR/backups/LAST_CUTOVER" ] && SRC="$(cat "$PROJECT_DIR/backups/LAST_CUTOVER")"
for L in $OLD_LABELS; do
  if [ -n "$SRC" ] && [ -f "$SRC/$L.plist" ]; then P="$SRC/$L.plist"
  elif [ -f "$PROJECT_DIR/launchd/rendered/$L.plist" ]; then P="$PROJECT_DIR/launchd/rendered/$L.plist"
  else echo "!! no plist for $L (run scripts/install.sh --no-load to render)" >&2; continue; fi
  cp "$P" "$LA_DIR/$L.plist"
  launchctl bootout "$DOMAIN/$L" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$LA_DIR/$L.plist" && echo "restored $L (from $P)"
done
sleep 3
launchctl list | grep -E 'com\.plccommandcenter\.' || true
echo "Rollback done. v1 logs: $PROJECT_DIR/logs/pump_monitor.log etc."
