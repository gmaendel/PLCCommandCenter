#!/bin/bash
# Roll back from v2 to the five v1 agents (pump_dashboard, pump_monitor, modbus-monitor,
# siemens_monitor, ab-monitor). Safe to run repeatedly.
#   1. unloads + removes <prefix>.poller / <prefix>.web (prefix: PLCCC_LABEL_PREFIX, default com.plccommandcenter)
#   2. restores the v1 plists saved by install.sh --replace (backups/LAST_CUTOVER), or - if
#      none - the legacy plists rendered into launchd/rendered/, and loads them.
# The database needs no rollback: v2 only added tables/columns, which v1 ignores.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
env_get() { { grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$PROJECT_DIR/.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '"'"'" ; }
PREFIX="${PLCCC_LABEL_PREFIX:-$(env_get PLCCC_LABEL_PREFIX)}"; PREFIX="${PREFIX:-com.plccommandcenter}"
OLD_LABELS="$PREFIX.legacy.pump_dashboard $PREFIX.legacy.pump_monitor $PREFIX.legacy.modbus-monitor $PREFIX.legacy.siemens_monitor $PREFIX.legacy.ab-monitor"

for L in $PREFIX.poller $PREFIX.web; do
  if launchctl bootout "$DOMAIN/$L" 2>/dev/null; then echo "stopped $L"; fi
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
launchctl list | grep -F "$PREFIX." || true
echo "Rollback done. v1 logs: $PROJECT_DIR/logs/pump_monitor.log etc."
