"""v2.2: OPC UA + MQTT drivers (mocked clients), device options API, configurable iMessage group."""
import json
import os
import sqlite3
import time
import unittest
from unittest import mock

from tests._env import TMP, FakeClock
import plccc_settings as settings
from plccc import db as dbmod
from plccc import drivers
from plccc.alerts import Alerter, AckChecker, find_chat, APPLE_EPOCH_OFFSET

PW = 'test-pw'


class Row(dict):
    def keys(self):
        return list(super().keys())


def dev(**kw):
    d = {'id': 7, 'name': 'X', 'type': 'opc_ua', 'host': 'opc.tcp://10.0.0.5:4841/srv', 'port': 0, 'rack': 0, 'slot': 1,
         'unit_id': 1, 'word_swap': 0, 'options': None}
    d.update(kw)
    return Row(d)


def tag(addr, **kw):
    t = {'id': 1, 'name': 'T', 'address': addr, 'data_type': 'REAL'}
    t.update(kw)
    return Row(t)


# ── helpers ───────────────────────────────────────────────────────────────────
class HelperTests(unittest.TestCase):
    def test_to_number(self):
        self.assertEqual(drivers.to_number(True), 1.0)
        self.assertEqual(drivers.to_number(3), 3.0)
        self.assertEqual(drivers.to_number(' 93.5 '), 93.5)
        self.assertEqual(drivers.to_number(b'12'), 12.0)
        self.assertEqual(drivers.to_number('ON'), 1.0)
        self.assertEqual(drivers.to_number('false'), 0.0)
        for bad in ('abc', float('nan'), [1], {'a': 1}, None):
            with self.assertRaises(ValueError):
                drivers.to_number(bad)

    def test_opcua_endpoint(self):
        self.assertEqual(drivers.opcua_endpoint('opc.tcp://10.0.0.5:4841/srv'), ('opc.tcp://10.0.0.5:4841/srv', '10.0.0.5', 4841))
        self.assertEqual(drivers.opcua_endpoint('10.0.0.5'), ('opc.tcp://10.0.0.5:4840', '10.0.0.5', 4840))
        self.assertEqual(drivers.opcua_endpoint('plc.local', 4999)[2], 4999)
        with self.assertRaises(drivers.DriverError):
            drivers.opcua_endpoint('opc.tcp://')

    def test_mqtt_address_and_json_path(self):
        self.assertEqual(drivers.mqtt_split_address('farm/gen/state|temp'), ('farm/gen/state', 'temp'))
        self.assertEqual(drivers.mqtt_split_address(' farm/x '), ('farm/x', ''))
        obj = {'temp': 93, 'data': {'sensors': [{'value': 1.5}, {'value': 2}]}, 'arr': [5, 6]}
        self.assertEqual(drivers.json_path_get(obj, 'temp'), 93)
        self.assertEqual(drivers.json_path_get(obj, 'data.sensors[1].value'), 2)
        self.assertEqual(drivers.json_path_get(obj, '$.data.sensors[0].value'), 1.5)
        self.assertEqual(drivers.json_path_get(obj, 'arr.1'), 6)

    def test_mqtt_decode(self):
        self.assertEqual(drivers.mqtt_decode(b'93'), 93.0)
        self.assertEqual(drivers.mqtt_decode(b'true'), 1.0)
        self.assertEqual(drivers.mqtt_decode(b'{"temp":93}', 'temp'), 93.0)
        self.assertEqual(drivers.mqtt_decode(b'{"on":false}', 'on'), 0.0)
        with self.assertRaisesRegex(ValueError, 'not found'):
            drivers.mqtt_decode(b'{"temp":93}', 'hum')
        with self.assertRaisesRegex(ValueError, 'not JSON'):
            drivers.mqtt_decode(b'hello', 'temp')
        with self.assertRaisesRegex(ValueError, 'non-numeric'):
            drivers.mqtt_decode(b'hello')

    def test_registered_and_read_only(self):
        self.assertIs(drivers.DRIVERS['opc_ua'], drivers.OpcUaDriver)
        self.assertIs(drivers.DRIVERS['mqtt'], drivers.MqttDriver)
        for cls in (drivers.OpcUaDriver, drivers.MqttDriver):
            self.assertFalse([m for m in dir(cls) if 'write' in m.lower()])

    def test_device_options(self):
        self.assertEqual(drivers.device_options(dev(options='{"username":"u"}')), {'username': 'u'})
        self.assertEqual(drivers.device_options(dev(options='junk')), {})
        self.assertEqual(drivers.device_options(Row({'type': 'x'})), {})


