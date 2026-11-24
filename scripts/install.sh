#!/bin/bash
# Install PLCCommandCenter v2 (unified poller + dashboard) on this Mac. Run as your user, not sudo.
#
#   scripts/install.sh --no-load   stage only: venv + deps + import check + unit tests + render plists.
#                                  Never touches ~/Library/LaunchAgents. Safe while v1 is live.
#   scripts/install.sh --replace   CUTOVER: preflight (as --no-load), back up the DB, stop + unload the
#                                  5 v1 agents (plists saved to backups/launchagents-<ts>/), load the 2 v2
#                                  agents, then verify (poller heartbeat + every enabled device polled +
#                                  dashboard /healthz). If verification fails it rolls back to v1
#                                  automatically (disable with --no-auto-rollback).
#   scripts/install.sh             load/refresh the v2 agents; refuses while any v1 agent is loaded.
#   Extra: --skip-tests   --no-auto-rollback   --verify-timeout SECONDS (default 150)
#
# Rollback any time: scripts/rollback.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"
VENV="$PROJECT_DIR/venv"
LA_DIR="$HOME/Library/LaunchAgents"
RENDER_DIR="$PROJECT_DIR/launchd/rendered"
LEGACY_NAMES="pump_dashboard pump_monitor modbus-monitor siemens_monitor ab-monitor"
DOMAIN="gui/$(id -u)"

LOAD=1; REPLACE=0; TESTS=1; AUTO_RB=1; VERIFY_TIMEOUT=150
while [ $# -gt 0 ]; do
  case "$1" in
    --no-load) LOAD=0 ;;
    --replace|--replace-existing) REPLACE=1 ;;
    --skip-tests) TESTS=0 ;;
    --no-auto-rollback) AUTO_RB=0 ;;
    --verify-timeout) shift; VERIFY_TIMEOUT="$1" ;;
    -h|--help) sed -n 2,17p "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" != "0" ] || { echo "Run as your normal user, not root." >&2; exit 1; }
is_loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }
env_get() { { grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$PROJECT_DIR/.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '"'"'" ; }
# launchd label prefix: PLCCC_LABEL_PREFIX (environment or .env), default com.plccommandcenter
PREFIX="${PLCCC_LABEL_PREFIX:-$(env_get PLCCC_LABEL_PREFIX)}"; PREFIX="${PREFIX:-com.plccommandcenter}"
case "$PREFIX" in *[!A-Za-z0-9._-]*|"") echo "Invalid PLCCC_LABEL_PREFIX: $PREFIX" >&2; exit 2 ;; esac
NEW_LABELS="$PREFIX.poller $PREFIX.web"
OLD_LABELS=""; for N in $LEGACY_NAMES; do OLD_LABELS="$OLD_LABELS $PREFIX.legacy.$N"; done

echo "==> Project: $PROJECT_DIR"
"$PYTHON" --version >/dev/null || { echo "Python not found at $PYTHON (xcode-select --install)" >&2; exit 1; }

# ── 1. venv + dependencies ───────────────────────────────────────────────────
[ -x "$VENV/bin/python" ] || { echo "==> Creating venv"; "$PYTHON" -m venv "$VENV"; }
"$VENV/bin/python" -m pip install -q -r "$PROJECT_DIR/requirements.txt"
VPY="$VENV/bin/python"

# ── 2. config / data sanity ──────────────────────────────────────────────────
mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/logs" "$RENDER_DIR"
if [ ! -f "$PROJECT_DIR/.env" ]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"; chmod 600 "$PROJECT_DIR/.env"
  echo "==> Created .env from .env.example"
