"""PLCCommandCenter v2 dashboard.

Every v1 route (pump_web.py) is kept with the same request/response shape:
  GET  /                      new mobile-first UI (v1 UI still at /classic)
  GET/POST /api/threshold     POST {threshold_gpm, password}
  GET  /api/data              ?tab=pump|aquifer &hours=1|6|24|168|1440 | &start=&end= (America/Chicago)
  GET  /api/device_status
  GET/POST /api/devices, PUT/DELETE /api/devices/<id>
  GET/POST /api/tags,    PUT/DELETE /api/tags/<id>
New (additive):
  GET  /api/overview, /api/tag_history, /api/alerts, /healthz, /manifest.webmanifest
  POST /api/auth/check, /api/ack, /api/tags/<id>/alarm
  GET/PUT/DELETE /api/overview_layout   (customizable Overview cards; stored in data/overview_layout.json)
  GET/POST /api/settings/imessage, POST /api/settings/imessage/check|test, GET /api/requests/<id>
  GET/POST /api/settings/telegram, POST /api/settings/telegram/find|test   (v2.3; token never returned in full)
"""
import ast
import datetime
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import zoneinfo

from flask import Flask, jsonify, render_template, render_template_string, request, send_from_directory

import plccc_settings as settings
from . import __version__
from . import db as dbmod
from . import telegram as tgmod

log = logging.getLogger('plccc.web')
HERE = os.path.dirname(os.path.abspath(__file__))
TZ = zoneinfo.ZoneInfo('America/Chicago')
STAGE_LABELS = {1: 'Alarm', 2: 'Escalation 1', 3: 'Final warning', 4: 'Acknowledged'}
TYPE_LABELS = {'allen_bradley_slc': 'AB SLC/MicroLogix', 'allen_bradley_clx': 'AB ControlLogix',
               'siemens_s7': 'Siemens S7', 'modbus_tcp': 'Modbus TCP', 'opc_ua': 'OPC UA', 'mqtt': 'MQTT'}
OPTION_KEYS = {'opc_ua': ('username', 'password', 'security_policy', 'security_mode'),
               'mqtt': ('username', 'password', 'tls', 'tls_insecure', 'client_id', 'stale_after')}
OPCUA_POLICIES = ('None', 'Basic256Sha256', 'Aes128Sha256RsaOaep', 'Aes256Sha256RsaPss')
IMESSAGE_TEST_COOLDOWN_S = 30
TELEGRAM_TEST_COOLDOWN_S = 30
TELEGRAM_FIND_COOLDOWN_S = 3
_tg_lock = threading.Lock()
_tg_last = {}           # 'test'/'find' -> time.time() of last call

app = Flask(__name__, static_folder=os.path.join(HERE, 'static'), template_folder=os.path.join(HERE, 'templates'))
app.config['JSON_SORT_KEYS'] = False
app.json.sort_keys = False


def get_conn():
    return dbmod.connect()


def _is_v1_database(conn):
    if settings.SEED_V1_DEVICES:
        return True
    for table in ('flow_log', 'aquifer_log'):
        try:
            if conn.execute('SELECT 1 FROM %s LIMIT 1' % table).fetchone():
                return True
        except sqlite3.Error:
            pass
    return False


def init_db():
    conn = get_conn()
    try:
        dbmod.migrate(conn)
        # v1 seed on an empty database (same rows as pump_web.init_db) - only for an upgraded v1 database
        # (it already has pump/aquifer history) or when PLCCC_SEED_V1_DEVICES=1. Fresh installs start empty.
        if conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0] == 0 and _is_v1_database(conn):
            conn.execute("""INSERT INTO devices (name,type,host,port,description) VALUES
                ('Recharge Pump (MicroLogix)','allen_bradley_slc','pump-plc.example.com',44818,
                 'Allen-Bradley MicroLogix – recharge pump flow monitor')""")
            mlx = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.executemany('INSERT INTO tags (device_id,name,address,data_type,units,description,log_source) VALUES (?,?,?,?,?,?,?)',
                             [(mlx, 'Pump GPM', 'N7:6', 'INT', 'GPM', 'Recharge pump flow rate', '{"table":"flow_log","column":"gpm"}'),
                              (mlx, 'Water Level', 'N7:1', 'INT', '', 'Water level sensor reading', '{"table":"flow_log","column":"level"}')])
            conn.execute("""INSERT INTO devices (name,type,host,port,rack,slot,description) VALUES
                ('Aquifer Monitor (S7-1200)','siemens_s7','pump-plc.example.com',102,0,1,'Siemens S7-1200 – aquifer level sensor')""")
            s7 = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute('INSERT INTO tags (device_id,name,address,data_type,units,description,log_source) VALUES (?,?,?,?,?,?,?)',
                         (s7, 'Aquifer Level', 'DB6.REAL108', 'REAL', 'ft', 'Aquifer depth measurement',
                          '{"table":"aquifer_log","column":"level"}'))
            dbmod.migrate(conn)
    finally:
        conn.close()


# ── auth ─────────────────────────────────────────────────────────────────────
_fail_lock = threading.Lock()
_failures = {}          # ip -> [timestamps]
MAX_FAILS, FAIL_WINDOW = 20, 600


def _client_ip():
    return request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For', '').split(',')[0].strip() \
        or request.remote_addr or '?'


