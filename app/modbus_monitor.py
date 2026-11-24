"""
modbus_monitor.py — polls all Modbus TCP devices configured in the devices/tags DB.

Address format in the tags table (1-indexed, matches SCADA tag configurators):
  HRxxxx  = Holding Register  (FC 03)  — HR4800 → protocol address 4799
  IRxxxx  = Input Register    (FC 04)
  COxxxx  = Coil              (FC 01)
  DIxxxx  = Discrete Input    (FC 02)

Data types:
  INT   – 1 register, signed 16-bit
  WORD  – 1 register, unsigned 16-bit
  DINT  – 2 registers, signed 32-bit
  REAL  – 2 registers, IEEE 754 float
  BOOL  – 1 coil bit

Word order (set per device in the DB):
  word_swap=0  →  high word first (big-endian)  — Modbus standard
  word_swap=1  →  low word first  (swapped)     — some devices (e.g. Byte Swap YES in config)

Writes to tag_readings (tag_id, value, timestamp) — read by /api/device_status.
"""

import time
import datetime
import sqlite3
import struct
import logging
import subprocess
import sys

import requests
import plccc_settings as settings  # project paths + secrets from .env
from pymodbus.client import ModbusTcpClient

DB_PATH = settings.DB_PATH
POLL_INTERVAL     = 60   # seconds
IMSG_GROUP        = 'Recharge Pump Alarm'  # iMessage group chat name
TELEGRAM_TOKEN = settings.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = settings.TELEGRAM_CHAT_ID

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('modbus_monitor')

# In-memory alarm state: {tag_id: {'active': bool}}
alarm_states = {}

# In-memory device connectivity state: {device_id: {'offline': bool, 'fail_count': int}}
device_states = {}

OFFLINE_THRESHOLD = 3   # consecutive poll failures before sending offline alert


def send_telegram(text):
    """Send an alarm message via Telegram Bot API."""
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        resp = requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
        if resp.ok:
            log.info('Telegram alarm sent: %s', text[:80])
        else:
            log.error('Telegram send failed: %s %s', resp.status_code, resp.text[:200])
    except Exception as e:
        log.error('Telegram send error: %s', e)


def send_imessage(text):
    """Send an iMessage to the Recharge Pump Alarm group chat."""
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
        with open('/tmp/modbus_imsg.log', 'a') as f:
            subprocess.Popen(['osascript', '-e', script], stdout=f, stderr=f)
        log.info('iMessage alarm dispatched: %s', text[:80])
    except Exception as e:
        log.error('iMessage send error: %s', e)


def send_alarm(text):
    """Send alarm via both Telegram (reliable) and iMessage (group chat)."""
    send_telegram(text)
    send_imessage(text)


def check_alarm(tag_id, tag_name, device_name, units, value,
                alarm_enabled, alarm_low, alarm_high, alarm_message, conn):
    """Check value against alarm thresholds; send iMessage on state change."""
    if not alarm_enabled:
        return

    # Determine if currently in alarm
    in_alarm = False
    reason   = ''
    if alarm_low is not None and value < alarm_low:
        in_alarm = True
        reason   = f'below low limit of {alarm_low} {units}'
    elif alarm_high is not None and value > alarm_high:
        in_alarm = True
        reason   = f'above high limit of {alarm_high} {units}'

    state = alarm_states.get(tag_id, {'active': False})
    now   = time.time()

    if in_alarm and not state.get('active', False):
        # ── New alarm ──
        msg = alarm_message.strip() if alarm_message and alarm_message.strip() else (
            f'\u26a0\ufe0f ALARM [{device_name}] {tag_name} = {value} {units} — {reason}\n'
            f'(Reply \'ack\' to acknowledge)'
        )
        send_alarm(msg)
        alarm_states[tag_id] = {'active': True}
        conn.execute('INSERT OR REPLACE INTO tag_alarm_state (tag_id,active,triggered_at,last_value) VALUES (?,1,CURRENT_TIMESTAMP,?)',
                     (tag_id, value))
        log.warning('ALARM: %s %s = %s %s (%s)', device_name, tag_name, value, units, reason)

    elif not in_alarm and state.get('active', False):
        # ── Recovery ──
        msg = f'\u2705 CLEAR [{device_name}] {tag_name} = {value} {units} — alarm cleared'
        send_alarm(msg)
        alarm_states[tag_id] = {'active': False}
        conn.execute('INSERT OR REPLACE INTO tag_alarm_state (tag_id,active,triggered_at,last_value) VALUES (?,0,NULL,?)',
                     (tag_id, value))
        log.info('CLEAR: %s %s = %s %s', device_name, tag_name, value, units)


