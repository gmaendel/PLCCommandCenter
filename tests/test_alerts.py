"""Alerter (dry-run / live with fakes), dedup, iMessage failure fallback, chat.db ack parsing."""
import os
import sqlite3
import subprocess
import time
import unittest

from tests._env import FakeClock, TMP
from plccc import db as dbmod
from plccc.alerts import Alerter, AckChecker, build_applescript, APPLE_EPOCH_OFFSET


def setUpModule():
    conn = dbmod.connect()
    dbmod.migrate(conn)
    conn.close()


class Resp:
    def __init__(self, code=200):
        self.status_code, self.text = code, 'ok'


class AlerterTests(unittest.TestCase):
    def test_dry_run_never_touches_network_or_osascript(self):
        def boom(*a, **k):
            raise AssertionError('network/osascript must not be called in dry-run')
        a = Alerter(dry_run=True, http_post=boom, run_cmd=boom)
        a.start()
        self.assertTrue(a.send('hello dry', kind='alarm', source='test'))
        a.flush(2)
        conn = dbmod.connect()
        row = conn.execute("SELECT * FROM alert_log WHERE text='hello dry'").fetchone()
        conn.close()
        self.assertEqual((row['dry_run'], row['telegram'], row['imessage']), (1, 'dry-run', 'dry-run'))

    def test_dedup_window(self):
        clock = FakeClock()
        a = Alerter(dry_run=True, clock=clock)
        self.assertTrue(a.send('same'))
        self.assertFalse(a.send('same'))
        clock.advance(61)
        self.assertTrue(a.send('same'))

    def test_live_sends_both_channels(self):
        posts, cmds = [], []
        a = Alerter(dry_run=False, telegram_token='TOK', telegram_chat='42', imessage_chat='Recharge Pump Alarm',
                    http_post=lambda url, json, timeout: posts.append((url, json, timeout)) or Resp(200),
                    run_cmd=lambda args, **kw: cmds.append(args) or subprocess.CompletedProcess(args, 0, '', ''))
        a.start()
        a.send('pump alarm "quoted" \\ slash')
        a.flush(5)
        self.assertEqual(posts[0][0], 'https://api.telegram.org/botTOK/sendMessage')
        self.assertEqual(posts[0][1], {'chat_id': '42', 'text': 'pump alarm "quoted" \\ slash'})
        self.assertEqual(posts[0][2], 10)
        self.assertEqual(cmds[0][:2], ['osascript', '-e'])
        self.assertIn('send "pump alarm \\"quoted\\" \\\\ slash" to targetChat', cmds[0][2])
        self.assertIn('first chat whose name is "Recharge Pump Alarm"', cmds[0][2])

    def test_imessage_failure_reported_on_telegram(self):
        posts = []
        a = Alerter(dry_run=False, http_post=lambda url, json, timeout: posts.append(json['text']) or Resp(200),
                    run_cmd=lambda args, **kw: subprocess.CompletedProcess(args, 1, '', 'Not authorized (-1743)'))
        a.start()
        a.send('x1')
        a.flush(5)
        time.sleep(0.2)
        a.flush(5)
        self.assertIn('x1', posts)
        self.assertIn('\u26a0\ufe0f iMessage send FAILED!\nError: Not authorized (-1743)', posts)

    def test_imessage_timeout_reported(self):
        posts = []

        def hang(args, **kw):
            raise subprocess.TimeoutExpired(args, 25)
        a = Alerter(dry_run=False, http_post=lambda url, json, timeout: posts.append(json['text']) or Resp(200), run_cmd=hang)
        a.start()
        a.send('x2')
        a.flush(5)
        time.sleep(0.2)
        a.flush(5)
        self.assertTrue(any('TIMED OUT' in p for p in posts))

    def test_telegram_error_does_not_block_imessage(self):
        cmds = []
        a = Alerter(dry_run=False, http_post=lambda *a, **k: (_ for _ in ()).throw(IOError('down')),
                    run_cmd=lambda args, **kw: cmds.append(args) or subprocess.CompletedProcess(args, 0, '', ''))
        a.start()
        a.send('x3')
        a.flush(5)
        self.assertEqual(len(cmds), 1)

    def test_applescript_escaping(self):
        s = build_applescript('a"b\\c', 'Chat "x"')
        self.assertIn('send "a\\"b\\\\c"', s)
        self.assertIn('name is "Chat \\"x\\""', s)


class AckTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(TMP, 'chat_fixture.db')
        if os.path.exists(self.path):
            os.remove(self.path)
        c = sqlite3.connect(self.path)
        c.executescript('''
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, is_from_me INTEGER, date INTEGER, item_type INTEGER DEFAULT 0);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            INSERT INTO chat VALUES (1, 'Recharge Pump Alarm'), (2, 'Family');''')
        self.c = c
        self.now = time.time()

    def add(self, chat, text, from_me=0, at=None, item_type=0):
        at = self.now if at is None else at
        cur = self.c.execute('INSERT INTO message (text,is_from_me,date,item_type) VALUES (?,?,?,?)',
                             (text, from_me, int((at - APPLE_EPOCH_OFFSET) * 1e9), item_type))
        self.c.execute('INSERT INTO chat_message_join VALUES (?,?)', (chat, cur.lastrowid))
        self.c.commit()

    def checker(self):
        return AckChecker(ack_file=os.path.join(TMP, 'ackfile'), chat_db=self.path, chat_name='Recharge Pump Alarm')

    def test_ack_reply_after_alarm(self):
        self.add(1, 'ACK')
        self.assertTrue(self.checker().imessage_acked(since_timestamp=self.now - 10))

    def test_got_it(self):
        self.add(1, 'Got it, heading out')
        self.assertTrue(self.checker().imessage_acked(since_timestamp=self.now - 10))

    def test_any_reply_acks(self):
        self.add(1, 'on my way')
        self.assertTrue(self.checker().imessage_acked(since_timestamp=self.now - 10))

    def test_reply_without_plain_text_acks(self):
        # newer macOS can store a reply only in attributedBody (text NULL); tapbacks too
        self.add(1, None)
        self.assertTrue(self.checker().imessage_acked(since_timestamp=self.now - 10))

    def test_group_event_does_not_ack(self):
        # e.g. the item_type=3 row macOS adds when a group is created/renamed
        self.add(1, None, item_type=3)
        self.assertFalse(self.checker().imessage_acked(since_timestamp=self.now - 10))

    def test_keyword_mode_still_available(self):
        self.add(1, 'on my way')
        ch = AckChecker(ack_file=os.path.join(TMP, 'ackfile'), chat_db=self.path,
                        chat_name='Recharge Pump Alarm', keywords=('ack',))
        self.assertFalse(ch.imessage_acked(since_timestamp=self.now - 10))

    def test_old_ack_ignored(self):
        self.add(1, 'ack', at=self.now - 3600)
        self.assertFalse(self.checker().imessage_acked(since_timestamp=self.now - 10))

    def test_own_message_ignored(self):
        self.add(1, 'ack', from_me=1)
        self.assertFalse(self.checker().imessage_acked(since_timestamp=self.now - 10))

    def test_other_chat_ignored(self):
        self.add(2, 'ack')
        self.assertFalse(self.checker().imessage_acked(since_timestamp=self.now - 10))

    def test_missing_db(self):
        self.assertFalse(AckChecker(chat_db=os.path.join(TMP, 'nope.db')).imessage_acked(0))

    def test_ack_file_touch_clear(self):
        ch = self.checker()
        ch.clear()
        self.assertFalse(ch.file_acked())
        ch.touch()
        self.assertTrue(ch.file_acked())
        ch.clear()
        self.assertFalse(ch.file_acked())


if __name__ == '__main__':
    unittest.main()
