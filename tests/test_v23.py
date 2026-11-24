"""v2.3: Telegram settings card (live config, multi-chat, find my chat, test), fresh-install defaults."""
import json
import os
import sqlite3
import unittest
from unittest import mock

from tests._env import TMP
import plccc_settings as settings
from plccc import db as dbmod
from plccc import telegram as tg
from plccc.alerts import Alerter

PW = 'test-pw'
TOKEN = '123456789:AAHfakefakefakefakefakefakefake1234'
TOKEN2 = '987654321:BBHfakefakefakefakefakefakefake9876'


class Resp:
    def __init__(self, code=200, data=None):
        self.status_code, self._data, self.text = code, data, json.dumps(data)

    def json(self):
        if self._data is None:
            raise ValueError('no json')
        return self._data


class FakeTelegram:
    """Records calls; answers per method."""
    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, url, json=None, timeout=None):
        method = url.rsplit('/', 1)[1]
        self.calls.append((url, method, json, timeout))
        a = self.answers.get(method, (200, {'ok': True, 'result': True}))
        if isinstance(a, Exception):
            raise a
        return Resp(*a)


def setUpModule():
    global web, c
    if os.path.exists(settings.DB_PATH):
        os.remove(settings.DB_PATH)
    with open(settings.CONFIG_PATH, 'w') as f:
        f.write('{"threshold_gpm": 2000}')
    from plccc import web as w
    web = w
    web.init_db()
    c = web.app.test_client()


def clear():
    conn = dbmod.connect()
    conn.execute("DELETE FROM app_settings WHERE key LIKE 'telegram_%'")
    conn.close()
    web._tg_last.clear()
    web.tg_http_post = None


class ConfigTests(unittest.TestCase):
    def setUp(self):
        clear()

    def test_env_fallback_is_the_old_behaviour(self):
        cfg = tg.load_config()
        self.assertEqual((cfg['token'], cfg['token_source'], cfg['chats'], cfg['chats_source'], cfg['enabled']),
                         ('TEST', 'env', [{'id': '0', 'title': ''}], 'env', True))

    def test_env_fallback_send_payload_identical(self):
        fake = FakeTelegram()
        a = Alerter(dry_run=False, http_post=fake)
        self.assertEqual(a.send_telegram_now('hi'), 'ok')
        self.assertEqual(fake.calls, [('https://api.telegram.org/botTEST/sendMessage', 'sendMessage',
                                       {'chat_id': '0', 'text': 'hi'}, 10)])

    def test_comma_separated_env_chats(self):
        with mock.patch.object(settings, 'TELEGRAM_CHAT_ID', '-1001, 42'):
            self.assertEqual([x['id'] for x in tg.load_config()['chats']], ['-1001', '42'])

    def test_live_config_multi_chat_and_disable(self):
        fake = FakeTelegram()
        a = Alerter(dry_run=False, http_post=fake)
        dbmod.set_setting('telegram_token', TOKEN)
        dbmod.set_setting('telegram_chats', json.dumps([{'id': '-100111', 'title': 'Farm'}, {'id': '55', 'title': 'Me'}]))
        self.assertEqual(a.send_telegram_now('alarm'), 'ok')
        self.assertEqual([(u, j['chat_id']) for u, m, j, t in fake.calls],
                         [('https://api.telegram.org/bot%s/sendMessage' % TOKEN, '-100111'),
                          ('https://api.telegram.org/bot%s/sendMessage' % TOKEN, '55')])
        dbmod.set_setting('telegram_enabled', '0')        # no restart needed
        fake.calls.clear()
        self.assertEqual(a.send_telegram_now('alarm2'), 'disabled')
        self.assertEqual(fake.calls, [])

    def test_partial_failure_status(self):
        dbmod.set_setting('telegram_token', TOKEN)
        dbmod.set_setting('telegram_chats', json.dumps([{'id': '1'}, {'id': '2'}]))
        calls = []

        def post(url, json, timeout):
            calls.append(json['chat_id'])
            return Resp(200 if json['chat_id'] == '1' else 403, {'ok': False})
        self.assertEqual(Alerter(dry_run=False, http_post=post).send_telegram_now('x'), '1: ok; 2: error 403')

    def test_empty_chat_list_means_not_configured(self):
        dbmod.set_setting('telegram_chats', '[]')
        fake = FakeTelegram()
        self.assertEqual(Alerter(dry_run=False, http_post=fake).send_telegram_now('x'), 'not configured')
        self.assertEqual(fake.calls, [])

    def test_explicit_constructor_values_still_static(self):
        dbmod.set_setting('telegram_token', TOKEN)
        fake = FakeTelegram()
        Alerter(dry_run=False, telegram_token='TOK', telegram_chat='42', http_post=fake).send_telegram_now('x')
        self.assertEqual(fake.calls[0][0], 'https://api.telegram.org/botTOK/sendMessage')

    def test_mask_and_redact(self):
        m = tg.mask_token(TOKEN)
        self.assertNotIn(TOKEN, m)
        self.assertTrue(m.startswith('123456789:AA') and m.endswith('1234'))
        err = IOError('Max retries exceeded with url: /bot%s/getMe' % TOKEN)
        with self.assertRaises(tg.TelegramError) as cm:
            tg.call(TOKEN, 'getMe', http_post=FakeTelegram({'getMe': err}))
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN, tg.send_message(TOKEN, '1', 'x', http_post=FakeTelegram({'sendMessage': err})))

    def test_validators(self):
        self.assertTrue(tg.valid_token(TOKEN))
        for bad in ('', 'abc', '123:short', TOKEN + ' x'):
            self.assertFalse(tg.valid_token(bad), bad)
        self.assertEqual(tg.normalize_chat_id(' -1001234 '), '-1001234')
        self.assertEqual(tg.normalize_chat_id('@farm_alarms'), '@farm_alarms')
        for bad in ('', 'abc', '12a', '-'):
            with self.assertRaises(ValueError):
                tg.normalize_chat_id(bad)


