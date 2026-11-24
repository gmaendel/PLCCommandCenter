"""Unified poller + alerter: replaces pump_monitor.py, modbus_monitor.py,
siemens_monitor.py and ab_monitor.py.

  * Supervisor thread (main): migrates the DB, keeps one DeviceWorker thread per enabled
    device (restarts dead workers, restarts a worker when its connection settings change),
    writes a heartbeat to service_status every 15 s.
  * DeviceWorker: polls one device every POLL_INTERVAL seconds with its own driver and DB
    connection, so a hung/offline PLC never delays the others. Every exception is caught
    per poll; a crashed thread is restarted by the supervisor.
  * Alarm/comm state lives in the supervisor (keyed by tag/device id) so restarting a
    worker never re-sends an active alarm.

Run:  python app/plccc_poller.py [--once] [--duration SECONDS]
"""
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import warnings

warnings.filterwarnings('ignore', message='.*OpenSSL.*')

import plccc_settings as settings  # noqa: E402
from . import db as dbmod  # noqa: E402
from .alarms import ThresholdAlarm, EscalatingAlarm, CommMonitor  # noqa: E402
from .alerts import Alerter, AckChecker, find_chat  # noqa: E402
from .drivers import make_driver, apply_scaling, tcp_probe, TYPE_LABELS  # noqa: E402

log = logging.getLogger('plccc.poller')

LOG_TABLES = {'flow_log': ('gpm', 'level'), 'aquifer_log': ('level',)}
PRUNE_EVERY_N_POLLS = 10
INT_TYPES = ('INT', 'DINT', 'WORD', 'BOOL')


def _sig(dev):
    keys = ('type', 'host', 'port', 'rack', 'slot', 'unit_id', 'word_swap', 'comm_config', 'name', 'options')
    return tuple(dev[k] if k in dev.keys() else None for k in keys)


class Registry:
    """Alarm + comm state that must survive worker restarts."""

    def __init__(self):
        self.threshold = {}     # tag_id -> ThresholdAlarm
        self.escalating = {}    # tag_id -> (config_json, EscalatingAlarm)
        self.comm = {}          # device_id -> {'fail_count', 'offline'}


