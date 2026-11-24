"""Side-by-side: drive the ORIGINAL v1 pump_monitor.main() and the v2 DeviceWorker with
the same simulated PLC (value as a function of a fake clock) and compare every alert
(text without the wall-clock 'Time:' line, plus the simulated second it was sent).

Also compares v1 check_alarm() of modbus/siemens/ab monitors with v2 ThresholdAlarm.
"""
import os
import re
import sqlite3
import types
import unittest
from unittest import mock

from tests._env import FakeClock, TMP
import plccc_settings as settings
from plccc import db as dbmod
from plccc import poller as v2poller
from plccc.alarms import ThresholdAlarm

T0 = 1_800_000_000.0


class StopSim(BaseException):
    pass


def norm(text):
    # v2 deliberately changed the reply hint (any reply acks, not only 'ack'); compare the rest.
    text = text.replace("(Reply 'ack' to silence further alerts)", '(Reply to this message to acknowledge)')
    text = text.replace("(Reply 'ack' to acknowledge)", '(Reply to this message to acknowledge)')
    return re.sub(r'Time: [0-9: -]+', 'Time: <t>', text)


class FakeTag:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error


class Scenario:
    """value_at(t) -> int GPM | 'fail' (read error) | 'exc' (exception); port_open(t) -> bool."""

    def __init__(self, value_at, port_open=lambda t: False, ack_at=None, enabled_at=lambda t: True, threshold=2000):
        self.value_at, self.port_open, self.ack_at, self.enabled_at, self.threshold = \
            value_at, port_open, ack_at, enabled_at, threshold


def write_threshold(v):
    with open(settings.CONFIG_PATH, 'w') as f:
        f.write('{"threshold_gpm": %d}' % v)


# ── v1 runner ────────────────────────────────────────────────────────────────
def run_v1(sc, seconds):
    import pump_monitor as v1
    clock = FakeClock(T0)
    sent = []
    write_threshold(sc.threshold)
    ack_file = os.path.join(TMP, 'v1_ack')

    class FakeSLC:
        def __init__(self, path):
            self.connected = False

        def open(self):
            self.connected = True

        def close(self):
            self.connected = False

        def read(self, *tags):
            v = sc.value_at(clock() - T0)
            if v == 'exc':
                raise IOError('socket timeout')
            if v == 'fail':
                return [FakeTag(None, 'err'), FakeTag(None, 'err')]
            return [FakeTag(v), FakeTag(1)]

    def sleep(s):
        clock.advance(s)
        if clock() - T0 >= seconds:
            raise StopSim()

    patches = [
        mock.patch.object(v1, 'SLCDriver', FakeSLC),
        mock.patch.object(v1, 'send_telegram_message', lambda t: sent.append((clock() - T0, norm(t)))),
        mock.patch.object(v1, 'send_sms_message', lambda t: None),
        mock.patch.object(v1, 'check_imessage_for_ack',
                          lambda since_timestamp=None: sc.ack_at is not None and clock() - T0 >= sc.ack_at),
        mock.patch.object(v1, 'ping_plc', lambda host, port=None: sc.port_open(clock() - T0)),
        mock.patch.object(v1, 'gpm_alarm_enabled', lambda conn: sc.enabled_at(clock() - T0)),
        mock.patch.object(v1, 'ACK_FILE', ack_file),
        mock.patch.object(v1, 'time', types.SimpleNamespace(time=clock, sleep=sleep)),
    ]
    for p in patches:
        p.start()
    try:
        with mock.patch('builtins.print'):
            v1.main()
    except StopSim:
        pass
    finally:
        for p in patches:
            p.stop()
        if os.path.exists(ack_file):
            os.remove(ack_file)
    return sent


# ── v2 runner ────────────────────────────────────────────────────────────────
class CapAlerter:
    def __init__(self, clock):
        self.clock, self.sent = clock, []

    def send(self, text, kind='alarm', source=''):
        self.sent.append((self.clock() - T0, norm(text)))
        return True


def fresh_db():
    if os.path.exists(settings.DB_PATH):
        os.remove(settings.DB_PATH)
    conn = dbmod.connect()
    dbmod.migrate(conn)
    conn.execute("INSERT INTO devices (id,name,type,host,port) VALUES (1,'Recharge Pump (MicroLogix)','allen_bradley_slc','pump-plc.example.com',44818)")
    conn.execute("INSERT INTO tags (id,device_id,name,address,data_type,units,alarm_enabled,alarm_low,log_source) VALUES "
                 "(1,1,'Pump GPM','N7:6','INT','GPM',1,2000,'{\"table\":\"flow_log\",\"column\":\"gpm\"}')")
    conn.execute("INSERT INTO tags (id,device_id,name,address,data_type,units,log_source) VALUES "
                 "(2,1,'Water Level','N7:1','INT','','{\"table\":\"flow_log\",\"column\":\"level\"}')")
    dbmod.migrate(conn)      # seeds the escalation profile + pump comm profile
    return conn