# ── OPC UA ────────────────────────────────────────────────────────────────────
class FakeNode:
    def __init__(self, client, nid):
        self.client, self.nid = client, nid

    def read_value(self):
        v = self.client.values[self.nid]
        if isinstance(v, Exception):
            raise v
        return v


class FakeUaClient:
    def __init__(self, url, values=None):
        self.url, self.values = url, values or {}
        self.user = self.password = self.security = None
        self.connected = self.disconnected = 0
        self.fail_connect = False

    def set_user(self, u):
        self.user = u

    def set_password(self, p):
        self.password = p

    def set_security_string(self, s):
        self.security = s

    def connect(self):
        if self.fail_connect:
            raise OSError('refused')
        self.connected += 1

    def disconnect(self):
        self.disconnected += 1

    def get_node(self, nid):
        if not nid.startswith(('ns=', 'i=')):
            raise ValueError('invalid')
        return FakeNode(self, nid)

    def read_attributes(self, nodes):
        out = []
        for n in nodes:
            v = self.values[n.nid]
            if isinstance(v, Exception):
                raise v                     # whole batch fails -> driver falls back per node
            if isinstance(v, FakeStatus):
                out.append(type('DV', (), {'StatusCode': v, 'Value': None})())
            else:
                out.append(type('DV', (), {'StatusCode': FakeStatus(True), 'Value': type('V', (), {'Value': v})()})())
        return out


class FakeStatus:
    def __init__(self, good, name='BadNodeIdUnknown'):
        self.good, self.name = good, name

    def is_good(self):
        return self.good


class OpcUaTests(unittest.TestCase):
    def make(self, values, **opts):
        holder = {}

        def factory(url):
            holder['c'] = FakeUaClient(url, values)
            return holder['c']
        d = drivers.OpcUaDriver(dev(options=json.dumps(opts) if opts else None), client_factory=factory)
        return d, holder

    def test_connect_and_batch_read(self):
        d, h = self.make({'ns=2;s=Pump.GPM': 612, 'ns=2;s=Run': True}, username='op', password='pw')
        self.assertEqual((d.url, d.probe_host, d.port), ('opc.tcp://10.0.0.5:4841/srv', '10.0.0.5', 4841))
        d.connect()
        c = h['c']
        self.assertEqual((c.user, c.password, c.security), ('op', 'pw', None))
        self.assertEqual(d.read([tag('ns=2;s=Pump.GPM'), tag(' ns=2;s=Run ')]), [(612.0, None), (1.0, None)])
        d.connect()
        self.assertEqual(c.connected, 1)       # persistent
        d.close()
        self.assertEqual(c.disconnected, 1)
        self.assertFalse(d.connected)

    def test_bad_node_falls_back_per_node(self):
        d, h = self.make({'ns=2;s=A': 1.5, 'ns=2;s=B': RuntimeError('BadNodeIdUnknown'), 'ns=2;s=C': 'text'})
        d.connect()
        out = d.read([tag('ns=2;s=A'), tag('ns=2;s=B'), tag('garbage'), tag('ns=2;s=C')])
        self.assertEqual(out[0], (1.5, None))
        self.assertIn('BadNodeIdUnknown', out[1][1])
        self.assertIn('bad NodeId', out[2][1])
        self.assertIn('non-numeric', out[3][1])

    def test_bad_status_is_per_tag_error(self):
        d, h = self.make({'ns=2;s=A': 2, 'ns=2;s=B': FakeStatus(False), 'ns=2;s=N': None})
        d.connect()
        out = d.read([tag('ns=2;s=A'), tag('ns=2;s=B'), tag('ns=2;s=N')])
        self.assertEqual(out, [(2.0, None), (None, 'OPC UA status BadNodeIdUnknown'), (None, 'no value')])

    def test_connection_lost_raises(self):
        d, h = self.make({'ns=2;s=A': ConnectionError('closed')})
        d.connect()
        with self.assertRaises(drivers.DriverError):
            d.read([tag('ns=2;s=A')])

    def test_connect_failure(self):
        def factory(url):
            c = FakeUaClient(url)
            c.fail_connect = True
            return c
        d = drivers.OpcUaDriver(dev(), client_factory=factory)
        with self.assertRaisesRegex(drivers.DriverError, 'OPC UA connect'):
            d.connect()
        self.assertFalse(d.connected)

    def test_security_policy(self):
        d, h = self.make({}, security_policy='Basic256Sha256', security_mode='Sign')
        with mock.patch.object(drivers, 'opcua_client_cert', return_value=('/c.der', '/k.pem')):
            d.connect()
        self.assertEqual(h['c'].security, 'Basic256Sha256,Sign,/c.der,/k.pem')
        d2, _ = self.make({}, security_policy='Bogus')
        with self.assertRaises(drivers.DriverError):
            d2.connect()