def check_password(data):
    """True if data['password'] matches DASHBOARD_PASSWORD. Constant-time; >20 failures in 10 min -> locked."""
    ip = _client_ip()
    now = time.time()
    with _fail_lock:
        lst = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW]
        _failures[ip] = lst
        if len(lst) >= MAX_FAILS:
            return None
    pw = (data or {}).get('password')
    ok = isinstance(pw, str) and hmac.compare_digest(pw.encode(), settings.DASHBOARD_PASSWORD.encode())
    if not ok:
        with _fail_lock:
            _failures.setdefault(ip, []).append(now)
    return ok


def unauthorized(result):
    if result is None:
        return jsonify({'error': 'Too many attempts, try again later'}), 429
    return jsonify({'error': 'Unauthorized'}), 401


# ── v1 helpers ───────────────────────────────────────────────────────────────
def get_threshold():
    return dbmod.read_threshold_config('threshold_gpm', 2000)


def _range_clause(hours, start, end, allowed=(1, 6, 24, 168, 1440)):
    """Returns (sql_where, params) on the UTC `timestamp` column, v1 semantics."""
    if start and end:
        try:
            s = datetime.datetime.strptime(start, '%Y-%m-%dT%H:%M').replace(tzinfo=TZ)
            e = datetime.datetime.strptime(end, '%Y-%m-%dT%H:%M').replace(tzinfo=TZ)
            fmt = '%Y-%m-%d %H:%M:%S'
            return 'timestamp>=? AND timestamp<=?', (s.astimezone(datetime.timezone.utc).strftime(fmt),
                                                     e.astimezone(datetime.timezone.utc).strftime(fmt))
        except ValueError:
            return "timestamp>=datetime('now','-1 hours')", ()
    if hours not in allowed:
        hours = 1
    return "timestamp>=datetime('now','-%d hours')" % hours, ()


# ════════════════════════════════════════════════════════════ PAGES
@app.route('/')
def index():
    return render_template('index.html', version=__version__, dry_run=settings.DRY_RUN)


_classic_html = None


@app.route('/classic')
def classic():
    """The v1 single-page dashboard, unchanged (it talks to the same API)."""
    global _classic_html
    if _classic_html is None:
        with open(os.path.join(os.path.dirname(HERE), 'pump_web.py'), encoding='utf-8') as f:
            src = f.read()
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == 'HTML_TEMPLATE' for t in node.targets):
                _classic_html = node.value.value
        _classic_html = _classic_html or '<p>classic UI unavailable</p>'
    return render_template_string(_classic_html)


@app.route('/manifest.webmanifest')
def manifest():
    resp = send_from_directory(app.static_folder, 'manifest.webmanifest', mimetype='application/manifest+json')
    return resp


@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
def apple_icon():
    return send_from_directory(os.path.join(app.static_folder, 'icons'), 'apple-touch-icon.png')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.static_folder, 'icons'), 'icon-192.png', mimetype='image/png')


@app.after_request
def _headers(resp):
    if request.path.startswith('/api/') or request.path == '/healthz':
        resp.headers['Cache-Control'] = 'no-store'
    elif request.path.startswith('/static/'):
        resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


# ════════════════════════════════════════════════════════════ v1 API (unchanged contracts)
@app.route('/api/threshold', methods=['GET', 'POST'])
def threshold():
    if request.method == 'POST':
        data = request.get_json(silent=True)
        auth = check_password(data)
        if not auth:
            return unauthorized(auth)
        val = data.get('threshold_gpm')
        if val is None or not str(val).isdigit():
            return jsonify({'error': 'Invalid value'}), 400
        if dbmod.write_threshold_config(val):
            # keep the Pump GPM tag's displayed low limit in sync (v2 cosmetic; v1 ignores it)
            try:
                conn = get_conn()
                conn.execute("UPDATE tags SET alarm_low=?, updated_at=CURRENT_TIMESTAMP "
                             "WHERE alarm_config LIKE '%pump_config.threshold_gpm%'", (int(val),))
                conn.close()
            except Exception as e:
                log.warning('could not sync alarm_low: %s', e)
            return jsonify({'threshold_gpm': int(val)})
        return jsonify({'error': 'Failed to save'}), 500
    return jsonify({'threshold_gpm': get_threshold()})


@app.route('/api/data')
def get_data():
    tab = request.args.get('tab', default='pump')
    hours = request.args.get('hours', default=1, type=int)
    start = request.args.get('start')
    end = request.args.get('end')
    try:
        conn = get_conn()
        table = 'flow_log' if tab == 'pump' else 'aquifer_log'
        qcols = 'timestamp,gpm,level' if tab == 'pump' else 'timestamp,level as y'
        where, params = _range_clause(hours, start, end)
        rows = conn.execute('SELECT %s FROM %s WHERE %s ORDER BY timestamp ASC' % (qcols, table, where), params).fetchall()
        if tab == 'pump':
            data = [{'x': r['timestamp'] + 'Z', 'y': r['gpm'], 'level': r['level']} for r in rows]
        else:
            data = [{'x': r['timestamp'] + 'Z', 'y': r['y']} for r in rows]
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _latest_for_tag(conn, tag):
    value = timestamp = None
    if tag['log_source']:
        try:
            src = json.loads(tag['log_source'])
            tbl, col = src['table'], src['column']
            if tbl in ('flow_log', 'aquifer_log') and col.replace('_', '').isalpha():
                row = conn.execute('SELECT %s,timestamp FROM %s ORDER BY timestamp DESC LIMIT 1' % (col, tbl)).fetchone()
                if row:
                    value, timestamp = row[col], row['timestamp']
        except Exception:
            pass
    if value is None:
        row = conn.execute('SELECT value,timestamp FROM tag_readings WHERE tag_id=? ORDER BY timestamp DESC LIMIT 1',
                           (tag['id'],)).fetchone()
        if row:
            value, timestamp = row['value'], row['timestamp']
    return value, timestamp


