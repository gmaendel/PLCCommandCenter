#!/bin/bash
# Read-only smoke test of the unified poller against the REAL PLCs, in DRY-RUN, on a COPY of the DB.
# Nothing is sent, nothing is written to any PLC, the live DB and /tmp/pump_ack are never touched.
#   scripts/dryrun_smoke.sh [SECONDS=300] [SOURCE_DB=<this project>/data/pump_data.db]
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECS="${1:-300}"
SRC="${2:-$PROJECT_DIR/data/pump_data.db}"
WORK="$PROJECT_DIR/backups/smoke-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$WORK"
sqlite3 "$SRC" ".timeout 10000" ".backup '$WORK/pump_data.db'"
cp "$PROJECT_DIR/data/pump_config.json" "$WORK/" 2>/dev/null || echo '{"threshold_gpm": 2000}' > "$WORK/pump_config.json"
echo "smoke DB: $WORK/pump_data.db"
PLCCC_DRY_RUN=1 PLCCC_DB_PATH="$WORK/pump_data.db" PLCCC_CONFIG_PATH="$WORK/pump_config.json" \
PLCCC_ACK_FILE="$WORK/ack" PYTHONPATH="$("$PROJECT_DIR/venv/bin/python" -c 'import site;print(site.getsitepackages()[0])')" \
  "$PROJECT_DIR/venv/bin/python" -u "$PROJECT_DIR/app/plccc_poller.py" --duration "$SECS" 2>&1 | tee "$WORK/poller.log"