# ── MQTT ──────────────────────────────────────────────────────────────────────
class FakeMqtt:
    def __init__(self, drv):
        self.drv, self.subs, self.stopped = drv, [], False

    def subscribe(self, topic, qos=0):
        self.subs.append(topic)

    def disconnect(self):
        pass

    def loop_stop(self):
        self.stopped = True

    # test helpers
    def up(self):
        self.drv._on_connect(self, None, {}, 0, None)

    def down(self):
        self.drv._on_disconnect(self, None, {}, 7, None)

    def msg(self, topic, payload):
        m = type('M', (), {'topic': topic, 'payload': payload})()
        self.drv._on_message(self, None, m)


class MqttTests(unittest.TestCase):
    def make(self, **opts):
        clock = FakeClock()
        holder = {}

        def factory(drv):
            holder['c'] = FakeMqtt(drv)
            return holder['c']
        d = drivers.MqttDriver(dev(type='mqtt', host='broker.local', port=0, options=json.dumps(opts)),
                               client_factory=factory, clock=clock)
        d.connect_wait = 0.01
        return d, holder, clock

    def test_defaults(self):
        d, _, _ = self.make()
        self.assertEqual((d.port, d.stale_after, d.close_on_error, d.persistent), (1883, 300.0, False, True))
        self.assertEqual(self.make(tls=True)[0].port, 8883)
        self.assertEqual(self.make(stale_after=60)[0].stale_after, 60.0)

    def test_not_connected_raises(self):
        d, h, _ = self.make()
        with self.assertRaisesRegex(drivers.DriverError, 'not connected'):
            d.connect()
        self.assertIsNotNone(d._client)     # client kept: paho keeps reconnecting in the background

    def test_cache_stale_and_resubscribe(self):
        d, h, clock = self.make(stale_after=120)
        h_c = None
        try:
            d.connect()
        except drivers.DriverError:
            pass
        h_c = h['c']
        h_c.up()
        d.connect()
        tags = [tag('farm/gen/temp'), tag('farm/gen/state|data.rpm'), tag('farm/none')]
        out = d.read(tags)
        self.assertEqual(sorted(h_c.subs), ['farm/gen/state', 'farm/gen/temp', 'farm/none'])
        self.assertTrue(all(e and 'no message yet' in e for _, e in out))
        h_c.msg('farm/gen/temp', b'93')
        h_c.msg('farm/gen/state', b'{"data":{"rpm":1800}}')
        out = d.read(tags)
        self.assertEqual(out[:2], [(93.0, None), (1800.0, None)])
        clock.advance(121)
        out = d.read(tags)
        self.assertTrue(out[0][1].startswith('stale'))
        h_c.msg('farm/gen/temp', b'94')
        self.assertEqual(d.read(tags)[0], (94.0, None))
        # reconnect re-subscribes everything (clean session)
        h_c.subs.clear()
        h_c.down()
        with self.assertRaisesRegex(drivers.DriverError, 'disconnected'):
            d.read(tags)
        h_c.up()
        self.assertEqual(sorted(h_c.subs), ['farm/gen/state', 'farm/gen/temp', 'farm/none'])
        d.close()
        self.assertTrue(h_c.stopped)