_WEB_STARTED = time.time()
_BOOT_TIME = None


def _boot_time():
    """Mac boot time (epoch seconds) from sysctl kern.boottime; cached."""
    global _BOOT_TIME
    if _BOOT_TIME is None:
        try:
            import subprocess, re as _re
            out = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], capture_output=True,
                                 text=True, timeout=3).stdout
            m = _re.search(r'sec\s*=\s*(\d+)', out)
            _BOOT_TIME = float(m.group(1)) if m else 0.0
        except Exception:
            _BOOT_TIME = 0.0
    return _BOOT_TIME or None


def _uptimes(poller):
    now = time.time()
    poll_s = None
    try:
        if poller.get('alive') and poller.get('started_at'):
            st = datetime.datetime.strptime(poller['started_at'], '%Y-%m-%d %H:%M:%S').replace(
                tzinfo=datetime.timezone.utc).timestamp()
            poll_s = max(0, int(now - st))
    except Exception:
        poll_s = None
    bt = _boot_time()
    return {'mac': int(now - bt) if bt else None, 'poller': poll_s, 'dashboard': int(now - _WEB_STARTED)}


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _is_recent(ts, seconds=180):
    try:
        t = datetime.datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        return (_utcnow() - t).total_seconds() < seconds
    except Exception:
        return False


def _runtime_map(conn):
    try:
        return {r['device_id']: dict(r) for r in conn.execute('SELECT * FROM device_runtime')}
    except sqlite3.Error:
        return {}


@app.route('/api/device_status')
def device_status():
    try:
        conn = get_conn()
        runtime = _runtime_map(conn)
        result = {}
        for dev in conn.execute('SELECT * FROM devices ORDER BY id').fetchall():
            tags = conn.execute('SELECT id,name,units,address,log_source,alarm_enabled,alarm_low,alarm_high '
                                'FROM tags WHERE device_id=? ORDER BY id', (dev['id'],)).fetchall()
            tag_status, stamps = {}, []
            for tag in tags:
                value, ts = _latest_for_tag(conn, tag)
                if ts:
                    stamps.append(ts)
                arow = conn.execute('SELECT active FROM tag_alarm_state WHERE tag_id=?', (tag['id'],)).fetchone()
                tag_status[str(tag['id'])] = {'name': tag['name'], 'units': tag['units'] or '', 'value': value,
                                              'timestamp': ts, 'alarm_active': bool(arow['active']) if arow else False}
            last_seen = max(stamps) if stamps else None
            result[str(dev['id'])] = {'connected': _is_recent(last_seen) if last_seen else False,
                                      'last_seen': last_seen, 'tags': tag_status,
                                      'runtime': runtime.get(dev['id'])}          # v2: additive key
        conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ── device options (v2.2: OPC UA / MQTT) ─────────────────────────────────────
def public_device(row):
    """Device dict for the API: options parsed, password never returned."""
    d = dict(row)
    opts = dbmod.loads(d.pop('options', None), {}) or {}
    d['has_password'] = bool(opts.get('password'))
    opts.pop('password', None)
    d['options'] = opts
    return d


def build_device(data, existing=None):
    """Validates the device body; returns (fields, options_json) or raises ValueError."""
    typ = data.get('type')
    if typ not in TYPE_LABELS:
        raise ValueError('Unsupported device type')
    host = str(data.get('host') or '').strip()
    port = data.get('port') or 0
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError('Port must be a number')
    old = dbmod.loads(existing['options'], {}) if existing is not None and 'options' in existing.keys() else {}
    old = old or {}
    raw = data.get('options') or {}
    if not isinstance(raw, dict):
        raise ValueError('options must be an object')
    opts = {}
    for k in OPTION_KEYS.get(typ, ()):
        if k in raw and raw[k] not in (None, ''):
            opts[k] = raw[k]
    if typ in OPTION_KEYS:
        if not opts.get('username'):
            opts.pop('password', None)
        elif 'password' not in opts and old.get('password') and old.get('username') == opts.get('username') \
                and not raw.get('clear_password'):
            opts['password'] = old['password']       # blank password field = keep the saved one
    if typ == 'opc_ua':
        from urllib.parse import urlparse
        if not host.lower().startswith('opc.tcp://'):
            host = 'opc.tcp://%s:%s' % (host, port or 4840)
        u = urlparse(host)
        if not u.hostname:
            raise ValueError('Endpoint URL must look like opc.tcp://192.168.1.50:4840')
        port = u.port or 4840
        pol = opts.get('security_policy') or 'None'
        if pol not in OPCUA_POLICIES:
            raise ValueError('Unsupported security policy')
        if pol == 'None':
            opts.pop('security_policy', None)
            opts.pop('security_mode', None)
        elif opts.get('security_mode', 'SignAndEncrypt') not in ('Sign', 'SignAndEncrypt'):
            raise ValueError('Security mode must be Sign or SignAndEncrypt')
    if typ == 'mqtt':
        opts['tls'] = bool(opts.get('tls'))
        opts['tls_insecure'] = bool(opts.get('tls_insecure'))
        if not opts['tls_insecure']:
            opts.pop('tls_insecure')
        if 'stale_after' in opts:
            try:
                opts['stale_after'] = max(10, int(float(opts['stale_after'])))
            except (TypeError, ValueError):
                raise ValueError('Stale after must be a number of seconds')
        if not port:
            port = 8883 if opts['tls'] else 1883
        if opts.get('client_id') and len(str(opts['client_id'])) > 64:
            raise ValueError('Client ID is too long')
    return host, port, (json.dumps(opts) if opts else None)

