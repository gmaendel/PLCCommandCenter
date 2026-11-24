#!/bin/bash
# Make a consistent copy of the live SQLite DB (safe while the services are running)
# using sqlite3's online .backup, plus pump_config.json.
#
#   scripts/export_data.sh [SOURCE_DIR_OR_DB] [OUT_DIR]
#
# Defaults: SOURCE = this project's data/pump_data.db ; OUT_DIR = <project>/backups
# On the OLD Mini at cutover:
#   scripts/export_data.sh ~/.openclaw/workspace ~/Desktop/plccc-export
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:-$PROJECT_DIR/data/pump_data.db}"
OUT_DIR="${2:-$PROJECT_DIR/backups}"
[ -d "$SRC" ] && SRC="$SRC/pump_data.db"
[ -f "$SRC" ] || { echo "No database at $SRC" >&2; exit 1; }
SRC_DIR="$(cd "$(dirname "$SRC")" && pwd)"

mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/pump_data-$TS.db"
sqlite3 "$SRC" ".timeout 10000" ".backup '$OUT'"
CHECK="$(sqlite3 "$OUT" 'PRAGMA integrity_check;')"
[ "$CHECK" = "ok" ] || { echo "Integrity check FAILED on $OUT: $CHECK" >&2; exit 1; }
[ -f "$SRC_DIR/pump_config.json" ] && cp "$SRC_DIR/pump_config.json" "$OUT_DIR/pump_config-$TS.json"
echo "Backup OK: $OUT ($(du -h "$OUT" | cut -f1))"
sqlite3 "$OUT" "SELECT 'flow_log rows: '||COUNT(*) FROM flow_log; SELECT 'devices: '||COUNT(*) FROM devices; SELECT 'tags: '||COUNT(*) FROM tags;"