# ── DB-backed: settings, alerts, requests, web ───────────────────────────────
def setUpModule():
    global web, c
    if os.path.exists(settings.DB_PATH):
        os.remove(settings.DB_PATH)
    with open(settings.CONFIG_PATH, 'w') as f:
        f.write('{"threshold_gpm": 2000}')
    from plccc import web as webmod
    web = webmod
    web.init_db()
    c = web.app.test_client()


def make_chat_db(path, names=('Recharge Pump Alarm', 'Farm Alarms')):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, is_from_me INTEGER, date INTEGER, item_type INTEGER DEFAULT 0);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);''')
    for i, n in enumerate(names, 1):
        db.execute('INSERT INTO chat VALUES (?,?)', (i, n))
        db.executemany('INSERT INTO chat_handle_join VALUES (?,?)', [(i, 1), (i, 2), (i, 3)])
    db.commit()
    return db


def add_msg(db, chat_id, text, at, from_me=0):
    cur = db.execute('INSERT INTO message (text,is_from_me,date,item_type) VALUES (?,?,?,0)',
                     (text, from_me, int((at - APPLE_EPOCH_OFFSET) * 1e9)))
    db.execute('INSERT INTO chat_message_join VALUES (?,?)', (chat_id, cur.lastrowid))
    db.commit()


class ImessageSettingTests(unittest.TestCase):
    def setUp(self):
        conn = dbmod.connect()
        conn.execute('DELETE FROM app_settings')
        conn.execute('DELETE FROM service_requests')
        conn.close()

    def j(self, method, url, body=None):
        r = c.open(url, method=method, data=json.dumps(body) if body is not None else None,
                   content_type='application/json')
        return r.status_code, r.get_json(silent=True)

    def test_default_is_unchanged(self):
        self.assertEqual(dbmod.imessage_chat(), 'Recharge Pump Alarm')
        self.assertEqual(Alerter(dry_run=True).imessage_chat, 'Recharge Pump Alarm')
        s, d = self.j('GET', '/api/settings/imessage')
        self.assertEqual((s, d['chat_name'], d['is_default'], d['check']), (200, 'Recharge Pump Alarm', True, None))

    def test_setting_drives_send_and_ack(self):
        dbmod.set_setting('imessage_chat', 'Farm Alarms')
        a = Alerter(dry_run=False, telegram_token='T', telegram_chat='1',
                    run_cmd=lambda *a, **k: type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})())
        self.assertEqual(a.imessage_chat, 'Farm Alarms')
        cmds = []
        a._run_cmd = lambda cmd, **k: cmds.append(cmd) or type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        self.assertEqual(a.send_imessage_now('hello'), 'ok')
        self.assertIn('first chat whose name is "Farm Alarms"', cmds[0][2])
        # ack reader re-reads the name on every check (no restart needed)
        path = os.path.join(TMP, 'chat_v22.db')
        db = make_chat_db(path)
        now = time.time()
        add_msg(db, 1, 'ok', now)            # reply in the OLD group
        ack = AckChecker(ack_file=os.path.join(TMP, 'ack22'), chat_db=path, chat_name=lambda: dbmod.imessage_chat())
        self.assertFalse(ack.imessage_acked(since_timestamp=now - 10))
        add_msg(db, 2, 'on it', now)         # reply in the configured group
        self.assertTrue(ack.imessage_acked(since_timestamp=now - 10))
        dbmod.set_setting('imessage_chat', None)
        self.assertEqual(ack.chat_name, 'Recharge Pump Alarm')
        self.assertTrue(ack.imessage_acked(since_timestamp=now - 10))
        db.close()

    def test_static_name_still_supported(self):
        dbmod.set_setting('imessage_chat', 'Farm Alarms')
        self.assertEqual(Alerter(dry_run=True, imessage_chat='X').imessage_chat, 'X')
        self.assertEqual(AckChecker(chat_name='Y').chat_name, 'Y')

    def test_send_imessage_to_reports_without_telegram(self):
        a = Alerter(dry_run=False, telegram_token='T', telegram_chat='1',
                    run_cmd=lambda *x, **k: type('R', (), {'returncode': 1, 'stdout': '', 'stderr': "Can't get chat"})())
        self.assertEqual(a.send_imessage_to('t', 'Nope'), ('failed', "Can't get chat"))
        self.assertEqual(a._tq.qsize(), 0)
        self.assertEqual(Alerter(dry_run=True).send_imessage_to('t', 'Nope'), ('dry-run', None))

    def test_find_chat(self):
        path = os.path.join(TMP, 'chat_find.db')
        db = make_chat_db(path)
        add_msg(db, 2, 'hi', time.time() - 60)
        r = find_chat(path, 'farm alarms')
        self.assertEqual((r['found'], r['matches'], r['participants'], r['error']), (True, 1, 3, None))
        self.assertIsNotNone(r['last_message_at'])
        self.assertFalse(find_chat(path, 'Nope')['found'])
        self.assertIn('not found', find_chat(os.path.join(TMP, 'missing.db'), 'x')['error'])
        db.close()

    def test_api_set_check_and_reset(self):
        self.assertEqual(self.j('POST', '/api/settings/imessage', {'chat_name': 'Farm Alarms'})[0], 401)
        for bad in ('', '   ', 'x' * 101, 'a\nb', None, 5):
            self.assertEqual(self.j('POST', '/api/settings/imessage', {'chat_name': bad, 'password': PW})[0], 400, bad)
        s, d = self.j('POST', '/api/settings/imessage', {'chat_name': '  Farm Alarms ', 'password': PW})
        self.assertEqual((s, d['chat_name'], d['is_default']), (200, 'Farm Alarms', False))
        self.assertEqual(dbmod.imessage_chat(), 'Farm Alarms')
        self.assertEqual(d['check']['status'], 'pending')                 # check queued for the poller
        s, r = self.j('GET', '/api/requests/%d' % d['request_id'])
        self.assertEqual((s, r['kind'], r['payload']['chat_name']), (200, 'imessage_check', 'Farm Alarms'))
        # poller side answers the request
        from plccc.poller import RequestWorker
        path = os.path.join(TMP, 'chat_req.db')
        make_chat_db(path).close()
        rw = RequestWorker(Alerter(dry_run=True), chat_db=path)
        rw.process_pending()
        s, d = self.j('GET', '/api/settings/imessage')
        self.assertEqual((d['check']['status'], d['check']['result']['found']), ('done', True))
        # test message: queued, dry-run send, cooldown
        self.assertEqual(self.j('POST', '/api/settings/imessage/test', {})[0], 401)
        s, t = self.j('POST', '/api/settings/imessage/test', {'password': PW})
        self.assertEqual((s, t['chat_name']), (200, 'Farm Alarms'))
        self.assertEqual(self.j('POST', '/api/settings/imessage/test', {'password': PW})[0], 429)
        rw.process_pending()
        d = self.j('GET', '/api/settings/imessage')[1]
        self.assertEqual(d['test']['result']['status'], 'dry-run')
        s, k = self.j('POST', '/api/settings/imessage/check', {'password': PW})
        self.assertEqual(s, 200)
        # back to default -> setting row removed
        s, d = self.j('POST', '/api/settings/imessage', {'chat_name': 'Recharge Pump Alarm', 'password': PW})
        self.assertTrue(d['is_default'])
        self.assertIsNone(dbmod.get_setting('imessage_chat'))
        self.assertEqual(self.j('GET', '/api/requests/99999')[0], 404)

    def test_requests_expire(self):
        conn = dbmod.connect()
        conn.execute("INSERT INTO service_requests (kind,payload,created_at) VALUES ('imessage_test','{}',datetime('now','-10 minutes'))")
        conn.close()
        from plccc.poller import RequestWorker
        sent = []
        a = Alerter(dry_run=True)
        a.send_imessage_to = lambda *x: sent.append(x) or ('ok', None)
        RequestWorker(a).process_pending()
        conn = dbmod.connect()
        self.assertEqual(conn.execute('SELECT status FROM service_requests').fetchone()[0], 'expired')
        conn.close()
        self.assertEqual(sent, [])


class DeviceOptionsApiTests(unittest.TestCase):
    def j(self, method, url, body=None):
        r = c.open(url, method=method, data=json.dumps(body) if body is not None else None,
                   content_type='application/json')
        return r.status_code, r.get_json(silent=True)

    def test_opcua_device_roundtrip_hides_password(self):
        body = {'name': 'UA', 'type': 'opc_ua', 'host': 'opc.tcp://10.1.1.5:4841/x', 'port': 0, 'password': PW,
                'options': {'username': 'op', 'password': 's3cret', 'security_policy': 'None', 'security_mode': 'Sign'}}
        s, d = self.j('POST', '/api/devices', body)
        self.assertEqual(s, 201, d)
        self.assertEqual((d['port'], d['has_password'], d['options']), (4841, True, {'username': 'op'}))
        self.assertNotIn('s3cret', json.dumps(self.j('GET', '/api/devices')[1]))
        conn = dbmod.connect()
        raw = json.loads(conn.execute('SELECT options FROM devices WHERE id=?', (d['id'],)).fetchone()[0])
        conn.close()
        self.assertEqual(raw['password'], 's3cret')
        # blank password on edit keeps the saved one
        body['options'] = {'username': 'op', 'password': '', 'security_policy': 'Basic256Sha256', 'security_mode': 'Sign'}
        s, d2 = self.j('PUT', '/api/devices/%d' % d['id'], body)
        self.assertEqual((s, d2['has_password'], d2['options']['security_policy']), (200, True, 'Basic256Sha256'))
        # bare host becomes an endpoint URL
        body.update(host='10.1.1.6', options={})
        s, d3 = self.j('PUT', '/api/devices/%d' % d['id'], body)
        self.assertEqual((d3['host'], d3['port'], d3['has_password']), ('opc.tcp://10.1.1.6:4840', 4840, False))
        body['options'] = {'security_policy': 'Bogus'}
        self.assertEqual(self.j('PUT', '/api/devices/%d' % d['id'], body)[0], 400)
        self.assertEqual(self.j('DELETE', '/api/devices/%d' % d['id'], {'password': PW})[0], 200)

    def test_mqtt_device(self):
        body = {'name': 'Broker', 'type': 'mqtt', 'host': 'broker.local', 'port': 0, 'password': PW,
                'options': {'tls': True, 'client_id': 'farm1', 'stale_after': '5'}}
        s, d = self.j('POST', '/api/devices', body)
        self.assertEqual(s, 201, d)
        self.assertEqual((d['port'], d['options']), (8883, {'tls': True, 'client_id': 'farm1', 'stale_after': 10}))
        self.assertEqual(self.j('POST', '/api/devices', dict(body, type='bogus'))[0], 400)
        self.assertEqual(self.j('DELETE', '/api/devices/%d' % d['id'], {'password': PW})[0], 200)

    def test_legacy_device_has_no_options(self):
        s, devs = self.j('GET', '/api/devices')
        self.assertEqual(devs[0]['options'], {})
        self.assertFalse(devs[0]['has_password'])

    def test_version(self):
        from plccc import __version__
        self.assertGreaterEqual(tuple(map(int, __version__.split('.'))), (2, 2, 0))


class WorkerKeepsMqttSubscription(unittest.TestCase):
    def test_close_on_error_false_is_honoured(self):
        from plccc import poller as v2poller
        conn = dbmod.connect()
        dbmod.migrate(conn)
        cur = conn.execute("INSERT INTO devices (name,type,host,port) VALUES ('MQ','mqtt','b',1883)")
        did = cur.lastrowid
        conn.execute("INSERT INTO tags (device_id,name,address) VALUES (?,?,?)", (did, 'T', 'a/b'))
        devrow = conn.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone()

        class D:
            persistent, close_on_error, port = True, False, 1883
            closed = 0

            def connect(self):
                pass

            def close(self):
                D.closed += 1

            def read(self, tags):
                return [(None, 'stale')] * len(tags)
        fake = type('A', (), {'send': lambda *a, **k: True})()
        with mock.patch.object(v2poller, 'make_driver', lambda d: D()):
            w = v2poller.DeviceWorker(devrow, alerter=fake, registry=v2poller.Registry(), interval=60, clock=FakeClock())
        self.assertFalse(w.poll_once(conn))
        self.assertEqual(D.closed, 0)
        import inspect
        self.assertIn("'options'", inspect.getsource(v2poller._sig))
        conn.execute('DELETE FROM devices WHERE id=?', (did,))
        conn.execute('DELETE FROM tags WHERE device_id=?', (did,))
        conn.close()


if __name__ == '__main__':
    unittest.main()
