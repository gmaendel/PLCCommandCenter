"""
ab_monitor.py — polls all Allen-Bradley SLC/MicroLogix and ControlLogix/CompactLogix
devices configured in the devices/tags DB.

Device types handled:
  allen_bradley_slc  → pycomm3 SLCDriver  (MicroLogix, SLC 500)
  allen_bradley_clx  → pycomm3 CIPDriver  (ControlLogix, CompactLogix)

Address format:
  SLC / MicroLogix:  N7:6   F8:2   B3:0   ST9:0   (standard SLC file/element notation)
  ControlLogix:      MyTag  Array[0]  Program:MainProgram.MyTag

Tag data types in the DB:
  INT   – signed 16-bit (SLC integer)
  REAL  – IEEE 754 float
  DINT  – signed 32-bit
  BOOL  – boolean
  (CLX reads the on-PLC type automatically; the DB data_type is used for display only)

Skips devices where all tags have log_source set — those are managed by pump_monitor.py.

Writes to tag_readings (tag_id, value, timestamp) — read by /api/device_status.
"""

import time
import datetime
import sqlite3
import logging
import subprocess
import sys

import requests
import plccc_settings as settings  # project paths + secrets from .env
from pycomm3 import SLCDriver, CIPDriver

DB_PATH = settings.DB_PATH
POLL_INTERVAL     = 60   # seconds between poll cycles
OFFLINE_THRESHOLD = 3    # consecutive failures before sending offline alert
IMSG_GROUP        = 'Recharge Pump Alarm'
TELEGRAM_TOKEN = settings.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = settings.TELEGRAM_CHAT_ID

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('ab_monitor')

# In-memory state
alarm_states  = {}   # {tag_id: {'active': bool}}
device_states = {}   # {dev_id: {'offline': bool, 'fail_count': int}}


# ── Notification helpers ──────────────────────────────────────────────────────

def send_telegram(text):
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        resp = requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
        if resp.ok:
            log.info('Telegram sent: %s', text[:80])
        else:
            log.error('Telegram failed: %s %s', resp.status_code, resp.text[:200])
    except Exception as e:
        log.error('Telegram error: %s', e)


def send_imessage(text):
    safe = text.replace('\\', '\\\\').replace('"', '\\"')
    script = f'''
tell application "Messages"
    try
        set targetChat to first chat whose name is "{IMSG_GROUP}"
        send "{safe}" to targetChat
    on error errMsg
        log "AppleScript Error: " & errMsg
    end try
end tell
'''
    try:
        with open('/tmp/ab_imsg.log', 'a') as f:
            subprocess.Popen(['osascript', '-e', script], stdout=f, stderr=f)
        log.info('iMessage dispatched: %s', text[:80])
    except Exception as e:
        log.error('iMessage error: %s', e)


def send_alarm(text):
    send_telegram(text)
    send_imessage(text)


# ── Alarm threshold checking ──────────────────────────────────────────────────

def check_alarm(tag_id, tag_name, device_name, units, value,
                alarm_enabled, alarm_low, alarm_high, alarm_message, conn):
    if not alarm_enabled:
        return

    in_alarm = False
    reason   = ''
    if alarm_low is not None and value < alarm_low:
        in_alarm = True
        reason   = f'below low limit of {alarm_low} {units}'
    elif alarm_high is not None and value > alarm_high:
        in_alarm = True
        reason   = f'above high limit of {alarm_high} {units}'

    state = alarm_states.get(tag_id, {'active': False})

    if in_alarm and not state.get('active', False):
        msg = alarm_message.strip() if alarm_message and alarm_message.strip() else (
            f'\u26a0\ufe0f ALARM [{device_name}] {tag_name} = {value} {units} \u2014 {reason}\n'
            f"(Reply 'ack' to acknowledge)"
        )
        send_alarm(msg)
        alarm_states[tag_id] = {'active': True}
        conn.execute(
            'INSERT OR REPLACE INTO tag_alarm_state (tag_id,active,triggered_at,last_value) '
            'VALUES (?,1,CURRENT_TIMESTAMP,?)',
            (tag_id, value)
        )
        log.warning('ALARM: %s %s = %s %s (%s)', device_name, tag_name, value, units, reason)

    elif not in_alarm and state.get('active', False):
        msg = f'\u2705 CLEAR [{device_name}] {tag_name} = {value} {units} \u2014 alarm cleared'
        send_alarm(msg)
        alarm_states[tag_id] = {'active': False}
        conn.execute(
            'INSERT OR REPLACE INTO tag_alarm_state (tag_id,active,triggered_at,last_value) '
            'VALUES (?,0,NULL,?)',
            (tag_id, value)
        )
        log.info('CLEAR: %s %s = %s %s', device_name, tag_name, value, units)


