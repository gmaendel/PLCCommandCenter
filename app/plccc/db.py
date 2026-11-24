"""SQLite access + additive-only schema migrations.

Compatibility rule: v2 only ever ADDS tables/columns, so the v1 scripts
(pump_monitor.py, modbus_monitor.py, siemens_monitor.py, ab_monitor.py, pump_web.py)
can still run against the same pump_data.db for rollback.
"""
import json
import sqlite3

import plccc_settings as settings

BUSY_TIMEOUT_S = 15

# ── Default alarm/comm profiles for the recharge pump (exact v1 pump_monitor.py text) ──
PUMP_ALARM_CONFIG = {
    'mode': 'escalating',
    'direction': 'low',
    # Threshold lives in data/pump_config.json (threshold_gpm, default 2000) exactly
    # like v1, so the v1 dashboard/monitor stay in sync during a rollback.
    'threshold_source': 'pump_config.threshold_gpm',
    'threshold_default': 2000,
    'escalation_wait_s': 400,
    'cooldown_s': 120,
    'ack': {'imessage_chat': 'Recharge Pump Alarm', 'keywords': []},
    'notify_clear': False,
    'messages': {
        'alarm': "\u26a0\ufe0f ALARM: Recharge Pump GPM is below {threshold} GPM!\nCurrent GPM: {value}\n"
                 "Time: {time}\n\n(Reply to this message to acknowledge)",
        'escalation_1': "\u26a0\ufe0f ESCALATION 1: Recharge Pump still below {threshold}!\nCurrent GPM: {value}\nTime: {time}",
        'final': "\U0001f6a8 FINAL WARNING: Recharge Pump still below {threshold}!\nCurrent GPM: {value}\nTime: {time}",
    },
}

PUMP_COMM_CONFIG = {
    # v1 pump_monitor: 10 consecutive failed reads, then a TCP probe of host:port.
    # Port open -> driver is wedged: rebuild it, reset the count, no alert.
    # Port closed -> one COMM LOSS alert; RECOVERY on the next good read.
    'offline_after': 10,
    'probe_port': True,
    'fail_on_any_tag_error': True,
    'messages': {
        'loss': "\U0001f6a8 COMM LOSS: Cannot reach PLC at {host}:{port}.\nTime: {time}\n\n"
                "Monitoring will resume automatically when it comes back online.",
        'recovery': "\u2705 RECOVERY: Communication to PLC {host}:{port} has been restored!",
    },
}


def connect(path=None):
    conn = sqlite3.connect(path or settings.DB_PATH, timeout=BUSY_TIMEOUT_S, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=%d' % (BUSY_TIMEOUT_S * 1000))
    return conn


def _add_column(conn, table, col, defn):
    cols = {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}
    if col not in cols:
        conn.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, defn))