class ApiTests(unittest.TestCase):
    def setUp(self):
        clear()

    def j(self, method, url, body=None):
        r = c.open(url, method=method, data=json.dumps(body) if body is not None else None,
                   content_type='application/json')
        return r.status_code, r.get_json(silent=True), r.get_data(as_text=True)

    def test_get_never_returns_full_token(self):
        dbmod.set_setting('telegram_token', TOKEN)
        s, d, raw = self.j('GET', '/api/settings/telegram')
        self.assertEqual(s, 200)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn(TOKEN[12:], raw)
        self.assertEqual((d['token_set'], d['token_source']), (True, 'settings'))

    def test_post_requires_password(self):
        s, d, _ = self.j('POST', '/api/settings/telegram', {'token': TOKEN})
        self.assertEqual(s, 401)
        s, d, _ = self.j('POST', '/api/settings/telegram', {'token': TOKEN, 'password': 'nope'})
        self.assertEqual(s, 401)
        self.assertIsNone(dbmod.get_setting('telegram_token'))
        for url in ('/api/settings/telegram/find', '/api/settings/telegram/test'):
            self.assertEqual(self.j('POST', url, {})[0], 401)

    def test_save_blank_keeps_token_and_clear_reverts(self):
        s, d, raw = self.j('POST', '/api/settings/telegram', {'password': PW, 'token': TOKEN})
        self.assertEqual((s, d['token_source']), (200, 'settings'))
        self.assertNotIn(TOKEN, raw)
        s, d, _ = self.j('POST', '/api/settings/telegram', {'password': PW, 'token': '', 'enabled': False})
        self.assertEqual((s, dbmod.get_setting('telegram_token'), d['enabled']), (200, TOKEN, False))
        s, d, _ = self.j('POST', '/api/settings/telegram', {'password': PW, 'clear_token': True, 'enabled': True})
        self.assertEqual((d['token_source'], d['enabled']), ('env', True))

    def test_bad_token_and_chats_rejected(self):
        self.assertEqual(self.j('POST', '/api/settings/telegram', {'password': PW, 'token': 'nope'})[0], 400)
        self.assertEqual(self.j('POST', '/api/settings/telegram', {'password': PW, 'chats': [{'id': 'abc'}]})[0], 400)
        self.assertEqual(self.j('POST', '/api/settings/telegram', {'password': PW, 'chats': 'x'})[0], 400)
        self.assertIsNone(dbmod.get_setting('telegram_chats'))

    def test_chats_add_remove_dedupe(self):
        s, d, _ = self.j('POST', '/api/settings/telegram', {'password': PW, 'chats': [
            {'id': '-100222', 'title': 'Farm\nAlarms'}, {'id': '-100222', 'title': 'dup'}, '77']})
        self.assertEqual(d['chats'], [{'id': '-100222', 'title': 'FarmAlarms'}, {'id': '77', 'title': ''}])
        self.assertEqual(d['chats_source'], 'settings')
        s, d, _ = self.j('POST', '/api/settings/telegram', {'password': PW, 'chats': []})
        self.assertEqual(d['chats'], [])

    def test_find_lists_recent_chats(self):
        fake = FakeTelegram({
            'getMe': (200, {'ok': True, 'result': {'username': 'farm_bot', 'first_name': 'Farm'}}),
            'getUpdates': (200, {'ok': True, 'result': [
                {'update_id': 1, 'message': {'chat': {'id': 55, 'type': 'private', 'first_name': 'Ann', 'username': 'ann'}}},
                {'update_id': 2, 'my_chat_member': {'chat': {'id': -100333, 'type': 'supergroup', 'title': 'Pump Alarms'}}},
                {'update_id': 3, 'message': {'chat': {'id': -100333, 'type': 'supergroup', 'title': 'Pump Alarms'}}}]})})
        web.tg_http_post = fake
        s, d, raw = self.j('POST', '/api/settings/telegram/find', {'password': PW, 'token': TOKEN})
        self.assertEqual(s, 200)
        self.assertEqual(d['bot']['username'], 'farm_bot')
        self.assertEqual(d['chats'], [{'id': '-100333', 'type': 'supergroup', 'title': 'Pump Alarms'},
                                      {'id': '55', 'type': 'private', 'title': 'Ann (@ann)'}])
        self.assertIsNone(d['message'])
        self.assertNotIn(TOKEN, raw)
        upd = [call for call in fake.calls if call[1] == 'getUpdates'][0]
        self.assertNotIn('offset', upd[2])          # never consumes updates
        self.assertIn(TOKEN, upd[0])                # typed (unsaved) token is used

    def test_find_no_updates_webhook_and_bad_token(self):
        me = (200, {'ok': True, 'result': {'username': 'farm_bot'}})
        web.tg_http_post = FakeTelegram({'getMe': me, 'getUpdates': (200, {'ok': True, 'result': []})})
        s, d, _ = self.j('POST', '/api/settings/telegram/find', {'password': PW, 'token': TOKEN})
        self.assertEqual((s, d['chats']), (200, []))
        self.assertIn('@farm_bot', d['message'])
        web._tg_last.clear()
        web.tg_http_post = FakeTelegram({'getMe': me, 'getUpdates': (409, {'ok': False, 'error_code': 409,
                                         'description': "Conflict: can't use getUpdates method while webhook is active"})})
        s, d, _ = self.j('POST', '/api/settings/telegram/find', {'password': PW, 'token': TOKEN})
        self.assertEqual((s, d['webhook']), (200, True))
        self.assertIn('webhook', d['message'])
        web._tg_last.clear()
        web.tg_http_post = FakeTelegram({'getMe': (401, {'ok': False, 'error_code': 401, 'description': 'Unauthorized'})})
        s, d, _ = self.j('POST', '/api/settings/telegram/find', {'password': PW, 'token': TOKEN})
        self.assertEqual(s, 400)
        self.assertIn('rejected this bot token', d['error'])

    def test_find_uses_saved_then_env_token(self):
        fake = FakeTelegram({'getMe': (200, {'ok': True, 'result': {}}), 'getUpdates': (200, {'ok': True, 'result': []})})
        web.tg_http_post = fake
        self.j('POST', '/api/settings/telegram/find', {'password': PW})
        self.assertIn('/botTEST/', fake.calls[0][0])

    def test_send_test_rate_limited(self):
        dbmod.set_setting('telegram_token', TOKEN)
        dbmod.set_setting('telegram_chats', json.dumps([{'id': '-1', 'title': 'A'}, {'id': '-2', 'title': 'B'}]))
        dbmod.set_setting('telegram_enabled', '0')     # test works while alerts are switched off
        fake = FakeTelegram({'sendMessage': (200, {'ok': True, 'result': {}})})
        web.tg_http_post = fake
        s, d, _ = self.j('POST', '/api/settings/telegram/test', {'password': PW})
        self.assertEqual((s, d['ok'], len(fake.calls)), (200, True, 2))
        self.assertEqual(fake.calls[0][2], {'chat_id': '-1', 'text': tg.TEST_MESSAGE})
        s, d, _ = self.j('POST', '/api/settings/telegram/test', {'password': PW})
        self.assertEqual(s, 429)
        self.assertEqual(len(fake.calls), 2)

    def test_send_test_reports_failures(self):
        dbmod.set_setting('telegram_token', TOKEN)
        dbmod.set_setting('telegram_chats', json.dumps([{'id': '-1', 'title': 'A'}]))
        web.tg_http_post = FakeTelegram({'sendMessage': (400, {'ok': False, 'error_code': 400, 'description': 'Bad Request: chat not found'})})
        s, d, _ = self.j('POST', '/api/settings/telegram/test', {'password': PW})
        self.assertEqual((s, d['ok']), (200, False))
        self.assertIn('Chat not found', d['results'][0]['error'])

    def test_send_test_needs_chat(self):
        dbmod.set_setting('telegram_chats', '[]')
        s, d, _ = self.j('POST', '/api/settings/telegram/test', {'password': PW})
        self.assertEqual(s, 400)


