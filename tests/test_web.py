"""Dashboard: every v1 route keeps its contract, new routes work, password protection."""
import json
import os
import unittest

from tests._env import TMP  # noqa: F401
import plccc_settings as settings
from plccc import db as dbmod

PW = 'test-pw'


def setUpModule():
    global web, c
    if os.path.exists(settings.DB_PATH):
        os.remove(settings.DB_PATH)
    with open(settings.CONFIG_PATH, 'w') as f:
        f.write('{"threshold_gpm": 2000}')
    from plccc import web as webmod
    web = webmod
    web.init_db()        # empty DB -> v1 seed rows (pump + aquifer)
    conn = dbmod.connect()
    conn.execute("INSERT INTO flow_log (timestamp,gpm,level) VALUES (datetime('now','-30 seconds'),490,1)")
    conn.execute("INSERT INTO aquifer_log (timestamp,level) VALUES (datetime('now','-30 seconds'),458.03)")
    conn.close()
    c = web.app.test_client()


class WebTests(unittest.TestCase):
    def j(self, method, url, body=None):
        r = c.open(url, method=method, data=json.dumps(body) if body is not None else None,
                   content_type='application/json')
        return r.status_code, r.get_json(silent=True)

    def test_pages(self):
        for url in ('/', '/classic', '/manifest.webmanifest', '/apple-touch-icon.png', '/favicon.ico',
                    '/static/app.js', '/static/app.css', '/static/vendor/chart.umd.min.js', '/healthz'):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            r.close()
        self.assertIn(b'viewport-fit=cover', c.get('/').data)
        self.assertIn(b'PLC Command Center', c.get('/classic').data)

    def test_v1_get_endpoints(self):
        self.assertEqual(self.j('GET', '/api/threshold'), (200, {'threshold_gpm': 2000}))
        s, d = self.j('GET', '/api/data?tab=pump&hours=1')
        self.assertEqual(s, 200)
        self.assertEqual(set(d[-1]), {'x', 'y', 'level'})
        self.assertTrue(d[-1]['x'].endswith('Z'))
        s, d = self.j('GET', '/api/data?tab=aquifer&hours=24')
        self.assertEqual((s, set(d[-1])), (200, {'x', 'y'}))
        s, d = self.j('GET', '/api/data?tab=pump&start=2026-11-01T00:00&end=2030-01-01T00:00')
        self.assertEqual(s, 200)
        s, d = self.j('GET', '/api/device_status')
        self.assertEqual(s, 200)
        self.assertTrue({'connected', 'last_seen', 'tags'} <= set(d['1']))
        self.assertTrue(d['1']['connected'])
        self.assertEqual(d['1']['tags']['1']['value'], 490.0)
        for url in ('/api/devices', '/api/tags', '/api/tags?device_id=1'):
            s, d = self.j('GET', url)
            self.assertEqual(s, 200, url)
            self.assertIsInstance(d, list)

    def test_v2_get_endpoints(self):
        s, d = self.j('GET', '/api/overview')
        self.assertEqual(s, 200)
        pump = [t for dv in d['devices'] for t in dv['tags'] if t['escalating']]
        self.assertEqual(len(pump), 1)
        self.assertEqual(pump[0]['alarm_low'], 2000)
        s, d = self.j('GET', '/api/tag_history?tag_id=1&hours=24')
        self.assertEqual((s, d['source'], d['stats']['last']), (200, 'flow_log', 490.0))
        self.assertEqual(self.j('GET', '/api/tag_history?tag_id=999')[0], 404)
        self.assertEqual(self.j('GET', '/api/alerts')[0], 200)
        s, d = self.j('GET', '/healthz')
        self.assertTrue(d['ok'])

    def test_writes_require_password(self):
        for m, url, body in [('POST', '/api/threshold', {'threshold_gpm': 1500}),
                             ('POST', '/api/devices', {'name': 'x', 'type': 'modbus_tcp', 'host': 'h'}),
                             ('PUT', '/api/devices/1', {'name': 'x', 'type': 'modbus_tcp'}),
                             ('DELETE', '/api/devices/1', {}), ('POST', '/api/tags', {}),
                             ('PUT', '/api/tags/1', {}), ('DELETE', '/api/tags/1', {}),
                             ('POST', '/api/ack', {}), ('POST', '/api/tags/1/alarm', {'alarm_enabled': 1}),
                             ('POST', '/api/auth/check', {})]:
            self.assertEqual(self.j(m, url, dict(body, password='wrong'))[0], 401, url)
        self.assertEqual(self.j('POST', '/api/threshold', None)[0], 401)

    def test_threshold_roundtrip(self):
        self.assertEqual(self.j('POST', '/api/threshold', {'threshold_gpm': 'abc', 'password': PW})[0], 400)
        self.assertEqual(self.j('POST', '/api/threshold', {'threshold_gpm': 1750, 'password': PW}), (200, {'threshold_gpm': 1750}))
        with open(settings.CONFIG_PATH) as f:
            self.assertEqual(json.load(f)['threshold_gpm'], 1750)
        self.assertEqual(dbmod.read_threshold_config(), 1750)
        self.j('POST', '/api/threshold', {'threshold_gpm': 2000, 'password': PW})

    def test_device_and_tag_crud(self):
        s, dev = self.j('POST', '/api/devices', {'name': 'Test', 'type': 'modbus_tcp', 'host': '10.0.0.9', 'port': 502, 'password': PW})
        self.assertEqual(s, 201)
        s, tag = self.j('POST', '/api/tags', {'device_id': dev['id'], 'name': 'T', 'address': 'HR1', 'alarm_enabled': 1,
                                              'alarm_low': 5, 'password': PW})
        self.assertEqual(s, 201)
        s, t2 = self.j('PUT', '/api/tags/%d' % tag['id'], {'name': 'T2', 'address': 'HR2', 'password': PW})
        self.assertEqual((s, t2['name'], t2['address']), (200, 'T2', 'HR2'))
        s, t3 = self.j('POST', '/api/tags/%d/alarm' % tag['id'], {'alarm_enabled': True, 'password': PW})
        self.assertEqual((s, t3['alarm_enabled']), (200, 1))
        s, d2 = self.j('PUT', '/api/devices/%d' % dev['id'], {'name': 'Test2', 'type': 'modbus_tcp', 'host': 'h', 'password': PW})
        self.assertEqual((s, d2['name']), (200, 'Test2'))
        self.assertEqual(self.j('DELETE', '/api/devices/%d' % dev['id'], {'password': PW}), (200, {'ok': True}))
        self.assertEqual(self.j('GET', '/api/tags?device_id=%d' % dev['id']), (200, []))   # cascade

    def test_ack_touches_ack_file(self):
        if os.path.exists(settings.ACK_FILE):
            os.remove(settings.ACK_FILE)
        self.assertEqual(self.j('POST', '/api/ack', {'password': PW}), (200, {'ok': True}))
        self.assertTrue(os.path.exists(settings.ACK_FILE))
        os.remove(settings.ACK_FILE)

    def test_downsample_keeps_extremes(self):
        rows = [('t%05d' % i, 100.0) for i in range(10000)]
        rows[5000] = ('t05000', 3.0)
        out = web._downsample(rows, 1000)
        self.assertLessEqual(len(out), 1000)
        self.assertIn(('t05000', 3.0), out)

    def test_rate_limit(self):
        web._failures.clear()
        for _ in range(web.MAX_FAILS):
            self.j('POST', '/api/auth/check', {'password': 'nope'})
        self.assertEqual(self.j('POST', '/api/auth/check', {'password': PW})[0], 429)
        web._failures.clear()
        self.assertEqual(self.j('POST', '/api/auth/check', {'password': PW}), (200, {'ok': True}))


if __name__ == '__main__':
    unittest.main()