def parse_address(addr_str):
    """
    Return (function_code, protocol_register_address).
    Uses 1-indexed SCADA convention to match tag configurators:
      HR4800 → protocol address 4799  (subtract 1)
      CO5    → protocol address 4
    """
    s = addr_str.strip().upper()
    if s.startswith('HR'):
        return 3, int(s[2:]) - 1
    elif s.startswith('IR'):
        return 4, int(s[2:]) - 1
    elif s.startswith('CO'):
        return 1, int(s[2:]) - 1
    elif s.startswith('DI'):
        return 2, int(s[2:]) - 1
    else:
        return 3, int(s) - 1   # bare number → holding register, 1-indexed


def reg_count(data_type):
    return 2 if data_type.upper() in ('REAL', 'DINT') else 1


def decode_value(registers, data_type, word_swap=False):
    """
    Convert raw Modbus registers to a Python float.
    word_swap=True  → first register received is the LOW word (swap before combining).
    word_swap=False → first register received is the HIGH word (standard big-endian).
    """
    dt = data_type.upper()

    if word_swap and len(registers) >= 2:
        # Device sends [LOW, HIGH] — swap so we always combine as [HIGH, LOW]
        registers = [registers[1], registers[0]]

    if dt == 'REAL':
        raw = (registers[0] << 16) | registers[1]
        return float(struct.unpack('>f', struct.pack('>I', raw))[0])
    elif dt == 'DINT':
        raw = (registers[0] << 16) | registers[1]
        if raw >= 0x80000000:
            raw -= 0x100000000
        return float(raw)
    elif dt == 'INT':
        v = registers[0]
        if v >= 0x8000:
            v -= 0x10000
        return float(v)
    else:
        # WORD, BOOL, default → unsigned 16-bit
        return float(registers[0])


def load_modbus_devices(conn):
    conn.row_factory = sqlite3.Row
    devs = conn.execute(
        "SELECT * FROM devices WHERE type='modbus_tcp' AND enabled=1 ORDER BY id"
    ).fetchall()
    result = []
    for dev in devs:
        tags = conn.execute(
            "SELECT * FROM tags WHERE device_id=? AND enabled=1 ORDER BY id",
            (dev['id'],)
        ).fetchall()
        result.append((dev, tags))
    return result