fi
chmod 600 "$PROJECT_DIR/.env"
# Dashboard password: ask for one if it is missing or still the example placeholder.
PW_NOW="$(env_get DASHBOARD_PASSWORD)"
if [ -z "$PW_NOW" ] || [[ "$PW_NOW" == *replace-with* ]]; then
  if [ -t 0 ]; then
    echo "==> Choose a dashboard password (needed to change settings on the dashboard)."
    while :; do
      read -r -s -p "    Password: " P1; echo
      read -r -s -p "    Again:    " P2; echo
      if [[ "$P1" == *\'* ]]; then echo "    Please don't use the ' character - try again."; continue; fi
      if [ -n "$P1" ] && [ "$P1" = "$P2" ]; then break; fi
      echo "    Passwords were empty or didn't match - try again."
    done
    P1="$P1" "$PYTHON" - "$PROJECT_DIR/.env" <<'PYEOF'
import os, re, sys
path, pw = sys.argv[1], os.environ['P1']
lines = open(path, encoding='utf-8').read().splitlines()
out, done = [], False
for ln in lines:
    if re.match(r'^\s*(export\s+)?DASHBOARD_PASSWORD=', ln):
        if not done:
            out.append("DASHBOARD_PASSWORD='%s'" % pw.replace("'", "")); done = True
        continue
    out.append(ln)
if not done:
    out.append("DASHBOARD_PASSWORD='%s'" % pw.replace("'", ""))
open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
PYEOF
    unset P1 P2
    echo "    Saved to .env"
  else
    echo "!! Set DASHBOARD_PASSWORD in $PROJECT_DIR/.env, then re-run." >&2; LOAD=0
  fi
fi
[ -f "$PROJECT_DIR/data/pump_config.json" ] || cp "$PROJECT_DIR/data/pump_config.example.json" "$PROJECT_DIR/data/pump_config.json"
if [ "$(env_get PLCCC_DRY_RUN)" = "1" ]; then
  echo "!! .env has PLCCC_DRY_RUN=1 - the installed poller would NOT send alarms. Remove it first." >&2
  [ "$LOAD" = 0 ] || exit 1
fi
if [ ! -f "$PROJECT_DIR/data/pump_data.db" ] && [ "$LOAD" = 1 ]; then
  V1_LOADED=""
  for L in $OLD_LABELS; do is_loaded "$L" && V1_LOADED="$V1_LOADED $L"; done
  if [ -n "$V1_LOADED" ] || [ "$REPLACE" = 1 ]; then
    # upgrading a v1 install: its database must be here, never start the cutover on an empty one
    echo "!! data/pump_data.db not found - refusing to load (would start with an empty DB)." >&2; exit 1
  fi
  echo "==> Fresh install: creating an empty database (add your PLCs on the dashboard's Devices tab)"
  (cd "$PROJECT_DIR" && PYTHONPATH="$PROJECT_DIR/app" "$VPY" -c 'from plccc import web; web.init_db()')
fi

# ── 3. import check + unit tests (dry-run, temp DB; nothing is sent) ─────────
"$VPY" "$PROJECT_DIR/scripts/check_imports.py"
if [ "$TESTS" = 1 ]; then
  echo "==> Unit tests"
  if OUT="$(cd "$PROJECT_DIR" && PYTHONPATH="$PROJECT_DIR/app:$PROJECT_DIR" "$VPY" -m unittest discover -s tests -t . 2>&1)"; then
    echo "$OUT" | tail -3
  else
    echo "$OUT" | tail -40 >&2; echo "!! unit tests failed - not installing" >&2; exit 1
  fi
fi

# ── 4. render plists ─────────────────────────────────────────────────────────
PYAPP="$("$VPY" -c 'import sys,os;print(os.path.join(sys.base_prefix,"Resources/Python.app/Contents/MacOS/Python"))')"
SITE="$("$VPY" -c 'import site;print(site.getsitepackages()[0])')"
[ -x "$PYAPP" ] || { echo "!! $PYAPP not found (needed for Full Disk Access)" >&2; exit 1; }
render() {  # template out label
  sed -e "s#__LABEL__#$3#g" -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" -e "s#__PYTHON__#$VPY#g" -e "s#__PYAPP__#$PYAPP#g" \
      -e "s#__SITE_PACKAGES__#$SITE#g" "$1" > "$2"
  plutil -lint "$2" >/dev/null
}
for N in poller web; do render "$PROJECT_DIR/launchd/$N.plist.template" "$RENDER_DIR/$PREFIX.$N.plist" "$PREFIX.$N"; done
for N in $LEGACY_NAMES; do render "$PROJECT_DIR/launchd/legacy/$N.plist.template" "$RENDER_DIR/$PREFIX.legacy.$N.plist" "$PREFIX.legacy.$N"; done
echo "==> Rendered plists in $RENDER_DIR (v2 + legacy v1 for rollback)"
echo "    iMessage 'ack' replies need Full Disk Access for: ${PYAPP%/Contents/MacOS/Python}"
echo "    (System Settings > Privacy & Security > Full Disk Access - see docs/SETUP.md)"

if [ "$LOAD" = 0 ]; then
  echo "==> Staged only. Nothing loaded. Cutover: scripts/install.sh --replace"
  exit 0
fi

# ── 5. cutover ───────────────────────────────────────────────────────────────
LOADED_OLD=""
for L in $OLD_LABELS; do is_loaded "$L" && LOADED_OLD="$LOADED_OLD $L"; done
if [ -n "$LOADED_OLD" ] && [ "$REPLACE" != 1 ]; then
  echo "!! v1 agents are still loaded:$LOADED_OLD" >&2
  echo "   Running both would double every alarm. Use: scripts/install.sh --replace" >&2
  exit 1
fi
TS="$(date +%Y%m%d-%H%M%S)"
BK="$PROJECT_DIR/backups/launchagents-$TS"
mkdir -p "$BK"
echo "==> Backing up database"
"$PROJECT_DIR/scripts/export_data.sh" "$PROJECT_DIR/data/pump_data.db" "$PROJECT_DIR/backups" || { echo "!! DB backup failed - aborting before any change" >&2; exit 1; }

if [ "$REPLACE" = 1 ]; then
  echo "==> Stopping v1 agents (plists saved to $BK)"
  for L in $OLD_LABELS; do
    launchctl bootout "$DOMAIN/$L" 2>/dev/null || true
    [ -f "$LA_DIR/$L.plist" ] && mv "$LA_DIR/$L.plist" "$BK/"
    echo "   stopped $L"
  done
  sleep 2
fi
echo "$BK" > "$PROJECT_DIR/backups/LAST_CUTOVER"

echo "==> Loading v2 agents"
mkdir -p "$LA_DIR"
for L in $NEW_LABELS; do
  launchctl bootout "$DOMAIN/$L" 2>/dev/null || true
  # bootout returns before the old job is gone; bootstrapping too early fails with "5: Input/output error"
  for _ in $(seq 1 20); do is_loaded "$L" || break; sleep 0.5; done
  cp "$RENDER_DIR/$L.plist" "$LA_DIR/$L.plist"
  loaded=0
  for _ in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$LA_DIR/$L.plist" 2>/dev/null; then loaded=1; break; fi
    sleep 2
  done
  [ "$loaded" = 1 ] || { echo "!! could not load $L (launchctl bootstrap failed 5 times)" >&2; exit 1; }
  echo "   loaded $L"
done

# ── 6. verify ────────────────────────────────────────────────────────────────
PORT="$(env_get DASHBOARD_PORT)"; PORT="${PORT:-8085}"
DB="$PROJECT_DIR/data/pump_data.db"
echo "==> Verifying (up to ${VERIFY_TIMEOUT}s): poller heartbeat, every enabled device polled, dashboard on :$PORT"
START_UTC="$(date -u '+%Y-%m-%d %H:%M:%S')"
ok=0; end=$(( $(date +%s) + VERIFY_TIMEOUT ))
while [ "$(date +%s)" -lt "$end" ]; do
  sleep 5
  PID="$(launchctl print "$DOMAIN/$PREFIX.poller" 2>/dev/null | awk '/^[[:space:]]*pid =/{print $3; exit}')"
  HB_PID="$(sqlite3 "$DB" "SELECT pid FROM service_status WHERE name='poller' AND heartbeat >= '$START_UTC'" 2>/dev/null || true)"
  MISSING="$(sqlite3 "$DB" "SELECT COUNT(*) FROM devices d LEFT JOIN device_runtime r ON r.device_id=d.id WHERE d.enabled=1 AND (r.updated_at IS NULL OR r.updated_at < '$START_UTC')" 2>/dev/null || echo 99)"
  WEB="$(curl -fsS -m 5 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true)"
  if [ -n "$PID" ] && [ "$PID" = "$HB_PID" ] && [ "$MISSING" = "0" ] && echo "$WEB" | grep -q '"ok":true'; then ok=1; break; fi
done
if [ "$ok" = 1 ]; then
  echo "==> v2 is live: poller pid $PID, all enabled devices polled, dashboard http://$(hostname):$PORT"
  sqlite3 -header -column "$DB" "SELECT d.name, r.online, r.fail_count, r.last_error FROM devices d JOIN device_runtime r ON r.device_id=d.id WHERE d.enabled=1"
  echo "    Logs: tail -f $PROJECT_DIR/logs/plccc_poller.log"
  echo "    Rollback: $PROJECT_DIR/scripts/rollback.sh"
else
  echo "!! Verification failed (poller pid='$PID' heartbeat pid='$HB_PID' devices-not-polled='$MISSING' web='$WEB')" >&2
  tail -20 "$PROJECT_DIR/logs/plccc_poller.error.log" 2>/dev/null >&2 || true
  if [ "$AUTO_RB" = 1 ] && [ "$REPLACE" = 1 ]; then
    echo "!! Rolling back to v1 automatically" >&2
    "$PROJECT_DIR/scripts/rollback.sh"
  fi
  exit 1
fi
