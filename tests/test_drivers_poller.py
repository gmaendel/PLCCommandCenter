"""Address parsing/decoding (verbatim from v1) and the DeviceWorker DB writes / comm logic."""
import os
import struct
import unittest
from unittest import mock

from tests._env import FakeClock
import plccc_settings as settings
from plccc import db as dbmod
from plccc import drivers
from plccc import poller as v2poller


class DecodeTests(unittest.TestCase):
    def test_modbus_parse(self):
        self.assertEqual(drivers.modbus_parse_address('HR4800'), (3, 4799))
        self.assertEqual(drivers.modbus_parse_address('ir200'), (4, 199))
        self.assertEqual(drivers.modbus_parse_address('CO5'), (1, 4))
        self.assertEqual(drivers.modbus_parse_address('DI1'), (2, 0))
        self.assertEqual(drivers.modbus_parse_address('40001'), (3, 40000))

    def test_modbus_decode(self):
        hi, lo = struct.unpack('>HH', struct.pack('>f', 12.5))
        self.assertEqual(drivers.modbus_decode([hi, lo], 'REAL'), 12.5)
        self.assertEqual(drivers.modbus_decode([lo, hi], 'REAL', word_swap=True), 12.5)
        self.assertEqual(drivers.modbus_decode([0xFFFF, 0xFFFE], 'DINT'), -2.0)
        self.assertEqual(drivers.modbus_decode([99, 0], 'DINT', word_swap=True), 99.0)
        self.assertEqual(drivers.modbus_decode([0x8000], 'INT'), -32768.0)
        self.assertEqual(drivers.modbus_decode([0x8000], 'WORD'), 32768.0)

    def test_s7_parse(self):
        self.assertEqual(drivers.s7_parse_address('DB6.REAL108'), (6, 'REAL', 108, 0))
        self.assertEqual(drivers.s7_parse_address('db6.dbd44'), (6, 'REAL', 44, 0))
        self.assertEqual(drivers.s7_parse_address('DB1.INT8'), (1, 'INT', 8, 0))
        self.assertEqual(drivers.s7_parse_address('DB4.DBX4.2'), (4, 'BOOL', 4, 2))
        with self.assertRaises(ValueError):
            drivers.s7_parse_address('M0.0')

    def test_s7_decode(self):
        self.assertEqual(drivers.s7_decode(bytearray(struct.pack('>f', 458.03125)), 'REAL'), 458.03125)
        self.assertEqual(drivers.s7_decode(bytearray(struct.pack('>h', -5)), 'INT'), -5.0)
        self.assertEqual(drivers.s7_decode(bytearray([0b100]), 'BOOL', 2), 1.0)

    def test_scaling(self):
        t = {'scale_factor': 0.01, 'scale_offset': 0.0}
        k = type('R', (dict,), {'keys': dict.keys})
        self.assertEqual(drivers.apply_scaling(4689, k(t)), 46.89)
        self.assertEqual(drivers.apply_scaling(7, k({'scale_factor': 1.0, 'scale_offset': 0.0})), 7)
        self.assertEqual(drivers.apply_scaling(7, k({'scale_factor': None, 'scale_offset': None})), 7)

    def test_only_read_methods(self):
        for cls in drivers.DRIVERS.values():
            self.assertFalse([m for m in dir(cls) if 'write' in m.lower()], cls)


class FakeAlerter:
    def __init__(self):
        self.sent = []

    def send(self, text, kind='alarm', source=''):
        self.sent.append((kind, text))
        return True


def make_db():
    if os.path.exists(settings.DB_PATH):
        os.remove(settings.DB_PATH)
    conn = dbmod.connect()
    dbmod.migrate(conn)
    conn.executescript('''
      INSERT INTO devices (id,name,type,host,port,unit_id,word_swap) VALUES
        (1,'Recharge Pump (MicroLogix)','allen_bradley_slc','pump-plc.example.com',44818,1,0),
        (2,'Aquifer Monitor (S7-1200)','siemens_s7','pump-plc.example.com',102,1,0),
        (3,'Generator','modbus_tcp','192.0.2.78',502,125,1);
      INSERT INTO tags (id,device_id,name,address,data_type,units,alarm_enabled,alarm_low,alarm_high,log_source) VALUES
        (1,1,'Pump GPM','N7:6','INT','GPM',0,2000,NULL,'{"table":"flow_log","column":"gpm"}'),
        (2,1,'Water Level','N7:1','INT','',0,NULL,NULL,'{"table":"flow_log","column":"level"}'),
        (3,2,'Aquifer Level','DB6.REAL108','REAL','ft',0,NULL,NULL,'{"table":"aquifer_log","column":"level"}'),
        (4,3,'RPM','HR4796','DINT','RPM',1,NULL,100,NULL),
        (6,3,'TempF','HR4800','DINT','°F',0,NULL,NULL,NULL);''')
    dbmod.migrate(conn)
    return conn


class ScriptDriver:
    def __init__(self, dev, script):
        self.dev, self.script, self.port = dev, script, dev['port']
        self.persistent = dev['type'] != 'modbus_tcp'
        self.closed = 0

    def connect(self):
        if self.script and self.script[0] == 'noconnect':
            self.script.pop(0)
            raise drivers.DriverError('Could not connect')

    def close(self):
        self.closed += 1

    def read(self, tags):
        item = self.script.pop(0)
        if item == 'exc':
            raise IOError('boom')
        return item