class DeviceWorker(threading.Thread):
    def __init__(self, dev, *, alerter, registry, interval, clock=time.time, db_path=None, once=False):
        super().__init__(name='dev-%s' % dev['id'], daemon=True)
        self.dev = dev
        self.dev_id = dev['id']
        self.name_ = dev['name']
        self.alerter = alerter
        self.reg = registry
        self.interval = interval
        self.clock = clock
        self.db_path = db_path
        self.once = once
        self.stop_event = threading.Event()
        self.sig = _sig(dev)
        self.comm_cfg = dbmod.loads(dev['comm_config'] if 'comm_config' in dev.keys() else None, {}) or {}
        self.driver = make_driver(dev)
        self.polls = 0
        self.last_poll_ok = None

    # ── helpers ──────────────────────────────────────────────────────────────
    def _send(self, text, kind):
        self.alerter.send(text, kind=kind, source=self.name_)

    def _comm(self):
        st = self.reg.comm.setdefault(self.dev_id, {'fail_count': 0, 'offline': False})
        cm = CommMonitor(device_name=self.name_, type_label=TYPE_LABELS.get(self.dev['type'], self.dev['type']),
                         host=getattr(self.driver, 'probe_host', None) or self.dev['host'], port=self.driver.port,
                         config=self.comm_cfg,
                         send=self._send, probe=tcp_probe, clock=self.clock)
        cm.fail_count, cm.offline = st['fail_count'], st['offline']
        return cm, st

    def _save_comm(self, cm, st):
        st['fail_count'], st['offline'] = cm.fail_count, cm.offline

    def _runtime(self, conn, ok, err=None):
        st = self.reg.comm.get(self.dev_id, {})
        try:
            conn.execute(
                'INSERT INTO device_runtime (device_id, online, fail_count, offline_alerted, last_ok, last_error, updated_at) '
                "VALUES (?,?,?,?,CASE WHEN ? THEN datetime('now') ELSE NULL END,?,datetime('now')) "
                'ON CONFLICT(device_id) DO UPDATE SET online=excluded.online, fail_count=excluded.fail_count, '
                'offline_alerted=excluded.offline_alerted, last_ok=COALESCE(excluded.last_ok, device_runtime.last_ok), '
                'last_error=excluded.last_error, updated_at=excluded.updated_at',
                (self.dev_id, 1 if ok else 0, st.get('fail_count', 0), 1 if st.get('offline') else 0,
                 1 if ok else 0, None if ok else (str(err)[:300] if err else 'poll failed')))
        except Exception as e:
            log.error('[%s] device_runtime update failed: %s', self.name_, e)

    def _alarm_state(self, conn, tag_id, **kw):
        try:
            if kw.get('new'):
                conn.execute('INSERT OR REPLACE INTO tag_alarm_state (tag_id,active,triggered_at,last_value,stage,acked,updated_at) '
                             "VALUES (?,1,CURRENT_TIMESTAMP,?,?,0,CURRENT_TIMESTAMP)", (tag_id, kw.get('value'), kw.get('stage')))
            elif kw.get('active'):
                conn.execute('UPDATE tag_alarm_state SET active=1, last_value=?, stage=?, acked=?, updated_at=CURRENT_TIMESTAMP '
                             'WHERE tag_id=?', (kw.get('value'), kw.get('stage'), 1 if kw.get('acked') else 0, tag_id))
            else:
                conn.execute('INSERT OR REPLACE INTO tag_alarm_state (tag_id,active,triggered_at,last_value,stage,acked,updated_at) '
                             'VALUES (?,0,NULL,?,?,0,CURRENT_TIMESTAMP)', (tag_id, kw.get('value'), kw.get('stage')))
        except Exception as e:
            log.error('[%s] tag_alarm_state write failed: %s', self.name_, e)

    # ── main loop ────────────────────────────────────────────────────────────
    def stop(self):
        self.stop_event.set()

    def run(self):
        conn = dbmod.connect(self.db_path)
        log.info('[%s] worker started (%s %s:%s, every %ss)', self.name_, self.dev['type'], self.dev['host'],
                 self.driver.port, int(self.interval))
        next_t = time.monotonic()
        try:
            while not self.stop_event.is_set():
                try:
                    self.poll_once(conn)
                except Exception as e:   # never let one bad poll kill the worker
                    log.exception('[%s] unexpected poll error: %s', self.name_, e)
                    self.driver.close()
                if self.once:
                    break
                next_t += self.interval
                delay = next_t - time.monotonic()
                if delay < 0:            # fell behind (slow PLC): don't burst-poll
                    next_t = time.monotonic()
                    delay = 0
                self.stop_event.wait(delay)
        finally:
            self.driver.close()
            conn.close()
            log.info('[%s] worker stopped', self.name_)

    def poll_once(self, conn):
        self.polls += 1
        tags = conn.execute('SELECT * FROM tags WHERE device_id=? AND enabled=1 ORDER BY id', (self.dev_id,)).fetchall()
        cm, st = self._comm()
        fail_any = bool(self.comm_cfg.get('fail_on_any_tag_error', False))
        results, err = [], None
        try:
            self.driver.connect()
            results = self.driver.read(tags) if tags else []
            good = sum(1 for v, e in results if e is None)
            if not tags:
                ok = True
            elif fail_any:
                ok = good == len(tags)
            else:
                ok = good > 0
            if not ok:
                errs = ['%s: %s' % (t['name'], e) for t, (v, e) in zip(tags, results) if e]
                err = '; '.join(errs)[:300]
        except Exception as e:
            ok, err = False, e
        for t, (v, e) in zip(tags, results):
            if e:
                log.warning('[%s] Tag "%s" (%s): read error - %s', self.name_, t['name'], t['address'], e)

        if not ok:
            log.warning('[%s] poll failed (%s:%s): %s', self.name_, self.dev['host'], self.driver.port, err)
            if getattr(self.driver, 'close_on_error', True):
                self.driver.close()
            events = cm.failure()
            self._save_comm(cm, st)
            if 'rebuild_driver' in events:
                self.driver.close()
                self.driver = make_driver(self.dev)
            self._runtime(conn, False, err)
            self.last_poll_ok = False
            return False

        if not self.driver.persistent:
            self.driver.close()
        cm.success()
        self._save_comm(cm, st)

        stored = []
        conn.execute('BEGIN IMMEDIATE')
        try:
            log_rows = {}
            for tag, (raw, e) in zip(tags, results):
                if e is not None:
                    continue
                value = round(float(apply_scaling(raw, tag)), 4)
                units = tag['units'] or ''
                log.info('[%s] %s (%s) = %s %s', self.name_, tag['name'], tag['address'], value, units)
                conn.execute('INSERT INTO tag_readings (tag_id, value) VALUES (?, ?)', (tag['id'], value))
                src = dbmod.loads(tag['log_source'])
                if src and src.get('table') in LOG_TABLES and src.get('column') in LOG_TABLES[src['table']]:
                    v = round(value, 2) if src['table'] == 'aquifer_log' else value
                    log_rows.setdefault(src['table'], {})[src['column']] = v
                elif (self.dev['type'] == 'siemens_s7' and not tag['log_source']
                      and tag['address'].strip().upper() in ('DB6.REAL108', 'DB6.DBD108')):
                    log_rows.setdefault('aquifer_log', {})['level'] = round(value, 2)   # v1 dual-write
                stored.append((tag, raw, value))
            for table, cols in log_rows.items():
                names = sorted(cols)
                conn.execute('INSERT INTO %s (%s) VALUES (%s)' % (table, ','.join(names), ','.join('?' * len(names))),
                             [cols[n] for n in names])
            if self.polls % PRUNE_EVERY_N_POLLS == 1:
                for tag, _, _ in stored:
                    conn.execute("DELETE FROM tag_readings WHERE tag_id=? AND timestamp < datetime('now', '-7 days')",
                                 (tag['id'],))
                for table in log_rows:
                    conn.execute("DELETE FROM %s WHERE timestamp < datetime('now', '-60 days')" % table)
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise

        for tag, raw, value in stored:
            try:
                self._check_alarms(conn, tag, raw, value)
            except Exception as e:
                log.exception('[%s] alarm check failed for %s: %s', self.name_, tag['name'], e)
        self._runtime(conn, True)
        self.last_poll_ok = True
        return True

    def _check_alarms(self, conn, tag, raw, value):
        tag_id = tag['id']
        units = tag['units'] or ''
        cfg_text = tag['alarm_config'] if 'alarm_config' in tag.keys() else None
        cfg = dbmod.loads(cfg_text)
        if cfg and cfg.get('mode') == 'escalating':
            entry = self.reg.escalating.get(tag_id)
            if entry is None or entry[0] != cfg_text:
                ack = cfg.get('ack') or {}
                checker = AckChecker(ack_file=ack.get('file') or settings.ACK_FILE,
                                     # one group for sending AND acks: the Settings-page name, re-read
                                     # on every check (alarm_config's legacy imessage_chat is ignored)
                                     chat_name=lambda: dbmod.imessage_chat(self.db_path),
                                     keywords=ack.get('keywords') or ())
                esc = EscalatingAlarm(cfg, clock=self.clock, send=self._send, ack=checker, name=tag['name'])
                entry = (cfg_text, esc)
                self.reg.escalating[tag_id] = entry
            esc = entry[1]
            # rebind side effects to the worker that currently owns this tag
            esc.send = self._send
            esc.on_state = lambda **kw: self._alarm_state(conn, tag_id, **kw)
            src = cfg.get('threshold_source')
            if src and src.startswith('pump_config.'):
                threshold = dbmod.read_threshold_config(src.split('.', 1)[1], cfg.get('threshold_default', 2000))
            else:
                threshold = tag['alarm_high'] if cfg.get('direction') == 'high' else tag['alarm_low']
            if threshold is None:
                return
            if isinstance(raw, int) and float(value).is_integer() and (tag['data_type'] or '').upper() in INT_TYPES:
                shown = int(value)        # v1 printed the PLC integer as-is ("490", not "490.0")
            else:
                shown = value
            ev = esc.evaluate(value=value, threshold=threshold, alarm_enabled=bool(tag['alarm_enabled']),
                              display_value=shown, extra={'device': self.name_, 'units': units})
            if ev:
                log.info('[%s] %s escalation events: %s (stage=%s)', self.name_, tag['name'], ev, esc.stage)
            return
        self.reg.escalating.pop(tag_id, None)
        ta = self.reg.threshold.setdefault(tag_id, ThresholdAlarm())
        kind, msg = ta.evaluate(value=value, alarm_enabled=tag['alarm_enabled'], alarm_low=tag['alarm_low'],
                                alarm_high=tag['alarm_high'], alarm_message=tag['alarm_message'] or '',
                                device_name=self.name_, tag_name=tag['name'], units=units)
        if kind == 'alarm':
            self._send(msg, 'alarm')
            self._alarm_state(conn, tag_id, new=True, value=value, stage=1)
            log.warning('ALARM: %s %s = %s %s', self.name_, tag['name'], value, units)
        elif kind == 'clear':
            self._send(msg, 'clear')
            self._alarm_state(conn, tag_id, active=False, value=value, stage=None)
            log.info('CLEAR: %s %s = %s %s', self.name_, tag['name'], value, units)


