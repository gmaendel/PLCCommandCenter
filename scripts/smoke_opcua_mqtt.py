"""Real-network smoke test for the OPC UA + MQTT drivers (never touches the live DB/config).

  * starts a local asyncua OPC UA server on 127.0.0.1:48400 (anonymous + username, None + Basic256Sha256)
  * starts a local mosquitto broker on 127.0.0.1:18830 (brew install mosquitto)
  * reads both through the real drivers AND through a DeviceWorker into a throw-away SQLite DB

Run:  PYTHONPATH=app:. venv/bin/python scripts/smoke_opcua_mqtt.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix='plccc-smoke-')
os.environ.update({'PLCCC_ENV_FILE': os.path.join(TMP, 'no.env'), 'PLCCC_DRY_RUN': '1',
                   'PLCCC_DATA_DIR': TMP, 'PLCCC_DB_PATH': os.path.join(TMP, 'smoke.db'),
                   'PLCCC_CONFIG_PATH': os.path.join(TMP, 'pump_config.json'), 'PLCCC_ACK_FILE': os.path.join(TMP, 'ack'),
                   'DASHBOARD_PASSWORD': 'x', 'TELEGRAM_BOT_TOKEN': 'T', 'TELEGRAM_CHAT_ID': '0'})
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

import logging  # noqa: E402
logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s %(message)s')
for n in ('asyncua', 'asyncua.server', 'asyncua.client', 'asyncua.uaprotocol'):
    logging.getLogger(n).setLevel(logging.ERROR)

from plccc import drivers, db as dbmod, poller  # noqa: E402

UA_PORT, MQ_PORT = 48400, 18830
RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond)))
    print('%s %s %s' % ('PASS' if cond else 'FAIL', name, detail))


# ── OPC UA server ────────────────────────────────────────────────────────────
class Users:
    def get_user(self, iserver, username=None, password=None, certificate=None):
        from asyncua.crypto.permission_rules import User, UserRole
        if username is None:
            return User(role=UserRole.User)            # anonymous
        if username == 'operator' and password == 'pw123':
            return User(role=UserRole.User, name=username)
        return None


def start_opcua():
    ready, state = threading.Event(), {}

    async def main():
        from asyncua import Server, ua
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        from cryptography.x509.oid import ExtendedKeyUsageOID
        from pathlib import Path
        cert, key = Path(TMP, 'srv-cert.der'), Path(TMP, 'srv-key.pem')
        await setup_self_signed_certificate(key, cert, 'urn:plccc:smoke:server', 'localhost',
                                            [ExtendedKeyUsageOID.SERVER_AUTH], {'commonName': 'smoke'})
        srv = Server(user_manager=Users())
        await srv.init()
        srv.set_endpoint('opc.tcp://127.0.0.1:%d/smoke/' % UA_PORT)
        srv.set_security_policy([ua.SecurityPolicyType.NoSecurity,
                                 ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
                                 ua.SecurityPolicyType.Basic256Sha256_Sign])
        await srv.load_certificate(str(cert))
        await srv.load_private_key(str(key))
        idx = await srv.register_namespace('urn:plccc:smoke')   # -> ns=2
        obj = await srv.nodes.objects.add_object(idx, 'Pump')
        gpm = await obj.add_variable(ua.NodeId('Pump.GPM', idx), 'GPM', 612.5)
        run = await obj.add_variable(ua.NodeId('Pump.Run', idx), 'Run', True)
        cnt = await obj.add_variable(ua.NodeId(1002, idx), 'Count', ua.Variant(7, ua.VariantType.Int32))
        txt = await obj.add_variable(ua.NodeId('Pump.Text', idx), 'Text', 'hello')
        state.update(srv=srv, gpm=gpm, loop=asyncio.get_running_loop(), stop=asyncio.Event())
        async with srv:
            ready.set()
            await state['stop'].wait()

    t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    t.start()
    if not ready.wait(30):
        raise SystemExit('OPC UA server did not start')
    return state


def ua_set(state, value):
    fut = asyncio.run_coroutine_threadsafe(state['gpm'].write_value(value), state['loop'])
    fut.result(5)


def dev_row(**kw):
    d = {'id': 1, 'name': 'smoke', 'type': 'opc_ua', 'host': '', 'port': 0, 'rack': 0, 'slot': 1, 'unit_id': 1,
         'word_swap': 0, 'options': None}
    d.update(kw)
    return type('Row', (dict,), {'keys': lambda self: list(dict.keys(self))})(d)


def tag(addr):
    return type('Row', (dict,), {'keys': lambda self: list(dict.keys(self))})({'id': 1, 'name': addr, 'address': addr})


def smoke_opcua():
    st = start_opcua()
    url = 'opc.tcp://127.0.0.1:%d/smoke/' % UA_PORT
    tags = [tag('ns=2;s=Pump.GPM'), tag('ns=2;s=Pump.Run'), tag('ns=2;i=1002'), tag('ns=2;s=Missing'), tag('ns=2;s=Pump.Text')]
    d = drivers.OpcUaDriver(dev_row(host=url))
    d.connect()
    out = d.read(tags)
    check('opcua anonymous read', out[:3] == [(612.5, None), (1.0, None), (7.0, None)], out)
    check('opcua unknown node is a per-tag error', out[3][0] is None and 'BadNodeIdUnknown' in (out[3][1] or ''), out[3])
    check('opcua text node is a per-tag error', out[4][0] is None and 'non-numeric' in out[4][1], out[4])
    ua_set(st, 590.25)
    check('opcua sees new value', d.read(tags[:1]) == [(590.25, None)])
    d.close()

    d = drivers.OpcUaDriver(dev_row(host=url, options=json.dumps({'username': 'operator', 'password': 'pw123'})))
    d.connect()
    check('opcua username login', d.read(tags[:1]) == [(590.25, None)])
    d.close()
    d = drivers.OpcUaDriver(dev_row(host=url, options=json.dumps({'username': 'operator', 'password': 'wrong'})))
    try:
        d.connect()
        check('opcua bad password rejected', False)
    except drivers.DriverError as e:
        check('opcua bad password rejected', True, str(e)[:90])
    for mode in ('SignAndEncrypt', 'Sign'):
        d = drivers.OpcUaDriver(dev_row(host=url, options=json.dumps(
            {'security_policy': 'Basic256Sha256', 'security_mode': mode})))
        try:
            d.connect()
            check('opcua Basic256Sha256 %s' % mode, d.read(tags[:1]) == [(590.25, None)])
        except Exception as e:
            check('opcua Basic256Sha256 %s' % mode, False, e)
        finally:
            d.close()
    d = drivers.OpcUaDriver(dev_row(host='opc.tcp://127.0.0.1:48499/'))
    try:
        d.connect()
        check('opcua offline server -> DriverError', False)
    except drivers.DriverError:
        check('opcua offline server -> DriverError', True)
    return st, url


# ── MQTT ─────────────────────────────────────────────────────────────────────
def mosquitto_bin():
    for p in ('/opt/homebrew/sbin/mosquitto', '/usr/local/sbin/mosquitto', shutil.which('mosquitto') or ''):
        if p and os.path.exists(p):
            return p
    return None


def start_broker():
    conf = os.path.join(TMP, 'mosq.conf')
    with open(conf, 'w') as f:
        f.write('listener %d 127.0.0.1\nallow_anonymous true\npersistence false\n' % MQ_PORT)
    p = subprocess.Popen([mosquitto_bin(), '-c', conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if drivers.tcp_probe('127.0.0.1', MQ_PORT, timeout=0.2):
            return p
        time.sleep(0.1)
    raise SystemExit('mosquitto did not start')


def publish(topic, payload, retain=True):
    import paho.mqtt.publish as pub
    pub.single(topic, payload, hostname='127.0.0.1', port=MQ_PORT, retain=retain, qos=1)


def smoke_mqtt():
    if not mosquitto_bin():
        print('SKIP mqtt: mosquitto not installed (brew install mosquitto)')
        return None
    broker = start_broker()
    publish('plccc/smoke/gen', json.dumps({'temp': 93, 'data': {'rpm': 1800}}))
    publish('plccc/smoke/psi', '42.5')
    d = drivers.MqttDriver(dev_row(type='mqtt', host='127.0.0.1', port=MQ_PORT, options=json.dumps({'stale_after': 30})))
    tags = [tag('plccc/smoke/gen|temp'), tag('plccc/smoke/gen|data.rpm'), tag('plccc/smoke/psi'), tag('plccc/smoke/none')]
    d.connect()
    out = d.read(tags)
    check('mqtt retained values via JSON path + plain number', out[:3] == [(93.0, None), (1800.0, None), (42.5, None)], out)
    check('mqtt topic without messages is a per-tag error', out[3][0] is None and 'no message' in out[3][1], out[3])
    publish('plccc/smoke/psi', '44.0', retain=False)
    time.sleep(0.5)
    check('mqtt live update cached', d.read(tags[2:3]) == [(44.0, None)])
    d.stale_after = 1
    time.sleep(1.3)
    out = d.read(tags[2:3])
    check('mqtt value goes stale', out[0][0] is None and out[0][1].startswith('stale'), out)
    d.stale_after = 30
    broker.terminate()
    broker.wait(5)
    time.sleep(1.0)
    try:
        d.read(tags)
        check('mqtt broker down -> poll fails', False)
    except drivers.DriverError as e:
        check('mqtt broker down -> poll fails', True, e)
    broker = start_broker()
    publish('plccc/smoke/psi', '45.5')
    t0 = time.time()
    while time.time() - t0 < 40:
        try:
            d.connect()
            if d.read(tags[2:3]) == [(45.5, None)]:
                break
        except drivers.DriverError:
            pass
        time.sleep(0.5)
    check('mqtt auto-reconnects + resubscribes', d.read(tags[2:3]) == [(45.5, None)], '%.1fs' % (time.time() - t0))
    d.close()
    return broker


# ── through the real poller worker into a throw-away DB ─────────────────────
def smoke_worker(url):
    conn = dbmod.connect()
    dbmod.migrate(conn)
    conn.execute("INSERT INTO devices (id,name,type,host,port) VALUES (11,'UA smoke','opc_ua',?,%d)" % UA_PORT, (url,))
    conn.execute("INSERT INTO tags (device_id,name,address,units) VALUES (11,'GPM','ns=2;s=Pump.GPM','GPM')")
    conn.execute("INSERT INTO devices (id,name,type,host,port) VALUES (12,'MQTT smoke','mqtt','127.0.0.1',%d)" % MQ_PORT)
    conn.execute("INSERT INTO tags (device_id,name,address,units) VALUES (12,'Temp','plccc/smoke/gen|temp','F')")

    class A:
        def send(self, *a, **k):
            return True
    for did in (11, 12):
        dev = conn.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone()
        w = poller.DeviceWorker(dev, alerter=A(), registry=poller.Registry(), interval=60)
        ok = w.poll_once(conn)
        w.driver.close()
        val = conn.execute('SELECT value FROM tag_readings r JOIN tags t ON t.id=r.tag_id WHERE t.device_id=? '
                           'ORDER BY r.rowid DESC LIMIT 1', (did,)).fetchone()
        rt = conn.execute('SELECT online FROM device_runtime WHERE device_id=?', (did,)).fetchone()
        check('worker poll %s' % dev['name'], ok and val is not None and rt['online'] == 1, val and val[0])
    conn.close()


if __name__ == '__main__':
    st, url = smoke_opcua()
    broker = smoke_mqtt()
    if broker:
        publish('plccc/smoke/gen', json.dumps({'temp': 95}))
        smoke_worker(url)
        broker.terminate()
    st['loop'].call_soon_threadsafe(st['stop'].set)
    time.sleep(0.5)
    shutil.rmtree(TMP, ignore_errors=True)
    bad = [n for n, ok in RESULTS if not ok]
    print('\n%d/%d smoke checks passed' % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)