def run_v2(sc, seconds):
    conn = fresh_db()
    clock = FakeClock(T0)
    write_threshold(sc.threshold)
    alerter = CapAlerter(clock)

    class FakeDriver:
        persistent = True

        def __init__(self, dev):
            self.port = dev['port']
            self.connected = False

        def connect(self):
            self.connected = True

        def close(self):
            self.connected = False

        def read(self, tags):
            v = sc.value_at(clock() - T0)
            if v == 'exc':
                raise IOError('socket timeout')
            if v == 'fail':
                return [(None, 'err')] * len(tags)
            return [(v, None), (1, None)][:len(tags)]

    dev = conn.execute('SELECT * FROM devices WHERE id=1').fetchone()
    with mock.patch.object(v2poller, 'make_driver', FakeDriver), \
            mock.patch.object(v2poller, 'tcp_probe', lambda h, p: sc.port_open(clock() - T0)), \
            mock.patch('plccc.alerts.AckChecker.imessage_acked',
                       lambda self, since_timestamp=None: sc.ack_at is not None and clock() - T0 >= sc.ack_at):
        w = v2poller.DeviceWorker(dev, alerter=alerter, registry=v2poller.Registry(), interval=60, clock=clock)
        while clock() - T0 < seconds:
            en = 1 if sc.enabled_at(clock() - T0) else 0
            conn.execute('UPDATE tags SET alarm_enabled=? WHERE id=1', (en,))
            w.poll_once(conn)
            clock.advance(60)
    conn.close()
    if os.path.exists(settings.ACK_FILE):
        os.remove(settings.ACK_FILE)
    return alerter.sent


class PumpEquivalence(unittest.TestCase):
    maxDiff = None

    def check(self, sc, seconds):
        a, b = run_v1(sc, seconds), run_v2(sc, seconds)
        self.assertTrue(a, 'scenario produced no v1 alerts')
        self.assertEqual(a, b)
        return a

    def test_sustained_low_flow(self):
        out = self.check(Scenario(lambda t: 500), 30 * 60)
        self.assertEqual([s for s, _ in out], [0, 420, 840])

    def test_recover_then_low_again(self):
        self.check(Scenario(lambda t: 500 if t < 300 or t >= 600 else 2600), 40 * 60)

    def test_flapping(self):
        self.check(Scenario(lambda t: 500 if (t // 60) % 3 else 2500), 30 * 60)

    def test_imessage_ack(self):
        out = self.check(Scenario(lambda t: 400, ack_at=200), 30 * 60)
        self.assertEqual(len(out), 1)

    def test_ack_after_escalation(self):
        out = self.check(Scenario(lambda t: 400, ack_at=500), 30 * 60)
        self.assertEqual(len(out), 2)

    def test_alarm_switch_toggled(self):
        self.check(Scenario(lambda t: 400, enabled_at=lambda t: t < 200 or t >= 900), 40 * 60)

    def test_threshold_raised(self):
        self.check(Scenario(lambda t: 1800, threshold=2000), 20 * 60)

    def test_comm_loss_port_closed_then_recovery(self):
        out = self.check(Scenario(lambda t: 'exc' if 120 <= t < 1500 else 2500), 40 * 60)
        texts = [t for _, t in out]
        self.assertTrue(texts[0].startswith('\U0001f6a8 COMM LOSS: Cannot reach PLC at pump-plc.example.com:44818.'))
        self.assertEqual(texts[1], '\u2705 RECOVERY: Communication to PLC pump-plc.example.com:44818 has been restored!')

    def test_read_errors_port_open_no_alert(self):
        sc = Scenario(lambda t: 'fail' if 60 <= t < 1800 else 2500, port_open=lambda t: True)
        self.assertEqual(run_v1(sc, 40 * 60), [])
        self.assertEqual(run_v2(sc, 40 * 60), [])

    def test_comm_loss_during_alarm(self):
        self.check(Scenario(lambda t: 'fail' if 300 <= t < 1200 else 600), 40 * 60)


class ThresholdEquivalence(unittest.TestCase):
    SEQ = [5, 50, 70, 80, 55, 9, 9, 30, 61, 59, 10, 10.0, 60, 60.5, 3, 45]

    def _v1(self, module, enabled_seq):
        mod = __import__(module)
        sent = []
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE tag_alarm_state (tag_id INTEGER PRIMARY KEY, active INTEGER, triggered_at, last_value)')
        with mock.patch.object(mod, 'send_alarm', lambda t: sent.append(t)):
            mod.alarm_states.clear()
            for v, en in zip(self.SEQ, enabled_seq):
                mod.check_alarm(9, 'PSI', 'Domestic Water', 'psi', v, en, 10.0, 60.0, '', conn)
        return sent

    def _v2(self, enabled_seq):
        ta, sent = ThresholdAlarm(), []
        for v, en in zip(self.SEQ, enabled_seq):
            k, m = ta.evaluate(value=v, alarm_enabled=en, alarm_low=10.0, alarm_high=60.0, alarm_message='',
                               device_name='Domestic Water', tag_name='PSI', units='psi')
            if k:
                sent.append(m)
        return sent

    def test_all_v1_monitors_match(self):
        for enabled in ([1] * len(self.SEQ), [1, 1, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1, 1, 1]):
            ref = self._v2(enabled)
            self.assertTrue(ref)
            for m in ('modbus_monitor', 'siemens_monitor', 'ab_monitor'):
                self.assertEqual([norm(t) for t in self._v1(m, enabled)], ref, m)

    def test_custom_message(self):
        ta = ThresholdAlarm()
        k, m = ta.evaluate(value=1, alarm_enabled=1, alarm_low=5, alarm_high=None, alarm_message='  Tank low!  ',
                           device_name='D', tag_name='T', units='ft')
        self.assertEqual((k, m), ('alarm', 'Tank low!'))


if __name__ == '__main__':
    unittest.main()
