"""
siemens_monitor.py — polls all Siemens S7 devices configured in the devices/tags DB.

Address format in the tags table:
  DB6.REAL108   = Data Block 6, REAL (4-byte IEEE754 float) at byte offset 108
  DB6.INT44     = Data Block 6, INT  (2-byte signed integer) at byte offset 44
  DB6.WORD44    = Data Block 6, WORD (2-byte unsigned 16-bit) at byte offset 44
  DB6.DINT44    = Data Block 6, DINT (4-byte signed integer) at byte offset 44
  DB6.DBD108    = Data Block 6, REAL at byte 108 (legacy DBD alias)
  DB6.DBX4.2    = Data Block 6, BOOL at byte 4, bit 2

Writes to tag_readings (tag_id, value, timestamp) — read by /api/device_status.
Also dual-writes Aquifer Level (DB6.REAL108) to aquifer_log for chart backward compat.
"""

import time
import datetime
import socket
import sqlite3
import struct
import logging
import re
import sys

import subprocess
import requests
import plccc_settings as settings  # project paths + secrets from .env
import snap7
from snap7.util import get_real, get_int, get_word, get_bool, get_dint

DB_PATH = settings.DB_PATH
POLL_INTERVAL = 60  # seconds

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('siemens_monitor')

IMSG_GROUP       = 'Recharge Pump Alarm'
TELEGRAM_TOKEN = settings.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = settings.TELEGRAM_CHAT_ID
OFFLINE_THRESHOLD = 3   # consecutive poll failures before alerting

# In-memory state
alarm_states  = {}   # {tag_id: {'active': bool}}
device_states = {}   # {dev_id: {'offline': bool, 'fail_count': int}}


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
        with open('/tmp/siemens_imsg.log', 'a') as f:
            subprocess.Popen(['osascript', '-e', script], stdout=f, stderr=f)
        log.info('iMessage dispatched: %s', text[:80])
    except Exception as e:
        log.error('iMessage error: %s', e)


def send_alarm(text):
    send_telegram(text)
    send_imessage(text)


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
            'INSERT OR REPLACE INTO tag_alarm_state (tag_id,active,triggered_at,last_value) VALUES (?,1,CURRENT_TIMESTAMP,?)',
            (tag_id, value)
        )
        log.warning('ALARM: %s %s = %s %s (%s)', device_name, tag_name, value, units, reason)
    elif not in_alarm and state.get('active', False):
        msg = f'\u2705 CLEAR [{device_name}] {tag_name} = {value} {units} \u2014 alarm cleared'
        send_alarm(msg)
        alarm_states[tag_id] = {'active': False}
        conn.execute(
            'INSERT OR REPLACE INTO tag_alarm_state (tag_id,active,triggered_at,last_value) VALUES (?,0,NULL,?)',
            (tag_id, value)
        )
        log.info('CLEAR: %s %s = %s %s', device_name, tag_name, value, units)


def parse_address(addr_str):
    """
    Parse a Siemens S7 address string.
    Returns (db_number, data_type, byte_offset, bit_offset).

    Supported formats:
      DB6.REAL108  → (6, 'REAL', 108, 0)
      DB6.INT44    → (6, 'INT',  44,  0)
      DB6.WORD44   → (6, 'WORD', 44,  0)
      DB6.DINT44   → (6, 'DINT', 44,  0)
      DB6.DBD108   → (6, 'REAL', 108, 0)   legacy alias
      DB6.DBX4.2   → (6, 'BOOL', 4,   2)
    """
    s = addr_str.strip().upper()
    m = re.match(r'DB(\d+)\.(REAL|INT|WORD|DINT|DBD|DBX|BOOL)(\d+)(?:\.(\d+))?', s)
    if not m:
        raise ValueError(f'Unsupported address format: {addr_str!r}')

    db_num   = int(m.group(1))
    type_str = m.group(2)
    byte_off = int(m.group(3))
    bit_off  = int(m.group(4)) if m.group(4) is not None else 0

    # Normalize legacy aliases
    if type_str == 'DBD':
        type_str = 'REAL'
    elif type_str == 'DBX':
        type_str = 'BOOL'

    return db_num, type_str, byte_off, bit_off


def byte_count_for_type(data_type):
    dt = data_type.upper()
    if dt in ('REAL', 'DINT'):
        return 4
    elif dt == 'BOOL':
        return 1       # read 1 byte; bit extracted by get_bool
    else:              # INT, WORD
        return 2


def decode_value(data, data_type, bit_offset=0):
    """Convert raw snap7 bytes to a Python float."""
    dt = data_type.upper()
    if dt == 'REAL':
        return float(get_real(data, 0))
    elif dt == 'INT':
        return float(get_int(data, 0))
    elif dt == 'WORD':
        return float(get_word(data, 0))
    elif dt == 'DINT':
        return float(get_dint(data, 0))
    elif dt == 'BOOL':
        return float(get_bool(data, 0, bit_offset))
    else:
        log.warning('Unknown data type %r — treating as REAL', data_type)
        return float(get_real(data, 0))