class FreshInstallTests(unittest.TestCase):
    def _db(self, name, history=False):
        path = os.path.join(TMP, name)
        if os.path.exists(path):
            os.remove(path)
        conn = dbmod.connect(path)
        dbmod.migrate(conn)
        if history:
            conn.execute("INSERT INTO flow_log (timestamp, gpm, level) VALUES ('2026-01-01 00:00:00', 1, 1)")
        return conn

    def test_fresh_db_is_not_seeded_with_v1_devices(self):
        conn = self._db('fresh.db')
        with mock.patch.object(settings, 'SEED_V1_DEVICES', False):
            self.assertFalse(web._is_v1_database(conn))
        conn.close()

    def test_v1_database_or_flag_is_seeded(self):
        conn = self._db('v1.db', history=True)
        with mock.patch.object(settings, 'SEED_V1_DEVICES', False):
            self.assertTrue(web._is_v1_database(conn))
        conn.close()
        conn = self._db('flag.db')
        with mock.patch.object(settings, 'SEED_V1_DEVICES', True):
            self.assertTrue(web._is_v1_database(conn))
        conn.close()

    def test_placeholders_count_as_unset(self):
        with mock.patch.dict(os.environ, {'X_TOK': 'replace-with-bot-token', 'X_OK': ' real '}):
            self.assertEqual(settings._secret('X_TOK'), '')
            self.assertEqual(settings._secret('X_OK'), 'real')

    def test_env_example_ships_no_secrets(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        vals = {}
        for ln in open(os.path.join(root, '.env.example'), encoding='utf-8'):
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1)
                vals[k] = v
        self.assertEqual(vals['TELEGRAM_BOT_TOKEN'], '')
        self.assertEqual(vals['TELEGRAM_CHAT_ID'], '')
        self.assertIn('replace-with', vals['DASHBOARD_PASSWORD'])
        ignored = open(os.path.join(root, '.gitignore'), encoding='utf-8').read().split()
        for pat in ('.env', '*.db', '*.db-wal', 'data/*', 'logs/*', 'backups/'):
            self.assertIn(pat, ignored)


class VersionTest(unittest.TestCase):
    def test_version(self):
        from plccc import __version__
        self.assertEqual(__version__, '2.3.0')


class FullDiskAccessCheckTests(unittest.TestCase):
    def test_missing_ok_denied(self):
        from plccc import poller
        self.assertEqual(poller.check_chat_db_access(os.path.join(TMP, 'nope.db')), 'missing')
        good = os.path.join(TMP, 'fake_chat.db')
        if os.path.exists(good):
            os.remove(good)
        c = sqlite3.connect(good)
        c.execute('CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT)')
        c.commit()
        c.close()
        self.assertEqual(poller.check_chat_db_access(good), 'ok')
        bad = os.path.join(TMP, 'not_a_db.db')
        with open(bad, 'wb') as f:
            f.write(b'this is not sqlite' * 100)
        self.assertTrue(poller.check_chat_db_access(bad).startswith('denied: '))

    def test_status_recorded_for_installer(self):
        from plccc import poller
        conn = dbmod.connect()
        try:
            self.assertEqual(poller.record_chat_db_access(conn, os.path.join(TMP, 'nope.db')), 'missing')
            self.assertEqual(json.loads(dbmod.get_setting('fda_status', conn=conn))['status'], 'missing')
        finally:
            conn.close()