TEST_MESSAGE = '\u2705 PLC Command Center test message. Alarms will be sent to this group.'
REQUEST_MAX_AGE_S = 300


class RequestWorker(threading.Thread):
    """Runs dashboard requests that need the poller's permissions (Full Disk Access for chat.db,
    Messages automation for AppleScript): 'imessage_check' and 'imessage_test'."""

    def __init__(self, alerter, db_path=None, interval=2.0, chat_db=None):
        super().__init__(name='requests', daemon=True)
        self.alerter = alerter
        self.db_path = db_path
        self.interval = interval
        self.chat_db = chat_db or settings.CHAT_DB_PATH
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.process_pending()
            except Exception as e:
                log.error('request worker error: %s', e)
            self.stop_event.wait(self.interval)

    def handle(self, kind, payload):
        chat = (payload or {}).get('chat_name') or dbmod.imessage_chat(self.db_path)
        if kind == 'imessage_check':
            return find_chat(self.chat_db, chat)
        if kind == 'imessage_test':
            status, err = self.alerter.send_imessage_to(TEST_MESSAGE, chat)
            log.info('iMessage test to "%s": %s %s', chat, status, err or '')
            return {'chat_name': chat, 'status': status, 'error': err}
        return {'error': 'unknown request %r' % kind}

    def process_pending(self):
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute("UPDATE service_requests SET status='expired', done_at=datetime('now') WHERE status='pending' "
                         "AND created_at < datetime('now', ?)", ('-%d seconds' % REQUEST_MAX_AGE_S,))
            conn.execute("DELETE FROM service_requests WHERE created_at < datetime('now','-7 days')")
            rows = conn.execute("SELECT * FROM service_requests WHERE status='pending' ORDER BY id").fetchall()
            for r in rows:
                cur = conn.execute("UPDATE service_requests SET status='running' WHERE id=? AND status='pending'", (r['id'],))
                if cur.rowcount != 1:
                    continue
                try:
                    res = self.handle(r['kind'], dbmod.loads(r['payload'], {}))
                    status = 'error' if res.get('error') and not res.get('status') else 'done'
                except Exception as e:
                    res, status = {'error': str(e)}, 'error'
                conn.execute("UPDATE service_requests SET status=?, result=?, done_at=datetime('now') WHERE id=?",
                             (status, json.dumps(res), r['id']))
        finally:
            conn.close()