# ── Device loading ────────────────────────────────────────────────────────────

def load_ab_devices(conn):
    """
    Return list of (device_row, [tag_row, ...]) for all enabled AB devices.
    Skips any device where every tag has log_source set — those are already
    managed by pump_monitor.py (e.g. the Recharge Pump MicroLogix).
    """
    conn.row_factory = sqlite3.Row
    devs = conn.execute(
        "SELECT * FROM devices WHERE type IN ('allen_bradley_slc','allen_bradley_clx') "
        "AND enabled=1 ORDER BY id"
    ).fetchall()

    result = []
    for dev in devs:
        tags = conn.execute(
            "SELECT * FROM tags WHERE device_id=? AND enabled=1 ORDER BY id",
            (dev['id'],)
        ).fetchall()

        # Skip devices fully managed by pump_monitor (all tags have log_source)
        if tags and all(t['log_source'] for t in tags):
            log.debug('[%s] Skipping — fully managed by pump_monitor.py', dev['name'])
            continue

        result.append((dev, tags))
    return result


# ── Polling ───────────────────────────────────────────────────────────────────

def poll_slc_device(dev, tags, conn):
    """Poll an SLC / MicroLogix device using pycomm3 SLCDriver."""
    dev_id = dev['id']
    host   = dev['host']
    port   = dev['port'] or 44818
    name   = dev['name']
    plc_ip = f'{host}:{port}'

    st = device_states.setdefault(dev_id, {'offline': False, 'fail_count': 0})

    try:
        with SLCDriver(plc_ip) as plc:
            # ── Connected ──────────────────────────────────────────────────
            if st['offline']:
                ts  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                msg = (f'\u2705 RECOVERY [{name}]\n'
                       f'Allen-Bradley SLC/MicroLogix back online at {host}:{port}\n'
                       f'Time: {ts}')
                send_alarm(msg)
            st['offline']    = False
            st['fail_count'] = 0

            log.info('[%s] Connected to %s (%d tag(s))', name, plc_ip, len(tags))

            # Read all tag addresses in one batch where possible
            addrs = [t['address'] for t in tags]
            results = plc.read(*addrs)
            if not isinstance(results, (list, tuple)):
                results = [results]

            for tag, result in zip(tags, results):
                tag_id   = tag['id']
                tag_name = tag['name']

                if result is None or result.error:
                    log.warning('[%s] Tag "%s" (%s): read error — %s',
                                name, tag_name, tag['address'], result.error if result else 'None')
                    continue

                value = float(result.value) if result.value is not None else None
                if value is None:
                    continue

                # Apply scaling
                sf = tag['scale_factor'] if tag['scale_factor'] is not None else 1.0
                so = tag['scale_offset'] if tag['scale_offset'] is not None else 0.0
                if sf != 1.0 or so != 0.0:
                    value = round(value * sf + so, 4)
                else:
                    value = round(value, 4)

                log.info('[%s] %s (%s) = %s %s',
                         name, tag_name, tag['address'], value, tag['units'] or '')

                conn.execute(
                    "INSERT INTO tag_readings (tag_id, value) VALUES (?, ?)",
                    (tag_id, value)
                )
                conn.execute(
                    "DELETE FROM tag_readings WHERE tag_id=? "
                    "AND timestamp < datetime('now', '-7 days')",
                    (tag_id,)
                )

                check_alarm(
                    tag_id        = tag_id,
                    tag_name      = tag_name,
                    device_name   = name,
                    units         = tag['units'] or '',
                    value         = value,
                    alarm_enabled = tag['alarm_enabled'],
                    alarm_low     = tag['alarm_low'],
                    alarm_high    = tag['alarm_high'],
                    alarm_message = tag['alarm_message'] or '',
                    conn          = conn,
                )

    except Exception as e:
        log.error('[%s] Poll error: %s', name, e)
        st['fail_count'] += 1
        if st['fail_count'] >= OFFLINE_THRESHOLD and not st['offline']:
            st['offline'] = True
            ts  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            msg = (f'\ud83d\udd34 COMM LOSS [{name}]\n'
                   f'Allen-Bradley SLC/MicroLogix unreachable at {host}:{port}\n'
                   f'Time: {ts}\n'
                   f'Monitoring will resume automatically when it comes back online.')
            send_alarm(msg)