def load_siemens_devices(conn):
    """Return list of (device_row, [tag_row, ...]) for all enabled S7 devices."""
    conn.row_factory = sqlite3.Row
    devs = conn.execute(
        "SELECT * FROM devices WHERE type='siemens_s7' AND enabled=1 ORDER BY id"
    ).fetchall()
    result = []
    for dev in devs:
        tags = conn.execute(
            "SELECT * FROM tags WHERE device_id=? AND enabled=1 ORDER BY id",
            (dev['id'],)
        ).fetchall()
        result.append((dev, tags))
    return result


def poll_device(dev, tags, conn, plc_clients):
    """Connect (or reuse) PLC client and poll every enabled tag."""
    dev_id = dev['id']
    host   = dev['host']
    port   = dev['port'] or 102
    name   = dev['name']

    plc = plc_clients.get(dev_id)
    if plc is None:
        plc = snap7.client.Client()
        plc_clients[dev_id] = plc

    st = device_states.setdefault(dev_id, {'offline': False, 'fail_count': 0})

    try:
        if not plc.get_connected():
            ip = socket.gethostbyname(host)
            plc.connect(ip, rack=0, slot=1, tcp_port=port)

        # ── Connected successfully ───────────────────────────────────────
        if st['offline']:
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            msg = f'\u2705 RECOVERY [{name}]\nSiemens S7 device back online at {host}:{port}\nTime: {ts}'
            send_alarm(msg)
        st['offline'] = False
        st['fail_count'] = 0

        log.info('[%s] Connected to %s:%s (%d tag(s))', name, host, port, len(tags))

        for tag in tags:
            tag_id   = tag['id']
            tag_name = tag['name']
            addr_str = tag['address']

            try:
                db_num, data_type, byte_off, bit_off = parse_address(addr_str)
                count = byte_count_for_type(data_type)
                data  = plc.db_read(db_num, byte_off, count)
                value = round(decode_value(data, data_type, bit_offset=bit_off), 4)
                # Apply per-tag scaling: result = (raw × factor) + offset
                scale_factor = tag['scale_factor'] if 'scale_factor' in tag.keys() and tag['scale_factor'] is not None else 1.0
                scale_offset = tag['scale_offset'] if 'scale_offset' in tag.keys() and tag['scale_offset'] is not None else 0.0
                if scale_factor != 1.0 or scale_offset != 0.0:
                    value = round(value * scale_factor + scale_offset, 4)

                log.info('[%s] %s (%s) = %s %s',
                         name, tag_name, addr_str, value, tag['units'] or '')

                # Write to the shared tag_readings table
                conn.execute(
                    "INSERT INTO tag_readings (tag_id, value) VALUES (?, ?)",
                    (tag_id, value)
                )
                conn.execute(
                    "DELETE FROM tag_readings WHERE tag_id=? AND timestamp < datetime('now', '-7 days')",
                    (tag_id,)
                )

                # Backward compat: dual-write Aquifer Level to aquifer_log so the
                # existing /api/data chart endpoint keeps working without changes.
                if addr_str.strip().upper() in ('DB6.REAL108', 'DB6.DBD108'):
                    conn.execute(
                        "INSERT INTO aquifer_log (level) VALUES (?)",
                        (round(value, 2),)
                    )
                    conn.execute(
                        "DELETE FROM aquifer_log WHERE timestamp < datetime('now', '-60 days')"
                    )

                # Check alarm thresholds
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
                log.error('[%s] Tag "%s" (%s): %s', name, tag_name, addr_str, e)
                # Read failed — the socket is likely dead. Disconnect and evict the
                # client so the next poll cycle reconnects fresh instead of retrying
                # on a broken connection (which would leave stale slots on the PLC).
                try:
                    plc.disconnect()
                except Exception:
                    pass
                plc_clients.pop(dev_id, None)
                break  # no point trying remaining tags on a dead connection

    except Exception as e:
        log.error('[%s] Connection/poll error: %s', name, e)
        st['fail_count'] += 1
        if st['fail_count'] >= OFFLINE_THRESHOLD and not st['offline']:
            st['offline'] = True
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            msg = (f'\ud83d\udd34 COMM LOSS [{name}]\n'
                   f'Siemens S7 device unreachable at {host}:{port}\n'
                   f'Time: {ts}\n'
                   f'Monitoring will resume automatically when it comes back online.')
            send_alarm(msg)
        try:
            plc.disconnect()
        except Exception:
            pass


def main():
    log.info('Siemens S7 monitor starting — poll interval %ds', POLL_INTERVAL)

    db_conn = sqlite3.connect(DB_PATH, isolation_level=None)
    db_conn.execute("PRAGMA foreign_keys = ON")

    # Ensure tables exist (idempotent)
    db_conn.execute('''CREATE TABLE IF NOT EXISTS aquifer_log (
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, level REAL
    )''')
    db_conn.execute('''CREATE TABLE IF NOT EXISTS tag_readings (
        tag_id    INTEGER NOT NULL,
        value     REAL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
    )''')

    plc_clients = {}   # dev_id → snap7.Client, reused across cycles

    while True:
        try:
            devices = load_siemens_devices(db_conn)
            if not devices:
                log.info('No enabled Siemens S7 devices found. Retrying next cycle.')
            for dev, tags in devices:
                poll_device(dev, tags, db_conn, plc_clients)
        except Exception as e:
            log.error('Poll cycle error: %s', e)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