class Supervisor:
    def __init__(self, *, interval=None, db_path=None, once=False, alerter=None):
        self.interval = interval or settings.POLL_INTERVAL
        self.db_path = db_path
        self.once = once
        self.alerter = alerter or Alerter(db_path=db_path)
        self.registry = Registry()
        self.workers = {}
        self.stop_event = threading.Event()
        self.started_at = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        self.requests = None

    def load_devices(self, conn):
        return conn.execute('SELECT * FROM devices WHERE enabled=1 ORDER BY id').fetchall()

    def sync_workers(self, conn):
        devs = {d['id']: d for d in self.load_devices(conn)}
        for dev_id in list(self.workers):
            w = self.workers[dev_id]
            if dev_id not in devs or _sig(devs[dev_id]) != w.sig:
                log.info('[%s] device removed/disabled/changed - stopping worker', w.name_)
                w.stop()
                w.join(timeout=30)
                del self.workers[dev_id]
                if dev_id not in devs:
                    self.registry.comm.pop(dev_id, None)
        for dev_id, dev in devs.items():
            w = self.workers.get(dev_id)
            if w is not None and not w.is_alive():
                log.error('[%s] worker thread died - restarting', w.name_)
                del self.workers[dev_id]
                w = None
            if w is None:
                try:
                    w = DeviceWorker(dev, alerter=self.alerter, registry=self.registry, interval=self.interval,
                                     db_path=self.db_path, once=self.once)
                except Exception as e:
                    log.error('[%s] cannot start worker: %s', dev['name'], e)
                    continue
                self.workers[dev_id] = w
                w.start()

    def heartbeat(self, conn):
        info = {'dry_run': settings.DRY_RUN, 'interval': self.interval, 'alerter_ok': self.alerter.healthy(),
                'workers': {str(k): {'name': w.name_, 'alive': w.is_alive(), 'last_ok': w.last_poll_ok}
                            for k, w in self.workers.items()}}
        try:
            conn.execute("INSERT INTO service_status (name,pid,started_at,heartbeat,info) VALUES "
                         "('poller',?,?,datetime('now'),?) ON CONFLICT(name) DO UPDATE SET pid=excluded.pid, "
                         'started_at=excluded.started_at, heartbeat=excluded.heartbeat, info=excluded.info',
                         (os.getpid(), self.started_at, json.dumps(info)))
        except Exception as e:
            log.error('heartbeat write failed: %s', e)

    def run(self, duration=None):
        conn = None
        for attempt in range(1, 31):
            try:
                conn = dbmod.connect(self.db_path)
                dbmod.migrate(conn)
                break
            except Exception as e:
                log.error('DB open/migrate failed (attempt %d): %s', attempt, e)
                time.sleep(10)
        if conn is None:
            raise SystemExit('database unavailable')
        log.info('PLCCommandCenter poller v2 starting - db=%s interval=%ss dry_run=%s ack_file=%s',
                 self.db_path or settings.DB_PATH, self.interval, settings.DRY_RUN, settings.ACK_FILE)
        record_chat_db_access(conn)
        self.alerter.start()
        if not self.once:
            self.requests = RequestWorker(self.alerter, db_path=self.db_path)
            self.requests.start()
        end = time.monotonic() + duration if duration else None
        try:
            while not self.stop_event.is_set():
                try:
                    self.alerter._ensure_threads()
                    self.sync_workers(conn)
                    self.heartbeat(conn)
                except Exception as e:
                    log.exception('supervisor loop error: %s', e)
                if self.once:
                    for w in list(self.workers.values()):
                        w.join(timeout=120)
                    break
                if end and time.monotonic() >= end:
                    break
                self.stop_event.wait(15)
        finally:
            self.shutdown()
            conn.close()

    def shutdown(self):
        if self.requests is not None:
            self.requests.stop()
        for w in self.workers.values():
            w.stop()
        for w in self.workers.values():
            w.join(timeout=30)
        self.alerter.flush(timeout=15)
        log.info('poller stopped')


