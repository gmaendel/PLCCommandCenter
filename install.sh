#!/bin/bash
# PLC Command Center - one-step installer and updater for macOS (Mac mini).
#
#   curl -fsSL https://raw.githubusercontent.com/gmaendel/PLCCommandCenter/main/install.sh | bash
#   ./install.sh                       (from a cloned or unzipped copy)
#
# Safe to run again at any time: it updates the code, the Python packages and the background services,
# and keeps your .env, database, devices and settings.
#
# Options (all optional):
#   --dir DIR             where to install (default ~/PLCCommandCenter, or this folder when run from a copy)
#   --yes, -y             don't ask questions; accept the defaults (needs a password from one of the next two)
#   --password-file FILE  dashboard password from a file (first line). Or set PLCCC_DASHBOARD_PASSWORD.
#   --port N              dashboard port (default 8085)
#   --label-prefix P      launchd label prefix (default com.plccommandcenter); lets two copies coexist
#   --power / --no-power  do / don't set "never sleep + restart after power failure" (needs sudo)
#   --no-open             don't open System Settings, Finder, Messages or the browser
#   --no-update           don't download newer code; just (re)install what is there
#   --repo URL            git URL to install from (default: the GitHub repo)
#   --branch NAME         branch to install (default main)
#   --dry-run             show what would happen, change nothing
#   --uninstall           stop and remove the background services (keeps your files)
#   -h, --help            this help
set -euo pipefail
{  # the whole script is one block, so bash reads it completely before running (safe to update itself)

REPO_URL="https://github.com/gmaendel/PLCCommandCenter.git"
BRANCH="main"
DIR=""; YES=0; PW_FILE=""; PORT=""; PREFIX=""; POWER="ask"; OPEN=1; UPDATE=1; DRY=0; UNINSTALL=0
CLT_PY="/Library/Developer/CommandLineTools/usr/bin/python3"

usage() { if [ -f "${BASH_SOURCE[0]:-}" ]; then sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; else echo 'Options: see https://github.com/gmaendel/PLCCommandCenter/blob/main/install.sh'; fi; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="${2:?--dir needs a folder}"; shift ;;
    --yes|-y) YES=1 ;;
    --password-file) PW_FILE="${2:?--password-file needs a file}"; shift ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    --label-prefix) PREFIX="${2:?--label-prefix needs a value}"; shift ;;
    --power) POWER="yes" ;;
    --no-power) POWER="no" ;;
    --no-open) OPEN=0 ;;
    --no-update) UPDATE=0 ;;
    --repo) REPO_URL="${2:?--repo needs a URL}"; shift ;;
    --branch) BRANCH="${2:?--branch needs a name}"; shift ;;
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# ── output helpers ───────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
else
  B=""; DIM=""; RED=""; GRN=""; YEL=""; BLU=""; RST=""