@app.route('/api/devices', methods=['GET'])
def list_devices():
    try:
        conn = get_conn()
        result = []
        for d in conn.execute('SELECT * FROM devices ORDER BY id').fetchall():
            dd = public_device(d)
            dd['tags'] = [dict(t) for t in conn.execute('SELECT * FROM tags WHERE device_id=? ORDER BY id', (d['id'],))]
            result.append(dd)
        conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/devices', methods=['POST'])
def create_device():
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    if not data.get('name') or not data.get('type') or not data.get('host'):
        return jsonify({'error': 'name, type, and host are required'}), 400
    try:
        host, port, options = build_device(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    try:
        conn = get_conn()
        cur = conn.execute('INSERT INTO devices (name,type,host,port,rack,slot,unit_id,word_swap,description,enabled,options) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (data['name'], data['type'], host, port, data.get('rack', 0),
                            data.get('slot', 1), data.get('unit_id', 1), data.get('word_swap', 0),
                            data.get('description', ''), 1 if data.get('enabled', True) else 0, options))
        dev = public_device(conn.execute('SELECT * FROM devices WHERE id=?', (cur.lastrowid,)).fetchone())
        dev['tags'] = []
        conn.close()
        return jsonify(dev), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>', methods=['PUT'])
def update_device(device_id):
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        conn = get_conn()
        existing = conn.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({'error': 'Not found'}), 404
        try:
            host, port, options = build_device(data, existing)
        except ValueError as e:
            conn.close()
            return jsonify({'error': str(e)}), 400
        conn.execute('UPDATE devices SET name=?,type=?,host=?,port=?,rack=?,slot=?,unit_id=?,word_swap=?,description=?,'
                     'enabled=?,options=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                     (data['name'], data['type'], host, port, data.get('rack', 0),
                      data.get('slot', 1), data.get('unit_id', 1), data.get('word_swap', 0), data.get('description', ''),
                      1 if data.get('enabled', True) else 0, options, device_id))
        row = conn.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
        conn.close()
        return jsonify(public_device(row))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/devices/<int:device_id>', methods=['DELETE'])
def delete_device(device_id):
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        conn = get_conn()
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('DELETE FROM devices WHERE id=?', (device_id,))
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags', methods=['GET'])
def list_tags():
    device_id = request.args.get('device_id', type=int)
    try:
        conn = get_conn()
        if device_id:
            rows = conn.execute('SELECT * FROM tags WHERE device_id=? ORDER BY id', (device_id,)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM tags ORDER BY device_id,id').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags', methods=['POST'])
def create_tag():
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    if not data.get('device_id') or not data.get('name') or not data.get('address'):
        return jsonify({'error': 'device_id, name, and address are required'}), 400
    try:
        conn = get_conn()
        cur = conn.execute('INSERT INTO tags (device_id,name,address,data_type,units,description,alarm_enabled,alarm_low,'
                           'alarm_high,alarm_message,scale_factor,scale_offset,enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (data['device_id'], data['name'], data['address'], data.get('data_type', 'REAL'),
                            data.get('units', ''), data.get('description', ''), data.get('alarm_enabled', 0),
                            data.get('alarm_low'), data.get('alarm_high'), data.get('alarm_message', ''),
                            data.get('scale_factor', 1.0), data.get('scale_offset', 0.0),
                            1 if data.get('enabled', True) else 0))
        tag = dict(conn.execute('SELECT * FROM tags WHERE id=?', (cur.lastrowid,)).fetchone())
        conn.close()
        return jsonify(tag), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/<int:tag_id>', methods=['PUT'])