def migrate(conn):
    """Create/upgrade the schema. Idempotent; additive only."""
    # v1 tables (same DDL as pump_web.init_db / monitors)
    conn.execute('''CREATE TABLE IF NOT EXISTS flow_log (
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, gpm REAL, level REAL)''')
    _add_column(conn, 'flow_log', 'level', 'REAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS aquifer_log (
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, level REAL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, type TEXT NOT NULL,
        host TEXT DEFAULT '', port INTEGER DEFAULT 0, rack INTEGER DEFAULT 0,
        slot INTEGER DEFAULT 1, unit_id INTEGER DEFAULT 1, word_swap INTEGER DEFAULT 0,
        description TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT, device_id INTEGER NOT NULL,
        name TEXT NOT NULL, address TEXT NOT NULL, data_type TEXT DEFAULT 'REAL',
        units TEXT DEFAULT '', description TEXT DEFAULT '',
        alarm_enabled INTEGER DEFAULT 0, alarm_low REAL DEFAULT NULL,
        alarm_high REAL DEFAULT NULL, alarm_message TEXT DEFAULT '',
        scale_factor REAL DEFAULT 1.0, scale_offset REAL DEFAULT 0.0,
        enabled INTEGER DEFAULT 1, log_source TEXT DEFAULT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE)''')
    for col, defn in [('log_source', 'TEXT DEFAULT NULL'), ('word_swap', 'INTEGER DEFAULT 0'),
                      ('alarm_enabled', 'INTEGER DEFAULT 0'), ('alarm_low', 'REAL DEFAULT NULL'),
                      ('alarm_high', 'REAL DEFAULT NULL'), ('alarm_message', "TEXT DEFAULT ''")]:
        _add_column(conn, 'tags', col, defn)
        _add_column(conn, 'devices', col, defn)
    _add_column(conn, 'tags', 'scale_factor', 'REAL DEFAULT 1.0')
    _add_column(conn, 'tags', 'scale_offset', 'REAL DEFAULT 0.0')
    conn.execute('''CREATE TABLE IF NOT EXISTS tag_readings (
        tag_id INTEGER NOT NULL, value REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS tag_alarm_state (
        tag_id INTEGER PRIMARY KEY, active INTEGER DEFAULT 0,
        triggered_at DATETIME DEFAULT NULL, last_value REAL DEFAULT NULL,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE)''')

    # ── v2 additions (ignored by v1 code) ────────────────────────────────────
    _add_column(conn, 'tags', 'alarm_config', 'TEXT DEFAULT NULL')     # JSON, e.g. escalation profile
    _add_column(conn, 'devices', 'comm_config', 'TEXT DEFAULT NULL')   # JSON, offline/probe policy
    _add_column(conn, 'tag_alarm_state', 'stage', 'INTEGER DEFAULT NULL')
    _add_column(conn, 'tag_alarm_state', 'acked', 'INTEGER DEFAULT 0')
    _add_column(conn, 'tag_alarm_state', 'updated_at', 'DATETIME DEFAULT NULL')
    conn.execute('''CREATE TABLE IF NOT EXISTS device_runtime (
        device_id INTEGER PRIMARY KEY, online INTEGER DEFAULT NULL, fail_count INTEGER DEFAULT 0,
        offline_alerted INTEGER DEFAULT 0, last_ok DATETIME DEFAULT NULL,
        last_error TEXT DEFAULT NULL, updated_at DATETIME DEFAULT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS service_status (
        name TEXT PRIMARY KEY, pid INTEGER, started_at DATETIME, heartbeat DATETIME, info TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS alert_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        kind TEXT, source TEXT, text TEXT, telegram TEXT, imessage TEXT, dry_run INTEGER DEFAULT 0)''')
    # v2.2: per-device driver options (OPC UA / MQTT credentials, TLS, security), app settings,
    # and dashboard -> poller requests (the poller has Full Disk Access + Messages automation).
    _add_column(conn, 'devices', 'options', 'TEXT DEFAULT NULL')
    conn.execute('''CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS service_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT,
        status TEXT DEFAULT 'pending', result TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        done_at DATETIME DEFAULT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tag_readings_tag_ts ON tag_readings(tag_id, timestamp)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_flow_log_ts ON flow_log(timestamp)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_aquifer_log_ts ON aquifer_log(timestamp)')

    # v1 backfill of log_source for the seed tags (same statements as pump_web.init_db)
    conn.execute("UPDATE tags SET log_source='{\"table\":\"flow_log\",\"column\":\"gpm\"}' WHERE address='N7:6' AND log_source IS NULL")
    conn.execute("UPDATE tags SET log_source='{\"table\":\"flow_log\",\"column\":\"level\"}' WHERE address='N7:1' AND log_source IS NULL")
    conn.execute("UPDATE tags SET log_source='{\"table\":\"aquifer_log\",\"column\":\"level\"}' WHERE address='DB6.REAL108' AND log_source IS NULL")

    # Turn the recharge pump into a regular tag/device with an escalation profile
    # (only when not configured yet; user edits are never overwritten).
    pump_tags = conn.execute(
        "SELECT t.id, t.device_id FROM tags t JOIN devices d ON d.id=t.device_id "
        "WHERE d.type='allen_bradley_slc' AND t.log_source LIKE '%\"flow_log\"%' AND t.log_source LIKE '%\"gpm\"%'"
    ).fetchall()
    for t in pump_tags:
        conn.execute('UPDATE tags SET alarm_config=? WHERE id=? AND alarm_config IS NULL',
                     (json.dumps(PUMP_ALARM_CONFIG), t['id']))
        conn.execute('UPDATE devices SET comm_config=? WHERE id=? AND comm_config IS NULL',
                     (json.dumps(PUMP_COMM_CONFIG), t['device_id']))


def loads(text, default=None):
    if not text:
        return default
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else default
    except Exception:
        return default


def read_threshold_config(key='threshold_gpm', default=2000):
    """Same semantics as v1 get_threshold(): int from pump_config.json, else default."""
    try:
        with open(settings.CONFIG_PATH, 'r') as f:
            return int(json.load(f).get(key, default))
    except Exception:
        return default


def write_threshold_config(value, key='threshold_gpm'):
    import os
    try:
        cfg = {}
        if os.path.exists(settings.CONFIG_PATH):
            with open(settings.CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        cfg[key] = int(value)
        tmp = settings.CONFIG_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        if os.path.exists(settings.CONFIG_PATH):
            os.chmod(tmp, os.stat(settings.CONFIG_PATH).st_mode & 0o777)
        os.replace(tmp, settings.CONFIG_PATH)
        return True
    except Exception:
        return False


# ── app settings (key/value) ─────────────────────────────────────────────────
def get_setting(key, default=None, conn=None, db_path=None):
    own = conn is None
    try:
        if own:
            conn = connect(db_path)
        row = conn.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
        return row[0] if row and row[0] not in (None, '') else default
    except sqlite3.Error:
        return default
    finally:
        if own and conn is not None:
            conn.close()


def set_setting(key, value, conn=None, db_path=None):
    own = conn is None
    try:
        if own:
            conn = connect(db_path)
        if value is None:
            conn.execute('DELETE FROM app_settings WHERE key=?', (key,))
        else:
            conn.execute('INSERT INTO app_settings (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                         (key, str(value)))
    finally:
        if own and conn is not None:
            conn.close()


def imessage_chat(db_path=None, conn=None):
    """The iMessage group used for BOTH sending alarms and reading ack replies.
    Settings page value (app_settings.imessage_chat) if set, else PLCCC_IMESSAGE_CHAT ('Recharge Pump Alarm')."""
    return get_setting('imessage_chat', None, conn=conn, db_path=db_path) or settings.IMESSAGE_CHAT