def setup_logging():
    """stdout by default; PLCCC_LOG_FILE=path -> size-rotated file (10 MB x 5), used by launchd."""
    fmt = '%(asctime)s [%(levelname)s] %(message)s'
    path = os.environ.get('PLCCC_LOG_FILE')
    if path:
        from logging.handlers import RotatingFileHandler
        h = RotatingFileHandler(os.path.expanduser(path), maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
        h.setFormatter(logging.Formatter(fmt))
        logging.basicConfig(level=logging.INFO, handlers=[h])
    else:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format=fmt)
    # third-party chatter (pycomm3 logs a benign ERROR on every Extended Forward Open fallback);
    # our workers log every read failure themselves.
    logging.getLogger('pycomm3').setLevel(logging.CRITICAL)
    logging.getLogger('snap7').setLevel(logging.WARNING)
    logging.getLogger('pymodbus').setLevel(logging.WARNING)


def check_chat_db_access(path=None):
    """Can this process read the Messages database? (needs Full Disk Access on macOS)
    -> 'ok' | 'missing' | 'denied: <error>'."""
    import sqlite3
    path = path or settings.CHAT_DB_PATH
    if not os.path.exists(path):
        return 'missing'
    try:
        c = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5)
        try:
            c.execute('SELECT COUNT(*) FROM chat').fetchone()
        finally:
            c.close()
        return 'ok'
    except Exception as e:
        return 'denied: %s' % e


def record_chat_db_access(conn, path=None):
    """Log + store the result in app_settings.fda_status (read by the installer to confirm Full Disk Access)."""
    status = check_chat_db_access(path)
    (log.info if status == 'ok' else log.warning)('Messages database (%s) readable: %s%s', path or settings.CHAT_DB_PATH,
        status, '' if status == 'ok' else ' - iMessage replies cannot acknowledge alarms until Full Disk Access is granted')
    try:
        dbmod.set_setting('fda_status', json.dumps({'status': status, 'python': sys.executable,
                                                    'checked_at': time.strftime('%Y-%m-%d %H:%M:%S')}), conn=conn)
    except Exception as e:
        log.error('could not store fda_status: %s', e)
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(description='PLCCommandCenter unified poller/alerter')
    ap.add_argument('--once', action='store_true', help='poll every device once, then exit')
    ap.add_argument('--duration', type=float, default=None, help='run for N seconds, then exit (testing)')
    ap.add_argument('--interval', type=float, default=None, help='override poll interval (seconds)')
    args = ap.parse_args(argv)
    setup_logging()
    sup = Supervisor(interval=args.interval, once=args.once)

    def _stop(signum, frame):
        log.info('signal %s received - shutting down', signum)
        sup.stop_event.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sup.run(duration=args.duration)


if __name__ == '__main__':
    main()