fi
STEP=0; TOTAL=8
step() { STEP=$((STEP + 1)); printf '\n%s[%d/%d] %s%s\n' "$B$BLU" "$STEP" "$TOTAL" "$1" "$RST"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$RED" "$*" "$RST" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '  %s[dry-run]%s %s\n' "$DIM" "$RST" "$*"; else "$@"; fi; }

# Questions are read from the terminal even when this script arrives through "curl | bash".
TTY=0
if [ "$YES" = 0 ] && (exec 3</dev/tty) 2>/dev/null; then TTY=1; fi
ask_yn() {  # question default(y|n) -> 0 for yes
  local q="$1" def="$2" a hint="[y/N]"
  [ "$def" = y ] && hint="[Y/n]"
  if [ "$TTY" = 0 ]; then [ "$def" = y ]; return; fi
  printf '  %s?%s %s %s ' "$B" "$RST" "$q" "$hint"
  IFS= read -r a </dev/tty || a=""
  case "${a:-$def}" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}
ask() {  # question default -> answer on stdout
  local q="$1" def="$2" a
  if [ "$TTY" = 0 ]; then printf '%s' "$def"; return; fi
  printf '  %s?%s %s [%s] ' "$B" "$RST" "$q" "$def" >/dev/tty
  IFS= read -r a </dev/tty || a=""
  printf '%s' "${a:-$def}"
}
pause() { [ "$TTY" = 1 ] && { printf '  %s%s%s ' "$DIM" "$1" "$RST"; IFS= read -r _ </dev/tty || true; }; return 0; }

env_get() {  # KEY -> value from $DIR/.env
  { grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$DIR/.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '"'"'"
}
env_set() {  # KEY VALUE -> written into $DIR/.env (replaces any existing line)
  if [ "$DRY" = 1 ]; then printf '  %s[dry-run]%s set %s in .env\n' "$DIM" "$RST" "$1"; return; fi
  KEY="$1" VAL="$2" "$PYTHON" - "$DIR/.env" <<'PYEOF'
import os, re, sys
path, key, val = sys.argv[1], os.environ['KEY'], os.environ['VAL']
lines = open(path, encoding='utf-8').read().splitlines() if os.path.exists(path) else []
out, done = [], False
for ln in lines:
    if re.match(r'^\s*(export\s+)?%s=' % re.escape(key), ln):
        if not done:
            out.append("%s='%s'" % (key, val)); done = True
        continue
    out.append(ln)
if not done:
    out.append("%s='%s'" % (key, val))
open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
os.chmod(path, 0o600)
PYEOF
}

is_project() { [ -f "$1/app/plccc_poller.py" ] && [ -f "$1/scripts/install.sh" ]; }

# Folder this script lives in, when it was run from a copy (empty when piped from curl).
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  is_project "$SELF_DIR" || SELF_DIR=""
fi

printf '\n%sPLC Command Center installer%s %s\n' "$B" "$RST" "$([ "$DRY" = 1 ] && echo '(dry run - nothing will be changed)')"
[ "$(id -u)" != 0 ] || die "Run this as your normal user, not with sudo."

# ── uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  [ -n "$DIR" ] || DIR="${SELF_DIR:-$HOME/PLCCommandCenter}"
  is_project "$DIR" || die "No PLC Command Center found in $DIR (use --dir)."
  [ -n "$PREFIX" ] && export PLCCC_LABEL_PREFIX="$PREFIX"
  printf '\nRemoving the background services of %s\n' "$DIR"
  run "$DIR/scripts/uninstall.sh"
  ok "Services removed. Your files are still in $DIR."
  info "To delete everything (settings, history, devices): rm -rf \"$DIR\""
  exit 0
fi

# ── 1. this Mac ──────────────────────────────────────────────────────────────
step "Checking this Mac"
[ "$(uname -s)" = Darwin ] || die "This installer is for macOS."
OSV="$(sw_vers -productVersion)"; ARCH="$(uname -m)"; OSMAJ="${OSV%%.*}"
if [ "$OSMAJ" -lt 12 ]; then die "macOS $OSV is too old (macOS 13 Ventura or newer recommended)."; fi
[ "$OSMAJ" -ge 13 ] || warn "macOS $OSV works but isn't tested; macOS 13 or newer is recommended."
ok "macOS $OSV on $([ "$ARCH" = arm64 ] && echo 'Apple silicon' || echo "Intel ($ARCH)")"

# ── 2. Command Line Tools (Python 3 + git) ───────────────────────────────────
step "Apple Command Line Tools (Python and git)"
if ! xcode-select -p >/dev/null 2>&1; then
  warn "Not installed yet. A window will open: click ${B}Install${RST} and accept the license."
  info "(It's a free download from Apple and can take 5-20 minutes.)"
  if [ "$DRY" = 1 ]; then run xcode-select --install
  else
    xcode-select --install >/dev/null 2>&1 || true
    printf '  Waiting for the installer to finish '
    for _ in $(seq 1 360); do xcode-select -p >/dev/null 2>&1 && [ -x "$CLT_PY" ] && break; printf '.'; sleep 10; done
    echo
    xcode-select -p >/dev/null 2>&1 || die "The Command Line Tools didn't finish installing. Run this installer again once they have."
  fi
fi
if [ -x "$CLT_PY" ]; then PYTHON="$CLT_PY"; else PYTHON="/usr/bin/python3"; fi
if [ "$DRY" = 1 ] && ! xcode-select -p >/dev/null 2>&1; then
  ok "(dry run) would use $CLT_PY"
else
  PYV="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || die "Python 3 doesn't run ($PYTHON)."
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python $PYV is too old; 3.9 or newer is needed."
  PYAPP_BUNDLE="$("$PYTHON" -c 'import sys,os;print(os.path.join(sys.base_prefix,"Resources/Python.app"))')"
  ok "Python $PYV ($PYTHON)"
  if command -v git >/dev/null 2>&1; then ok "git $(git --version | awk '{print $3}')"; else warn "git not found; the zip download will be used."; fi
fi
info "${DIM}Homebrew is not needed; every package comes ready-built from PyPI (including snap7 for Siemens).${RST}"

# ── 3. the code ──────────────────────────────────────────────────────────────
step "Getting PLC Command Center"
if [ -z "$DIR" ]; then
  if [ -n "$SELF_DIR" ]; then DIR="$SELF_DIR"
  else DIR="$(ask "Install folder" "$HOME/PLCCommandCenter")"; fi
fi
case "$DIR" in "~"*) DIR="$HOME${DIR#\~}" ;; esac
case "$DIR" in /*) ;; *) DIR="$PWD/$DIR" ;; esac
ZIP_URL="${REPO_URL%.git}/archive/refs/heads/$BRANCH.zip"

# Never start a second copy of the services for a folder another label already runs
# (e.g. an older install with different launchd names) - every alarm would go out twice.
[ -n "$PREFIX" ] || PREFIX="${PLCCC_LABEL_PREFIX:-$(env_get PLCCC_LABEL_PREFIX)}"
PREFIX="${PREFIX:-com.plccommandcenter}"
case "$PREFIX" in *[!A-Za-z0-9._-]*) die "--label-prefix may only use letters, digits, '.', '_' and '-'." ;; esac
for P in "$HOME"/Library/LaunchAgents/*.plist; do
  [ -f "$P" ] || continue
  grep -q "<string>$DIR/app/plccc_poller.py</string>" "$P" 2>/dev/null || grep -q "<string>$DIR/app/pump_monitor.py</string>" "$P" 2>/dev/null || continue
  L="$(basename "$P" .plist)"
  case "$L" in "$PREFIX.poller"|"$PREFIX.legacy."*) ;;
    *) die "$DIR is already running under the launchd label '$L'. Re-run with --label-prefix ${L%.*} (or remove that service first) so alarms don't go out twice." ;;
  esac
done


# Fail early (before downloading anything) when there is no way to get a dashboard password.
if [ "$TTY" = 0 ] && [ "$DRY" = 0 ] && [ -z "$PW_FILE" ] && [ -z "${PLCCC_DASHBOARD_PASSWORD:-}" ]; then
  PW_EXIST="$(env_get DASHBOARD_PASSWORD)"
  if [ -z "$PW_EXIST" ] || [[ "$PW_EXIST" == *replace-with* ]]; then
    die "A dashboard password is needed: use --password-file FILE or PLCCC_DASHBOARD_PASSWORD=..."
  fi
fi

download_zip_into() {  # target dir; keeps .env, data, logs, backups, venv
  local tmp; tmp="$(mktemp -d)"
  run curl -fsSL "$ZIP_URL" -o "$tmp/src.zip"
  run unzip -q "$tmp/src.zip" -d "$tmp"
  if [ "$DRY" = 0 ]; then
    local src; src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
    mkdir -p "$1"
    rsync -a --exclude '.env' --exclude 'data/' --exclude 'logs/' --exclude 'backups/' --exclude 'venv/' \
          --exclude 'launchd/rendered/' "$src/" "$1/"
    mkdir -p "$1/data" "$1/logs"
    [ -f "$1/data/pump_config.example.json" ] || cp "$src/data/pump_config.example.json" "$1/data/" 2>/dev/null || true
  fi
  rm -rf "$tmp"
}

if is_project "$DIR"; then
  if [ "$UPDATE" = 0 ]; then ok "Using the copy in $DIR (no update)"
  elif [ -d "$DIR/.git" ] && command -v git >/dev/null 2>&1; then
    if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
      warn "$DIR has local code changes; not updating it (use --no-update to silence this)."
    elif run git -C "$DIR" pull --ff-only --quiet origin "$BRANCH" 2>/dev/null; then
      ok "Updated $DIR to the latest $BRANCH ($(git -C "$DIR" rev-parse --short HEAD))"
    else
      warn "Couldn't update $DIR from git (offline, or its history differs); installing the code that is there."
    fi
  elif [ -n "$SELF_DIR" ] && [ "$DIR" = "$SELF_DIR" ]; then
    ok "Using this copy in $DIR"
  else
    info "Updating $DIR from $ZIP_URL"
    download_zip_into "$DIR"; ok "Updated $DIR"
  fi
elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  die "$DIR already exists and isn't a PLC Command Center folder. Pick another folder with --dir."
elif [ -n "$SELF_DIR" ] && [ "$UPDATE" = 0 ]; then
  info "Copying $SELF_DIR to $DIR"
  run mkdir -p "$DIR"; run rsync -a --exclude '.env' --exclude 'data/*.db*' --exclude 'venv/' --exclude 'logs/*.log' "$SELF_DIR/" "$DIR/"
  ok "Copied to $DIR"
elif command -v git >/dev/null 2>&1; then
  run git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$DIR"
  ok "Downloaded to $DIR"
else
  download_zip_into "$DIR"; ok "Downloaded to $DIR"
fi

# ── 4. settings (.env) and the dashboard password ────────────────────────────
step "Settings and dashboard password"
if [ ! -f "$DIR/.env" ]; then
  run cp "$DIR/.env.example" "$DIR/.env"; run chmod 600 "$DIR/.env"
  env_set PLCCC_ACK_FILE "$DIR/data/pump_ack"
  ok "Created $DIR/.env"
else
  ok "Keeping your existing .env"
fi
[ -n "$PORT" ] && { case "$PORT" in *[!0-9]*) die "--port must be a number" ;; esac; env_set DASHBOARD_PORT "$PORT"; }
[ "$PREFIX" != com.plccommandcenter ] && env_set PLCCC_LABEL_PREFIX "$PREFIX"
PW_NOW="$(env_get DASHBOARD_PASSWORD)"
NEWPW=""
if [ -n "$PW_FILE" ]; then
  [ -r "$PW_FILE" ] || die "Can't read $PW_FILE"
  NEWPW="$(head -1 "$PW_FILE" | tr -d '\r\n')"
elif [ -n "${PLCCC_DASHBOARD_PASSWORD:-}" ]; then
  NEWPW="$PLCCC_DASHBOARD_PASSWORD"
elif [ -z "$PW_NOW" ] || [[ "$PW_NOW" == *replace-with* ]]; then
  [ "$TTY" = 1 ] || [ "$DRY" = 1 ] || die "A dashboard password is needed: use --password-file FILE or PLCCC_DASHBOARD_PASSWORD=..."
  if [ "$TTY" = 1 ]; then
    info "Choose a ${B}dashboard password${RST}. You'll need it to change settings on the dashboard."
    info "${DIM}(Nothing shows while you type.)${RST}"
    while :; do
      printf '  Password: ' >/dev/tty; IFS= read -r -s P1 </dev/tty; echo >/dev/tty
      printf '  Again:    ' >/dev/tty; IFS= read -r -s P2 </dev/tty; echo >/dev/tty
      if [ "${#P1}" -lt 6 ]; then warn "Use at least 6 characters."; continue; fi
      if [[ "$P1" == *\'* ]]; then warn "Please don't use the ' character."; continue; fi
      [ "$P1" = "$P2" ] && break
      warn "They didn't match - try again."
    done
    NEWPW="$P1"; unset P1 P2
  fi
fi
if [ -n "$NEWPW" ]; then
  [ "${#NEWPW}" -ge 6 ] || die "The dashboard password must be at least 6 characters."
  [[ "$NEWPW" != *\'* ]] || die "The dashboard password can't contain the ' character."
  env_set DASHBOARD_PASSWORD "$NEWPW"; unset NEWPW
  ok "Dashboard password saved in .env"
elif [ "$DRY" = 0 ]; then
  ok "Dashboard password already set"
fi

# ── 5. Python packages + background services ─────────────────────────────────
step "Python packages and background services"
info "Installing packages, running the self-tests and starting the services. This can take a few minutes..."
if [ "$DRY" = 1 ]; then
  run env PLCCC_LABEL_PREFIX="$PREFIX" PYTHON="$PYTHON" "$DIR/scripts/install.sh"
else
  if ! PLCCC_LABEL_PREFIX="$PREFIX" PYTHON="$PYTHON" "$DIR/scripts/install.sh" </dev/null 2>&1 | sed 's/^/    /'; then
    die "Setting up the services failed (see the messages above). Logs: $DIR/logs/"
  fi
  ok "Services running: $PREFIX.poller and $PREFIX.web"
fi

# ── 6. keep the Mac running ──────────────────────────────────────────────────
step "Keep the Mac awake and running"
SLEEP_NOW="$(pmset -g 2>/dev/null | awk '$1=="sleep"{print $2; exit}')"
AR_NOW="$(pmset -g 2>/dev/null | awk '$1=="autorestart"{print $2; exit}')"
if [ "$SLEEP_NOW" = 0 ] && [ "$AR_NOW" = 1 ]; then
  ok "Already set: never sleeps, restarts after a power failure"
else
  info "Alarms only work while the Mac is awake. Recommended: never sleep, and start again after a power cut."
  DO_POWER=0
  case "$POWER" in yes) DO_POWER=1 ;; no) ;; ask) ask_yn "Set that now? (asks for your Mac password)" y && DO_POWER=1 ;; esac
  if [ "$DO_POWER" = 1 ]; then
    if run sudo pmset -a sleep 0 autorestart 1; then ok "Set: never sleep, restart after power failure"
    else warn "Couldn't change it; do it later in System Settings > Energy."; fi
  else
    info "Skipped. You can set it in System Settings > Energy (Prevent automatic sleeping, Start up after power failure)."
  fi
fi
info "Also turn on ${B}automatic login${RST} (System Settings > Users & Groups) so everything starts after a restart."

# ── 7. Full Disk Access (iMessage replies acknowledge alarms) ────────────────
step "Full Disk Access (lets replies in the iMessage group acknowledge alarms)"
DB="$DIR/data/pump_data.db"
fda_status() { sqlite3 "$DB" "SELECT value FROM app_settings WHERE key='fda_status'" 2>/dev/null | "$PYTHON" -c 'import json,sys
try: d=json.load(sys.stdin); print(d.get("status","?") + "|" + d.get("checked_at",""))
except Exception: print("?|")' 2>/dev/null || echo "?|"; }
if [ "$DRY" = 1 ]; then
  run open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
else
  ST="$(fda_status)"
  if [ "${ST%%|*}" = ok ]; then
    ok "Granted - the alarm service can read Messages"
  else
    [ "${ST%%|*}" = missing ] && warn "Messages has no history on this Mac yet (sign in to Messages first)."
    info "Give this Python Full Disk Access (System Settings > Privacy & Security > Full Disk Access):"
    printf '\n    %s%s%s\n\n' "$B" "$PYAPP_BUNDLE" "$RST"
    info "Click ${B}+${RST}, then drag ${B}Python.app${RST} from the Finder window into the list (or press Cmd-Shift-G and paste"
    info "the path above), and make sure its switch is on."
    if [ "$OPEN" = 1 ] && [ "$TTY" = 1 ]; then
      open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
      if ask_yn "Show Python.app in Finder so you can drag it in?" y; then open -R "$PYAPP_BUNDLE" 2>/dev/null || true; fi
    fi
    if [ "$TTY" = 1 ]; then
      for _ in 1 2 3; do
        pause "Press Return once it's switched on (or type s and Return to skip)..."
        BEFORE="${ST#*|}"
        launchctl kickstart -k "gui/$(id -u)/$PREFIX.poller" 2>/dev/null || true
        for _ in $(seq 1 20); do sleep 1; ST="$(fda_status)"; [ "${ST#*|}" != "$BEFORE" ] && break; done
        if [ "${ST%%|*}" = ok ]; then ok "Full Disk Access works"; break; fi
        warn "Not yet (${ST%%|*}). Check that Python.app is in the list and switched on."
      done
    else
      info "Then restart the alarm service: launchctl kickstart -k gui/\$(id -u)/$PREFIX.poller"
    fi
  fi
fi

# ── 8. Messages + dashboard ──────────────────────────────────────────────────
step "Messages and the dashboard"
GROUP="$(env_get PLCCC_IMESSAGE_CHAT)"; GROUP="${GROUP:-PLC Alarms}"
DPORT="${PORT:-$(env_get DASHBOARD_PORT)}"; DPORT="${DPORT:-8085}"
info "In ${B}Messages${RST} on this Mac: sign in with an Apple ID, then make a group chat with the people who"
info "should get alarms and name it exactly ${B}$GROUP${RST} (or use another name and set it on the dashboard)."
if [ "$OPEN" = 1 ] && [ "$TTY" = 1 ] && [ "$DRY" = 0 ] && ask_yn "Open Messages now?" y; then open -a Messages 2>/dev/null || true; fi
LANIP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
HOSTN="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
HEALTH="$(curl -fsS -m 5 "http://127.0.0.1:$DPORT/healthz" 2>/dev/null || true)"
if [ "$DRY" = 0 ]; then
  if echo "$HEALTH" | grep -q '"ok":true'; then ok "Dashboard is up"; else warn "The dashboard didn't answer yet; give it a minute (logs: $DIR/logs/)."; fi
fi
if [ "$OPEN" = 1 ] && [ "$DRY" = 0 ]; then open "http://localhost:$DPORT" 2>/dev/null || true; fi

if [ "$DRY" = 1 ]; then printf '\n%sDry run finished - nothing was changed.%s\n' "$B" "$RST"
else printf '\n%s%s✓ PLC Command Center is installed%s\n' "$B" "$GRN" "$RST"; fi
printf '  Dashboard:  %shttp://%s:%s%s   (this Mac: http://localhost:%s, or http://%s.local:%s)\n' "$B" "${LANIP:-<this-mac-ip>}" "$DPORT" "$RST" "$DPORT" "$HOSTN" "$DPORT"
printf '  Folder:     %s\n' "$DIR"
printf '  Services:   %s.poller (alarms), %s.web (dashboard)\n' "$PREFIX" "$PREFIX"
printf '  Next:       unlock the dashboard, add your PLCs under Devices, set up Telegram under Settings,\n'
printf '              and tap "Send test message" in the iMessage card (click Allow when macOS asks).\n'
printf '  Guide:      %s/docs/SETUP.md\n' "$DIR"
printf '  Update:     run this installer again.   Remove: %s/install.sh --uninstall\n\n' "$DIR"
exit 0
}
