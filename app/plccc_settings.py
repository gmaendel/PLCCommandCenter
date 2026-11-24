"""
Shared settings for PLCCommandCenter services.

Loads KEY=VALUE pairs from <project>/.env (gitignored) into the environment
at import time (real environment variables win), then exposes project-relative
paths and secrets. No third-party dependency needed.
"""
import os
import secrets as _secrets
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, val = line.split('=', 1)
            key = key.strip()
            if key.startswith('export '):
                key = key[len('export '):].strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            os.environ.setdefault(key, val)


ENV_FILE = os.path.expanduser(os.environ.get('PLCCC_ENV_FILE') or os.path.join(PROJECT_DIR, '.env'))
_load_env(ENV_FILE)


def _get(key, default=''):
    return os.environ.get(key, default)


# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR    = os.path.expanduser(_get('PLCCC_DATA_DIR') or os.path.join(PROJECT_DIR, 'data'))
DB_PATH     = os.path.expanduser(_get('PLCCC_DB_PATH') or os.path.join(DATA_DIR, 'pump_data.db'))
CONFIG_PATH = os.path.expanduser(_get('PLCCC_CONFIG_PATH') or os.path.join(DATA_DIR, 'pump_config.json'))

# ── Secrets ──────────────────────────────────────────────────────────────────
def _secret(key):
    # untouched .env.example placeholders ('replace-with-…') count as "not set"
    v = _get(key).strip()
    return '' if 'replace-with' in v else v


# Telegram can also be set up (and changed) on the dashboard's Settings page; these are the fallback.
TELEGRAM_BOT_TOKEN = _secret('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID   = _secret('TELEGRAM_CHAT_ID')

DASHBOARD_PASSWORD = _secret('DASHBOARD_PASSWORD')
if not DASHBOARD_PASSWORD:
    # Fail closed: with no password configured, no settings change can be authorized.
    DASHBOARD_PASSWORD = _secrets.token_hex(32)
    print('WARNING: DASHBOARD_PASSWORD not set in .env; dashboard settings changes are disabled.',
          file=sys.stderr)

# ── Non-secret overrides (defaults match the original scripts) ───────────────
DASHBOARD_HOST = _get('DASHBOARD_HOST', '0.0.0.0')
DASHBOARD_PORT = int(_get('DASHBOARD_PORT', '8085'))
PUMP_PLC_HOST  = _get('PUMP_PLC_HOST', 'pump-plc.example.com')
PUMP_PLC_PORT  = int(_get('PUMP_PLC_PORT', '44818'))

# ── v2 unified service (plccc poller + web) ──────────────────────────────────
def _flag(key, default='0'):
    return _get(key, default).strip().lower() in ('1', 'true', 'yes', 'on')


# PLCCC_DRY_RUN=1 -> alerts are logged (logs + alert_log table) but never sent.
DRY_RUN = _flag('PLCCC_DRY_RUN')

POLL_INTERVAL = float(_get('PLCCC_POLL_INTERVAL', '60'))    # seconds, every device
# Default iMessage group (the Settings page can change it). New installs get 'PLC Alarms' from .env.example;
# an .env without the line keeps the original 'Recharge Pump Alarm'.
IMESSAGE_CHAT = _get('PLCCC_IMESSAGE_CHAT', 'Recharge Pump Alarm')
# Seed the original two v1 devices into an empty DB? Only for v1 databases (flow/aquifer history present)
# or when PLCCC_SEED_V1_DEVICES=1; a fresh install starts with no devices.
SEED_V1_DEVICES = _get('PLCCC_SEED_V1_DEVICES', '').strip().lower() in ('1', 'true', 'yes', 'on')
# The live pump_monitor uses /tmp/pump_ack. A dry-run must never create/delete the
# live ack file, so dry-run defaults to a separate path.
ACK_FILE = os.path.expanduser(_get('PLCCC_ACK_FILE') or ('/tmp/pump_ack.dryrun' if DRY_RUN else '/tmp/pump_ack'))
CHAT_DB_PATH = os.path.expanduser(_get('PLCCC_CHAT_DB', '~/Library/Messages/chat.db'))
LOG_DIR = os.path.expanduser(_get('PLCCC_LOG_DIR') or os.path.join(PROJECT_DIR, 'logs'))
WEB_THREADS = int(_get('PLCCC_WEB_THREADS', '8'))