def update_tag(tag_id):
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        conn = get_conn()
        conn.execute('UPDATE tags SET name=?,address=?,data_type=?,units=?,description=?,alarm_enabled=?,alarm_low=?,'
                     'alarm_high=?,alarm_message=?,scale_factor=?,scale_offset=?,enabled=?,updated_at=CURRENT_TIMESTAMP '
                     'WHERE id=?',
                     (data['name'], data['address'], data.get('data_type', 'REAL'), data.get('units', ''),
                      data.get('description', ''), data.get('alarm_enabled', 0), data.get('alarm_low'),
                      data.get('alarm_high'), data.get('alarm_message', ''), data.get('scale_factor', 1.0),
                      data.get('scale_offset', 0.0), 1 if data.get('enabled', True) else 0, tag_id))
        row = conn.execute('SELECT * FROM tags WHERE id=?', (tag_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/<int:tag_id>', methods=['DELETE'])
def delete_tag(tag_id):
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        conn = get_conn()
        conn.execute('DELETE FROM tags WHERE id=?', (tag_id,))
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ════════════════════════════════════════════════════════════ v2 API (additive)
def _poller_status(conn):
    try:
        row = conn.execute("SELECT *, (julianday('now')-julianday(heartbeat))*86400.0 AS age "
                           "FROM service_status WHERE name='poller'").fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return {'alive': None, 'heartbeat': None, 'age_s': None, 'info': {}}
    info = dbmod.loads(row['info'], {}) or {}
    return {'alive': row['age'] is not None and row['age'] < 90, 'heartbeat': row['heartbeat'],
            'age_s': round(row['age'] or 0, 1), 'pid': row['pid'], 'started_at': row['started_at'],
            'dry_run': info.get('dry_run'), 'info': info}


@app.route('/api/overview')
def overview():
    try:
        conn = get_conn()
        runtime = _runtime_map(conn)
        alarm_rows = {r['tag_id']: dict(r) for r in conn.execute('SELECT * FROM tag_alarm_state')}
        thr = get_threshold()
        devices, active = [], []
        for dev in conn.execute('SELECT * FROM devices ORDER BY id').fetchall():
            tags_out, stamps = [], []
            for tag in conn.execute('SELECT * FROM tags WHERE device_id=? ORDER BY id', (dev['id'],)).fetchall():
                value, ts = _latest_for_tag(conn, tag)
                if ts:
                    stamps.append(ts)
                a = alarm_rows.get(tag['id']) or {}
                cfg = dbmod.loads(tag['alarm_config'] if 'alarm_config' in tag.keys() else None, {}) or {}
                escalating = cfg.get('mode') == 'escalating'
                low, high = tag['alarm_low'], tag['alarm_high']
                if escalating and str(cfg.get('threshold_source', '')).startswith('pump_config.'):
                    low = thr
                t = {'id': tag['id'], 'name': tag['name'], 'address': tag['address'], 'data_type': tag['data_type'],
                     'units': tag['units'] or '', 'enabled': bool(tag['enabled']), 'value': value, 'timestamp': ts,
                     'stale': not _is_recent(ts) if ts else True,
                     'alarm_enabled': bool(tag['alarm_enabled']), 'alarm_low': low, 'alarm_high': high,
                     'escalating': escalating, 'alarm_active': bool(a.get('active')),
                     'alarm_stage': a.get('stage'), 'alarm_stage_label': STAGE_LABELS.get(a.get('stage')),
                     'acked': bool(a.get('acked')), 'triggered_at': a.get('triggered_at'),
                     }
                tags_out.append(t)
                if t['alarm_active'] and t['alarm_enabled']:
                    active.append({'device': dev['name'], 'device_id': dev['id'], **t})
            last_seen = max(stamps) if stamps else None
            rt = runtime.get(dev['id']) or {}
            devices.append({'id': dev['id'], 'name': dev['name'], 'type': dev['type'],
                            'type_label': TYPE_LABELS.get(dev['type'], dev['type']), 'host': dev['host'],
                            'port': dev['port'], 'enabled': bool(dev['enabled']), 'description': dev['description'],
                            'connected': _is_recent(last_seen) if last_seen else False, 'last_seen': last_seen,
                            'runtime': rt, 'tags': tags_out})
        alerts = [dict(r) for r in conn.execute('SELECT id,timestamp,kind,source,text,telegram,imessage,dry_run '
                                                'FROM alert_log ORDER BY id DESC LIMIT 10')]
        poller = _poller_status(conn)
        out = {'server_time': _utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'version': __version__,
               'dry_run': settings.DRY_RUN, 'threshold_gpm': thr, 'poller': poller, 'uptime': _uptimes(poller),
               'active_alarms': active, 'devices': devices, 'recent_alerts': alerts,
               'ack_pending': os.path.exists(settings.ACK_FILE),
               'layout': _layout_payload(conn)}
        conn.close()
        return jsonify(out)
    except Exception as e:
        log.exception('overview failed')
        return jsonify({'error': str(e)}), 500


# ── customizable Overview layout ─────────────────────────────────────────────
# A layout is an ordered list of cards: {"type": "tile"|"chart", "tag_id": int, "hours": 1|6|24 (chart only)}.
# No saved file -> the default layout, which is exactly the classic Overview (escalating tag as a chart card,
# then a tile for every other enabled tag), computed live so new tags still show up until the user customizes it.
CARD_TYPES = ('tile', 'chart')
CHART_HOURS = (1, 6, 24)
MAX_CARDS = 60
_layout_lock = threading.Lock()


def layout_path():
    return os.path.expanduser(os.environ.get('PLCCC_LAYOUT_PATH') or
                              os.path.join(os.path.dirname(settings.DB_PATH), 'overview_layout.json'))


def default_layout(conn):
    rows = conn.execute('SELECT t.id, t.enabled, t.alarm_config FROM tags t JOIN devices d ON d.id=t.device_id '
                        'ORDER BY d.id, t.id').fetchall()
    esc = [r['id'] for r in rows if (dbmod.loads(r['alarm_config'], {}) or {}).get('mode') == 'escalating']
    cards = [{'type': 'chart', 'tag_id': esc[0], 'hours': 6}] if esc else []
    cards += [{'type': 'tile', 'tag_id': r['id']} for r in rows if r['enabled'] and r['id'] not in esc]
    return cards


def read_saved_layout():
    try:
        with open(layout_path(), encoding='utf-8') as f:
            data = json.load(f)
        cards = data.get('cards') if isinstance(data, dict) else None
        return validate_cards(cards) if cards is not None else None
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning('ignoring unreadable overview layout: %s', e)
        return None


def validate_cards(cards, known_ids=None):
    """Normalizes a card list; raises ValueError with a readable message."""
    if not isinstance(cards, list):
        raise ValueError('cards must be a list')
    if len(cards) > MAX_CARDS:
        raise ValueError('too many cards (max %d)' % MAX_CARDS)
    out = []
    for i, c in enumerate(cards):
        if not isinstance(c, dict):
            raise ValueError('card %d is not an object' % (i + 1))
        typ = c.get('type')
        if typ not in CARD_TYPES:
            raise ValueError('card %d: type must be tile or chart' % (i + 1))
        tid = c.get('tag_id')
        if isinstance(tid, bool) or not isinstance(tid, int) or tid <= 0:
            raise ValueError('card %d: tag_id must be a positive integer' % (i + 1))
        if known_ids is not None and tid not in known_ids:
            raise ValueError('card %d: tag %d does not exist' % (i + 1, tid))
        card = {'type': typ, 'tag_id': tid}
        if typ == 'chart':
            h = c.get('hours', 6)
            if h not in CHART_HOURS:
                raise ValueError('card %d: hours must be one of %s' % (i + 1, ', '.join(map(str, CHART_HOURS))))
            card['hours'] = h
        out.append(card)
    return out


def _layout_payload(conn):
    saved = read_saved_layout()
    return {'customized': saved is not None, 'cards': saved if saved is not None else default_layout(conn)}


@app.route('/api/overview_layout', methods=['GET'])
def get_overview_layout():
    try:
        conn = get_conn()
        out = _layout_payload(conn)
        out['default'] = default_layout(conn)
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/overview_layout', methods=['PUT', 'POST'])
def save_overview_layout():
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        conn = get_conn()
        known = {r[0] for r in conn.execute('SELECT id FROM tags')}
        try:
            cards = validate_cards((data or {}).get('cards'), known)
        except ValueError as e:
            conn.close()
            return jsonify({'error': str(e)}), 400
        path = layout_path()
        with _layout_lock:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'saved_at': _utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'cards': cards}, f, indent=1)
            os.replace(tmp, path)
        log.info('overview layout saved (%d cards) from %s', len(cards), _client_ip())
        out = _layout_payload(conn)
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/overview_layout', methods=['DELETE'])
def reset_overview_layout():
    auth = check_password(request.get_json(silent=True))
    if not auth:
        return unauthorized(auth)
    try:
        with _layout_lock:
            try:
                os.remove(layout_path())
            except FileNotFoundError:
                pass
        log.info('overview layout reset to default from %s', _client_ip())
        conn = get_conn()
        out = _layout_payload(conn)
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _downsample(rows, max_points):
    """Min/max bucket downsampling: keeps short dips/spikes visible on long ranges."""
    n = len(rows)
    if n <= max_points:
        return rows
    buckets = max_points // 2
    size = n / float(buckets)
    out = []
    for b in range(buckets):
        chunk = rows[int(b * size):int((b + 1) * size)]
        if not chunk:
            continue
        vals = [r for r in chunk if r[1] is not None]
        if not vals:
            continue
        lo = min(vals, key=lambda r: r[1])
        hi = max(vals, key=lambda r: r[1])
        out.extend(sorted({lo, hi}, key=lambda r: r[0]))
    return out