def poll_device(dev, tags, conn):
    host      = dev['host']
    port      = dev['port'] or 502
    unit_id   = dev['unit_id'] if dev['unit_id'] is not None else 1
    word_swap = bool(dev['word_swap']) if 'word_swap' in dev.keys() else False
    name      = dev['name']

    dev_id = dev['id']
    st = device_states.setdefault(dev_id, {'offline': False, 'fail_count': 0})

    client = ModbusTcpClient(host, port=port, timeout=5)
    if not client.connect():
        log.warning(f'[{name}] Could not connect to {host}:{port}')
        st['fail_count'] += 1
        if st['fail_count'] >= OFFLINE_THRESHOLD and not st['offline']:
            st['offline'] = True
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            msg = (f'🔴 COMM LOSS [{name}]\n'
                   f'Modbus TCP device unreachable at {host}:{port}\n'
                   f'Time: {ts}\n'
                   f'Monitoring will resume automatically when it comes back online.')
            send_alarm(msg)
        return

    # ── Connected successfully ──────────────────────────────────────────────
    if st['offline']:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        msg = f'✅ RECOVERY [{name}]\nModbus TCP device back online at {host}:{port}\nTime: {ts}'
        send_alarm(msg)
    st['offline'] = False
    st['fail_count'] = 0

    log.info(f'[{name}] Connected to {host}:{port} (unit={unit_id}, word_swap={word_swap})')

    for tag in tags:
        tag_id    = tag['id']
        tag_name  = tag['name']
        addr_str  = tag['address']
        data_type = tag['data_type'] or 'INT'

        try:
            fc, reg_addr = parse_address(addr_str)
            count = reg_count(data_type)

            if fc == 3:
                rr = client.read_holding_registers(address=reg_addr, count=count, slave=unit_id)
            elif fc == 4:
                rr = client.read_input_registers(address=reg_addr, count=count, slave=unit_id)
            elif fc == 1:
                rr = client.read_coils(address=reg_addr, count=1, slave=unit_id)
            elif fc == 2:
                rr = client.read_discrete_inputs(address=reg_addr, count=1, slave=unit_id)
            else:
                log.warning(f'[{name}] Unknown FC {fc} for tag {tag_name}')
                continue

            if rr.isError():
                log.warning(f'[{name}] Tag "{tag_name}" ({addr_str}): Modbus error — {rr}')
                continue

            if fc in (1, 2):
                value = float(rr.bits[0])
            else:
                value = decode_value(rr.registers, data_type, word_swap=word_swap)

            value = round(value, 4)
            # Apply per-tag scaling: result = (raw × factor) + offset
            scale_factor = tag['scale_factor'] if 'scale_factor' in tag.keys() and tag['scale_factor'] is not None else 1.0
            scale_offset = tag['scale_offset'] if 'scale_offset' in tag.keys() and tag['scale_offset'] is not None else 0.0
            if scale_factor != 1.0 or scale_offset != 0.0:
                value = round(value * scale_factor + scale_offset, 4)
            log.info(f'[{name}] {tag_name} ({addr_str}) = {value} {tag["units"] or ""}')

            conn.execute(
                "INSERT INTO tag_readings (tag_id, value) VALUES (?, ?)",
                (tag_id, value)
            )
            # Keep only last 7 days of readings per tag
            conn.execute(
                "DELETE FROM tag_readings WHERE tag_id=? AND timestamp < datetime('now', '-7 days')",
                (tag_id,)
            )

            # Check alarm thresholds
            check_alarm(
                tag_id       = tag_id,
                tag_name     = tag_name,
                device_name  = name,
                units        = tag['units'] or '',
                value        = value,
                alarm_enabled= tag['alarm_enabled'],
                alarm_low    = tag['alarm_low'],
                alarm_high   = tag['alarm_high'],
                alarm_message= tag['alarm_message'] or '',
                conn         = conn,
            )

        except Exception as e:
            log.error(f'[{name}] Tag "{tag_name}" ({addr_str}): {e}')

    client.close()


def main():
    log.info('Modbus TCP monitor starting — poll interval %ds', POLL_INTERVAL)

    db_conn = sqlite3.connect(DB_PATH, isolation_level=None)
    db_conn.execute("PRAGMA foreign_keys = ON")
    db_conn.execute('''CREATE TABLE IF NOT EXISTS tag_readings (
        tag_id    INTEGER NOT NULL,
        value     REAL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
    )''')

    while True:
        try:
            devices = load_modbus_devices(db_conn)
            if not devices:
                log.info('No enabled Modbus TCP devices. Retrying next cycle.')
            for dev, tags in devices:
                poll_device(dev, tags, db_conn)
        except Exception as e:
            log.error(f'Poll cycle error: {e}')
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