class WorkerTests(unittest.TestCase):
    def worker(self, conn, dev_id, script):
        dev = conn.execute('SELECT * FROM devices WHERE id=?', (dev_id,)).fetchone()
        alerter = FakeAlerter()
        with mock.patch.object(v2poller, 'make_driver', lambda d: ScriptDriver(d, script)):
            w = v2poller.DeviceWorker(dev, alerter=alerter, registry=v2poller.Registry(), interval=60, clock=FakeClock())
        return w, alerter

    def test_pump_writes_flow_log_and_tag_readings(self):
        conn = make_db()
        w, _ = self.worker(conn, 1, [[(490, None), (1, None)]])
        self.assertTrue(w.poll_once(conn))
        self.assertEqual(tuple(conn.execute('SELECT gpm, level FROM flow_log').fetchone()), (490.0, 1.0))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM tag_readings').fetchone()[0], 2)
        rt = conn.execute('SELECT * FROM device_runtime WHERE device_id=1').fetchone()
        self.assertEqual((rt['online'], rt['fail_count']), (1, 0))

    def test_pump_partial_read_is_failure_and_writes_nothing(self):
        conn = make_db()
        w, _ = self.worker(conn, 1, [[(490, None), (None, 'err')]])
        self.assertFalse(w.poll_once(conn))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM flow_log').fetchone()[0], 0)
        self.assertEqual(w.reg.comm[1]['fail_count'], 1)

    def test_aquifer_dual_write_rounded(self):
        conn = make_db()
        w, _ = self.worker(conn, 2, [[(458.0313, None)]])
        w.poll_once(conn)
        self.assertEqual(conn.execute('SELECT level FROM aquifer_log').fetchone()[0], 458.03)
        self.assertEqual(conn.execute('SELECT value FROM tag_readings WHERE tag_id=3').fetchone()[0], 458.0313)

    def test_generic_threshold_alarm_and_state_table(self):
        conn = make_db()
        w, al = self.worker(conn, 3, [[(150.0, None), (99.0, None)], [(160.0, None), (99.0, None)], [(0.0, None), (99.0, None)]])
        for _ in range(3):
            w.poll_once(conn)
        self.assertEqual([k for k, _ in al.sent], ['alarm', 'clear'])
        self.assertEqual(al.sent[0][1], "\u26a0\ufe0f ALARM [Generator] RPM = 150.0 RPM \u2014 above high limit of 100.0 RPM\n(Reply to this message to acknowledge)")
        self.assertEqual(al.sent[1][1], '\u2705 CLEAR [Generator] RPM = 0.0 RPM \u2014 alarm cleared')
        row = conn.execute('SELECT active, triggered_at FROM tag_alarm_state WHERE tag_id=4').fetchone()
        self.assertEqual((row[0], row[1]), (0, None))

    def test_generic_comm_loss_after_3_then_recovery(self):
        conn = make_db()
        w, al = self.worker(conn, 3, ['noconnect', 'noconnect', 'noconnect', 'noconnect', [(0.0, None), (99.0, None)]])
        for _ in range(5):
            w.poll_once(conn)
        self.assertEqual([k for k, _ in al.sent], ['comm_loss', 'recovery'])
        self.assertTrue(al.sent[0][1].startswith('\U0001f534 COMM LOSS [Generator]\nModbus TCP device unreachable at 192.0.2.78:502\nTime: '))
        self.assertTrue(al.sent[0][1].endswith('\nMonitoring will resume automatically when it comes back online.'))
        self.assertTrue(al.sent[1][1].startswith('\u2705 RECOVERY [Generator]\nModbus TCP device back online at 192.0.2.78:502\nTime: '))

    def test_generic_partial_tag_error_still_ok(self):
        conn = make_db()
        w, al = self.worker(conn, 3, [[(None, 'Modbus error'), (99.0, None)]])
        self.assertTrue(w.poll_once(conn))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM tag_readings').fetchone()[0], 1)

    def test_scaled_value(self):
        conn = make_db()
        conn.execute('UPDATE tags SET scale_factor=0.01 WHERE id=6')
        w, _ = self.worker(conn, 3, [[(0.0, None), (4689.0, None)]])
        w.poll_once(conn)
        self.assertEqual(conn.execute('SELECT value FROM tag_readings WHERE tag_id=6').fetchone()[0], 46.89)

    def test_pump_escalation_writes_alarm_state(self):
        conn = make_db()
        conn.execute('UPDATE tags SET alarm_enabled=1 WHERE id=1')
        w, al = self.worker(conn, 1, [[(490, None), (1, None)]])
        w.poll_once(conn)
        self.assertEqual(al.sent[0][0], 'alarm')
        self.assertIn('Current GPM: 490\n', al.sent[0][1])
        row = conn.execute('SELECT active, stage FROM tag_alarm_state WHERE tag_id=1').fetchone()
        self.assertEqual(tuple(row), (1, 1))

    def test_migration_is_additive_and_idempotent(self):
        conn = make_db()
        before = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
        dbmod.migrate(conn)
        after = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
        self.assertEqual(before, after)
        cfg = dbmod.loads(conn.execute('SELECT alarm_config FROM tags WHERE id=1').fetchone()[0])
        self.assertEqual(cfg['escalation_wait_s'], 400)
        self.assertIsNone(conn.execute('SELECT alarm_config FROM tags WHERE id=2').fetchone()[0])
        self.assertEqual(dbmod.loads(conn.execute('SELECT comm_config FROM devices WHERE id=1').fetchone()[0])['offline_after'], 10)


if __name__ == '__main__':
    unittest.main()