@app.route('/api/tag_history')
def tag_history():
    tag_id = request.args.get('tag_id', type=int)
    hours = request.args.get('hours', default=24, type=int)
    max_points = max(100, min(request.args.get('max_points', default=1500, type=int), 10000))
    try:
        conn = get_conn()
        tag = conn.execute('SELECT * FROM tags WHERE id=?', (tag_id,)).fetchone()
        if not tag:
            conn.close()
            return jsonify({'error': 'Not found'}), 404
        where, params = _range_clause(hours, request.args.get('start'), request.args.get('end'))
        src = dbmod.loads(tag['log_source'])
        if src and src.get('table') in ('flow_log', 'aquifer_log') and src.get('column') in ('gpm', 'level'):
            sql = 'SELECT timestamp, %s FROM %s WHERE %s ORDER BY timestamp ASC' % (src['column'], src['table'], where)
            rows = conn.execute(sql, params).fetchall()
            source = src['table']
        else:
            rows = conn.execute('SELECT timestamp, value FROM tag_readings WHERE tag_id=? AND %s ORDER BY timestamp ASC'
                                % where, (tag_id,) + tuple(params)).fetchall()
            source = 'tag_readings'
        conn.close()
        pts = [(r[0], r[1]) for r in rows]
        vals = [v for _, v in pts if v is not None]
        stats = {'count': len(pts), 'min': min(vals) if vals else None, 'max': max(vals) if vals else None,
                 'avg': round(sum(vals) / len(vals), 3) if vals else None, 'last': vals[-1] if vals else None}
        ds = _downsample(pts, max_points)
        return jsonify({'tag': {'id': tag['id'], 'name': tag['name'], 'units': tag['units'] or ''}, 'source': source,
                        'stats': stats, 'downsampled': len(ds) < len(pts),
                        'points': [{'x': t + 'Z', 'y': v} for t, v in ds]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/alerts')
def alerts():
    limit = max(1, min(request.args.get('limit', default=50, type=int), 500))
    try:
        conn = get_conn()
        rows = [dict(r) for r in conn.execute('SELECT * FROM alert_log ORDER BY id DESC LIMIT ?', (limit,))]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ── iMessage group setting (v2.2) ────────────────────────────────────────────
def _request_row(conn, rid):
    r = conn.execute('SELECT * FROM service_requests WHERE id=?', (rid,)).fetchone()
    if not r:
        return None
    return {'id': r['id'], 'kind': r['kind'], 'status': r['status'], 'created_at': r['created_at'],
            'done_at': r['done_at'], 'payload': json.loads(r['payload'] or '{}'),
            'result': json.loads(r['result']) if r['result'] else None}


def _enqueue(conn, kind, payload):
    cur = conn.execute('INSERT INTO service_requests (kind,payload) VALUES (?,?)', (kind, json.dumps(payload)))
    return cur.lastrowid


def _imessage_state(conn):
    name = dbmod.imessage_chat(conn=conn)
    out = {'chat_name': name, 'default': settings.IMESSAGE_CHAT, 'is_default': name == settings.IMESSAGE_CHAT,
           'check': None, 'test': None}
    for kind in ('imessage_check', 'imessage_test'):
        for r in conn.execute('SELECT id FROM service_requests WHERE kind=? ORDER BY id DESC LIMIT 5', (kind,)):
            row = _request_row(conn, r['id'])
            if (row['payload'].get('chat_name') or '').lower() == name.lower():
                out['check' if kind == 'imessage_check' else 'test'] = row
                break
    return out


def validate_chat_name(v):
    if not isinstance(v, str):
        raise ValueError('Chat name is required')
    v = v.strip()
    if not v:
        raise ValueError('Chat name is required')
    if len(v) > 100:
        raise ValueError('Chat name is too long (max 100 characters)')
    if any(ord(ch) < 32 for ch in v):
        raise ValueError('Chat name cannot contain control characters')
    return v


@app.route('/api/settings/imessage', methods=['GET'])
def get_imessage_setting():
    try:
        conn = get_conn()
        out = _imessage_state(conn)
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings/imessage', methods=['POST'])
def set_imessage_setting():
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        name = validate_chat_name((data or {}).get('chat_name'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    try:
        conn = get_conn()
        dbmod.set_setting('imessage_chat', None if name == settings.IMESSAGE_CHAT else name, conn=conn)
        rid = _enqueue(conn, 'imessage_check', {'chat_name': name})
        log.info('iMessage alarm group set to %r from %s', name, _client_ip())
        out = _imessage_state(conn)
        out['request_id'] = rid
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings/imessage/check', methods=['POST'])
def check_imessage_chat():
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        conn = get_conn()
        name = dbmod.imessage_chat(conn=conn)
        rid = _enqueue(conn, 'imessage_check', {'chat_name': name})
        conn.close()
        return jsonify({'request_id': rid, 'chat_name': name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings/imessage/test', methods=['POST'])
def test_imessage_chat():
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        conn = get_conn()
        recent = conn.execute("SELECT COUNT(*) FROM service_requests WHERE kind='imessage_test' "
                              "AND created_at > datetime('now', ?)", ('-%d seconds' % IMESSAGE_TEST_COOLDOWN_S,)).fetchone()[0]
        if recent:
            conn.close()
            return jsonify({'error': 'A test was just sent - wait %ds' % IMESSAGE_TEST_COOLDOWN_S}), 429
        name = dbmod.imessage_chat(conn=conn)
        rid = _enqueue(conn, 'imessage_test', {'chat_name': name})
        log.info('iMessage test requested for %r from %s', name, _client_ip())
        conn.close()
        return jsonify({'request_id': rid, 'chat_name': name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Telegram (v2.3) ──────────────────────────────────────────────────────────
# Calls go straight from the web process (plain HTTPS - no macOS permission needed, unlike iMessage).
# tg_http_post is swapped out by tests and the preview server.
tg_http_post = None


def _telegram_state(conn=None):
    return tgmod.public_config(tgmod.load_config(conn=conn))


def _tg_cooldown(kind, seconds):
    """None if allowed (and stamps the time), else seconds left."""
    now = time.time()
    with _tg_lock:
        last = _tg_last.get(kind)
        if last is not None and now - last < seconds:
            return int(seconds - (now - last)) + 1
        _tg_last[kind] = now
    return None


@app.route('/api/settings/telegram', methods=['GET'])
def get_telegram_setting():
    try:
        conn = get_conn()
        out = _telegram_state(conn)
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings/telegram', methods=['POST'])
def set_telegram_setting():
    """{password, token?, clear_token?, enabled?, chats?: [{id, title}]}. Blank/absent token keeps the saved one."""
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    data = data or {}
    token = (data.get('token') or '').strip() if isinstance(data.get('token', ''), str) else None
    if token is None:
        return jsonify({'error': 'Bot token must be text'}), 400
    if token and not tgmod.valid_token(token):
        return jsonify({'error': 'That doesn\u2019t look like a bot token. It should look like 123456789:AA\u2026 (copy it from @BotFather).'}), 400
    chats = None
    if 'chats' in data:
        if not isinstance(data['chats'], list):
            return jsonify({'error': 'chats must be a list'}), 400
        chats, seen = [], set()
        try:
            for c in data['chats']:
                if not isinstance(c, dict):
                    c = {'id': c}
                cid = tgmod.normalize_chat_id(c.get('id'))
                if cid in seen:
                    continue
                seen.add(cid)
                title = str(c.get('title') or '').strip()[:100]
                chats.append({'id': cid, 'title': ''.join(ch for ch in title if ord(ch) >= 32)})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if len(chats) > tgmod.MAX_CHATS:
            return jsonify({'error': 'At most %d chats' % tgmod.MAX_CHATS}), 400
    try:
        conn = get_conn()
        if data.get('clear_token'):
            dbmod.set_setting('telegram_token', None, conn=conn)
        elif token:
            dbmod.set_setting('telegram_token', token, conn=conn)
        if chats is not None:
            dbmod.set_setting('telegram_chats', json.dumps(chats), conn=conn)
        if 'enabled' in data:
            dbmod.set_setting('telegram_enabled', '1' if data.get('enabled') else '0', conn=conn)
        out = _telegram_state(conn)
        conn.close()
        log.info('Telegram settings saved from %s (token %s, %d chat(s), enabled=%s)', _client_ip(),
                 'changed' if token else ('cleared' if data.get('clear_token') else 'unchanged'),
                 len(out['chats']), out['enabled'])
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings/telegram/find', methods=['POST'])
def find_telegram_chats():
    """List chats the bot has recently seen (getUpdates, nothing consumed). Uses data.token if typed, else the saved one."""
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    token = ((data or {}).get('token') or '').strip() if isinstance((data or {}).get('token', ''), str) else ''
    if token and not tgmod.valid_token(token):
        return jsonify({'error': 'That doesn\u2019t look like a bot token. It should look like 123456789:AA\u2026'}), 400
    if not token:
        token = tgmod.load_config()['token']
    if not token:
        return jsonify({'error': 'Paste your bot token first (from @BotFather).'}), 400
    wait = _tg_cooldown('find', TELEGRAM_FIND_COOLDOWN_S)
    if wait:
        return jsonify({'error': 'Please wait %ds' % wait}), 429
    try:
        return jsonify(tgmod.find_chats(token, http_post=tg_http_post))
    except tgmod.TelegramError as e:
        return jsonify({'error': tgmod.friendly_error(e), 'code': e.code}), 400


@app.route('/api/settings/telegram/test', methods=['POST'])
def test_telegram():
    """Send a test message to every saved chat (rate-limited). Works even while alerts are switched off."""
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    cfg = tgmod.load_config()
    if not cfg['token']:
        return jsonify({'error': 'No bot token saved yet.'}), 400
    if not cfg['chats']:
        return jsonify({'error': 'Add at least one chat first.'}), 400
    wait = _tg_cooldown('test', TELEGRAM_TEST_COOLDOWN_S)
    if wait:
        return jsonify({'error': 'A test was just sent - wait %ds' % wait}), 429
    results = []
    for c in cfg['chats']:
        try:
            tgmod.call(cfg['token'], 'sendMessage', {'chat_id': c['id'], 'text': tgmod.TEST_MESSAGE}, http_post=tg_http_post)
            results.append({'id': c['id'], 'title': c['title'], 'ok': True})
        except tgmod.TelegramError as e:
            results.append({'id': c['id'], 'title': c['title'], 'ok': False, 'error': tgmod.friendly_error(e)})
    log.info('Telegram test from %s: %s', _client_ip(), ', '.join('%s=%s' % (r['id'], 'ok' if r['ok'] else 'fail') for r in results))
    return jsonify({'results': results, 'ok': all(r['ok'] for r in results)})


@app.route('/api/requests/<int:rid>', methods=['GET'])
def get_request(rid):
    try:
        conn = get_conn()
        row = _request_row(conn, rid)
        conn.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(row)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/check', methods=['POST'])
def auth_check():
    auth = check_password(request.get_json(silent=True))
    if not auth:
        return unauthorized(auth)
    return jsonify({'ok': True})


@app.route('/api/ack', methods=['POST'])
def ack():
    """Acknowledge the escalating (pump) alarm: same ack file v1 pump_monitor watches."""
    auth = check_password(request.get_json(silent=True))
    if not auth:
        return unauthorized(auth)
    try:
        open(settings.ACK_FILE, 'a').close()
        log.info('alarm acknowledged from dashboard (%s)', _client_ip())
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tags/<int:tag_id>/alarm', methods=['POST'])
def set_tag_alarm(tag_id):
    data = request.get_json(silent=True)
    auth = check_password(data)
    if not auth:
        return unauthorized(auth)
    try:
        conn = get_conn()
        conn.execute('UPDATE tags SET alarm_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                     (1 if data.get('alarm_enabled') else 0, tag_id))
        row = conn.execute('SELECT * FROM tags WHERE id=?', (tag_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/healthz')
def healthz():
    try:
        conn = get_conn()
        p = _poller_status(conn)
        conn.close()
        return jsonify({'ok': True, 'web': __version__, 'poller_alive': p['alive'], 'poller_age_s': p['age_s']})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def main():
    import argparse
    ap = argparse.ArgumentParser(description='PLCCommandCenter v2 dashboard')
    ap.add_argument('--host', default=settings.DASHBOARD_HOST)
    ap.add_argument('--port', type=int, default=settings.DASHBOARD_PORT)
    ap.add_argument('--dev', action='store_true', help='Flask dev server instead of waitress')
    args = ap.parse_args()
    fmt = '%(asctime)s [%(levelname)s] %(name)s %(message)s'
    path = os.environ.get('PLCCC_LOG_FILE')
    if path:
        from logging.handlers import RotatingFileHandler
        h = RotatingFileHandler(os.path.expanduser(path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
        h.setFormatter(logging.Formatter(fmt))
        logging.basicConfig(level=logging.INFO, handlers=[h])
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
    init_db()
    log.info('PLCCommandCenter web v%s on %s:%s (dry_run=%s, db=%s)', __version__, args.host, args.port,
             settings.DRY_RUN, settings.DB_PATH)
    if args.dev:
        app.run(host=args.host, port=args.port)
    else:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=settings.WEB_THREADS, ident='PLCCC')