def poll_clx_device(dev, tags, conn):
    """Poll a ControlLogix / CompactLogix device using pycomm3 CIPDriver."""
    dev_id = dev['id']
    host   = dev['host']
    port   = dev['port'] or 44818
    name   = dev['name']
    plc_ip = f'{host}:{port}'

    st = device_states.setdefault(dev_id, {'offline': False, 'fail_count': 0})

    try:
        with CIPDriver(plc_ip) as plc:
            # ── Connected ──────────────────────────────────────────────────
            if st['offline']:
                ts  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                msg = (f'\u2705 RECOVERY [{name}]\n'
                       f'Allen-Bradley ControlLogix back online at {host}:{port}\n'
                       f'Time: {ts}')
                send_alarm(msg)
            st['offline']    = False
            st['fail_count'] = 0

            log.info('[%s] Connected to %s (%d tag(s))', name, plc_ip, len(tags))

            addrs   = [t['address'] for t in tags]
            results = plc.read(*addrs)
            if not isinstance(results, (list, tuple)):
                results = [results]

            for tag, result in zip(tags, results):
                tag_id   = tag['id']
                tag_name = tag['name']

                if result is None or result.error:
                    log.warning('[%s] Tag "%s" (%s): read error — %s',
                                name, tag_name, tag['address'], result.error if result else 'None')
                    continue

                value = float(result.value) if result.value is not None else None
                if value is None:
                    continue

                sf = tag['scale_factor'] if tag['scale_factor'] is not None else 1.0
                so = tag['scale_offset'] if tag['scale_offset'] is not None else 0.0
                if sf != 1.0 or so != 0.0:
                    value = round(value * sf + so, 4)
                else:
                    value = round(value, 4)

                log.info('[%s] %s (%s) = %s %s',
                         name, tag_name, tag['address'], value, tag['units'] or '')

                conn.execute(
                    "INSERT INTO tag_readings (tag_id, value) VALUES (?, ?)",
                    (tag_id, value)
                )
                conn.execute(
                    "DELETE FROM tag_readings WHERE tag_id=? "
                    "AND timestamp < datetime('now', '-7 days')",
                    (tag_id,)
                )

                check_alarm(
                    tag_id        = tag_id,
                    tag_name      = tag_name,
                    device_name   = name,
                    units         = tag['units'] or '',
                    value         = value,
                    alarm_enabled = tag['alarm_enabled'],
                    alarm_low     = tag['alarm_low'],
                    alarm_high    = tag['alarm_high'],
                    alarm_message = tag['alarm_message'] or '',
                    conn          = conn,
                )

    except Exception as e:
        log.error('[%s] Poll error: %s', name, e)
        st['fail_count'] += 1
        if st['fail_count'] >= OFFLINE_THRESHOLD and not st['offline']:
            st['offline'] = True
            ts  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            msg = (f'\ud83d\udd34 COMM LOSS [{name}]\n'
                   f'Allen-Bradley ControlLogix unreachable at {host}:{port}\n'
                   f'Time: {ts}\n'
                   f'Monitoring will resume automatically when it comes back online.')
            send_alarm(msg)


def poll_device(dev, tags, conn):
    if dev['type'] == 'allen_bradley_clx':
        poll_clx_device(dev, tags, conn)
    else:
        poll_slc_device(dev, tags, conn)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info('Allen-Bradley monitor starting — poll interval %ds', POLL_INTERVAL)

    db_conn = sqlite3.connect(DB_PATH, isolation_level=None)
    db_conn.execute('PRAGMA foreign_keys = ON')
    db_conn.execute('''CREATE TABLE IF NOT EXISTS tag_readings (
        tag_id    INTEGER NOT NULL,
        value     REAL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
    )''')
    db_conn.execute('''CREATE TABLE IF NOT EXISTS tag_alarm_state (
        tag_id       INTEGER PRIMARY KEY,
        active       INTEGER DEFAULT 0,
        triggered_at DATETIME DEFAULT NULL,
        last_value   REAL DEFAULT NULL,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
    )''')

    while True:
        try:
            devices = load_ab_devices(db_conn)
            if not devices:
                log.info('No enabled Allen-Bradley devices to poll (excluding pump_monitor devices).')
            for dev, tags in devices:
                poll_device(dev, tags, db_conn)
        except Exception as e:
            log.error('Poll cycle error: %s', e)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
