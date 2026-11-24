"""Customizable Overview layout (v2.1): default reproduces the classic Overview, API round-trips, validation."""
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
    web.init_db()
    conn = dbmod.connect()
    conn.execute("INSERT INTO flow_log (timestamp,gpm,level) VALUES (datetime('now','-30 seconds'),490,1)")
    conn.close()
    c = web.app.test_client()


class LayoutTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(web.layout_path()):
            os.remove(web.layout_path())

    tearDown = setUp

    def j(self, method, url, body=None):
        r = c.open(url, method=method, data=json.dumps(body) if body is not None else None,
                   content_type='application/json')
        return r.status_code, r.get_json(silent=True)

    def tag_ids(self):
        conn = dbmod.connect()
        rows = conn.execute('SELECT id,name FROM tags ORDER BY id').fetchall()
        conn.close()
        return {r['name']: r['id'] for r in rows}

    def test_default_matches_classic_overview(self):
        ids = self.tag_ids()
        s, d = self.j('GET', '/api/overview_layout')
        self.assertEqual(s, 200)
        self.assertFalse(d['customized'])
        # classic Overview: escalating pump tag as the chart card, then a tile per other enabled tag
        self.assertEqual(d['cards'], [{'type': 'chart', 'tag_id': ids['Pump GPM'], 'hours': 6},
                                      {'type': 'tile', 'tag_id': ids['Water Level']},
                                      {'type': 'tile', 'tag_id': ids['Aquifer Level']}])
        self.assertEqual(d['default'], d['cards'])
        s, ov = self.j('GET', '/api/overview')
        self.assertEqual(ov['layout'], {'customized': False, 'cards': d['cards']})

    def test_default_skips_disabled_tags(self):
        ids = self.tag_ids()
        conn = dbmod.connect()
        conn.execute('UPDATE tags SET enabled=0 WHERE id=?', (ids['Aquifer Level'],))
        conn.close()
        try:
            cards = self.j('GET', '/api/overview_layout')[1]['cards']
            self.assertNotIn(ids['Aquifer Level'], [x['tag_id'] for x in cards])
        finally:
            conn = dbmod.connect()
            conn.execute('UPDATE tags SET enabled=1 WHERE id=?', (ids['Aquifer Level'],))
            conn.close()

    def test_save_roundtrip_and_reset(self):
        ids = self.tag_ids()
        cards = [{'type': 'tile', 'tag_id': ids['Aquifer Level']},
                 {'type': 'chart', 'tag_id': ids['Water Level'], 'hours': 24},
                 {'type': 'chart', 'tag_id': ids['Pump GPM']}]
        s, d = self.j('PUT', '/api/overview_layout', {'cards': cards, 'password': PW})
        self.assertEqual(s, 200, d)
        self.assertTrue(d['customized'])
        cards[2]['hours'] = 6        # default range filled in
        self.assertEqual(d['cards'], cards)
        self.assertEqual(self.j('GET', '/api/overview_layout')[1]['cards'], cards)
        self.assertEqual(self.j('GET', '/api/overview')[1]['layout']['cards'], cards)
        with open(web.layout_path()) as f:
            self.assertEqual(json.load(f)['cards'], cards)
        s, d = self.j('DELETE', '/api/overview_layout', {'password': PW})
        self.assertEqual(s, 200)
        self.assertFalse(d['customized'])
        self.assertFalse(os.path.exists(web.layout_path()))
        self.assertEqual(d['cards'][0]['type'], 'chart')

    def test_empty_layout_is_allowed(self):
        s, d = self.j('PUT', '/api/overview_layout', {'cards': [], 'password': PW})
        self.assertEqual((s, d['customized'], d['cards']), (200, True, []))

    def test_requires_password(self):
        self.assertEqual(self.j('PUT', '/api/overview_layout', {'cards': []})[0], 401)
        self.assertEqual(self.j('PUT', '/api/overview_layout', {'cards': [], 'password': 'nope'})[0], 401)
        self.assertEqual(self.j('DELETE', '/api/overview_layout', {})[0], 401)
        self.assertFalse(os.path.exists(web.layout_path()))

    def test_validation(self):
        ids = self.tag_ids()
        bad = [None, 'x', [{'type': 'gauge', 'tag_id': ids['Pump GPM']}], [{'type': 'tile', 'tag_id': 99999}],
               [{'type': 'tile', 'tag_id': 'abc'}], [{'type': 'tile', 'tag_id': True}], ['tile'],
               [{'type': 'chart', 'tag_id': ids['Pump GPM'], 'hours': 7}],
               [{'type': 'tile', 'tag_id': ids['Pump GPM']}] * (web.MAX_CARDS + 1)]
        for cards in bad:
            s, d = self.j('PUT', '/api/overview_layout', {'cards': cards, 'password': PW})
            self.assertEqual(s, 400, cards)
            self.assertIn('error', d)
        self.assertFalse(os.path.exists(web.layout_path()))

    def test_corrupt_file_falls_back_to_default(self):
        with open(web.layout_path(), 'w') as f:
            f.write('{not json')
        s, d = self.j('GET', '/api/overview_layout')
        self.assertEqual((s, d['customized']), (200, False))
        self.assertEqual(len(d['cards']), 3)

    def test_deleted_tag_card_is_kept_but_harmless(self):
        ids = self.tag_ids()
        with open(web.layout_path(), 'w') as f:
            json.dump({'cards': [{'type': 'tile', 'tag_id': 4242}, {'type': 'tile', 'tag_id': ids['Pump GPM']}]}, f)
        s, ov = self.j('GET', '/api/overview')
        self.assertEqual(s, 200)
        self.assertEqual([x['tag_id'] for x in ov['layout']['cards']], [4242, ids['Pump GPM']])

    def test_version(self):
        from plccc import __version__
        self.assertGreaterEqual(tuple(map(int, __version__.split('.'))), (2, 1, 0))
        self.assertEqual(self.j('GET', '/healthz')[1]['web'], __version__)

    def test_page_has_layout_hooks(self):
        html = c.get('/').data
        for hook in (b'id="ovCards"', b'id="ovCharts"', b'id="ovEditTop"', b'id="ovEditBottom"'):
            self.assertIn(hook, html)
        js = c.get('/static/app.js').data
        self.assertIn(b'/api/overview_layout', js)


if __name__ == '__main__':
    unittest.main()
