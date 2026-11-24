"""Single alerting path for every device: Telegram + iMessage group chat.

* PLCCC_DRY_RUN=1: nothing is sent; every alert is logged and stored in alert_log (dry_run=1).
* Telegram and iMessage each have their own worker thread + queue, so a slow or hung
  Messages.app never delays Telegram or PLC polling, and alerts stay in order per channel.
* iMessage uses the v1 pump_monitor AppleScript (errors propagate). A failed / timed-out
  iMessage is reported via Telegram, exactly like v1 pump_monitor (v1 modbus/siemens/ab
  monitors silently ignored iMessage failures; v2 reports them for every device).
* Identical alert text within DEDUP_WINDOW_S is dropped (guards against double sends).
"""
import datetime
import logging
import os
import queue
import subprocess
import threading
import time

import plccc_settings as settings
from . import db as dbmod
from . import telegram as tgmod

log = logging.getLogger('plccc.alerts')

DEDUP_WINDOW_S = 60
TELEGRAM_TIMEOUT_S = 10
IMESSAGE_TIMEOUT_S = 25
APPLE_EPOCH_OFFSET = 978307200


def _now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def build_applescript(text, chat_name):
    safe_text = text.replace('\\', '\\\\').replace('"', '\\"')
    safe_chat = chat_name.replace('\\', '\\\\').replace('"', '\\"')
    return f'''
    tell application "Messages"
        try
            set targetChat to first chat whose name is "{safe_chat}"
            send "{safe_text}" to targetChat
        on error errMsg number errNum
            error errMsg number errNum
        end try
    end tell
    '''


class Alerter:
    def __init__(self, dry_run=None, telegram_token=None, telegram_chat=None, imessage_chat=None,
                 db_path=None, clock=time.time, http_post=None, run_cmd=None):
        self.dry_run = settings.DRY_RUN if dry_run is None else dry_run
        # Explicit token/chat (tests) are fixed; otherwise the Telegram config is read live per send
        # from the Settings page (app_settings), falling back to .env - see plccc.telegram.load_config.
        self._token = telegram_token
        self._chat_id = telegram_chat
        self._imessage_chat = imessage_chat      # None -> Settings page value (db), else env default
        self.db_path = db_path
        self.clock = clock
        self._http_post = http_post
        self._run_cmd = run_cmd or subprocess.run
        self._recent = {}
        self._lock = threading.Lock()
        self._tq = queue.Queue()
        self._iq = queue.Queue()
        self._threads = []
        self.sent = []           # (channel, text) — used by tests / dry-run introspection
        self._stopping = False

    @property
    def imessage_chat(self):
        if self._imessage_chat:
            return self._imessage_chat
        try:
            return dbmod.imessage_chat(self.db_path)
        except Exception:
            return settings.IMESSAGE_CHAT

    def telegram_config(self):
        """-> (enabled, token, [chat ids]) for this send."""
        if self._token is not None or self._chat_id is not None:
            tok = settings.TELEGRAM_BOT_TOKEN if self._token is None else self._token
            chat = settings.TELEGRAM_CHAT_ID if self._chat_id is None else self._chat_id
            return True, tok, [chat]
        try:
            cfg = tgmod.load_config(db_path=self.db_path)
            return cfg['enabled'], cfg['token'], [c['id'] for c in cfg['chats']]
        except Exception as e:
            log.error('Telegram config read failed, using .env: %s', e)
            return True, settings.TELEGRAM_BOT_TOKEN, [settings.TELEGRAM_CHAT_ID]

    @property
    def token(self):
        return self.telegram_config()[1]

    @property
    def chat_id(self):
        chats = self.telegram_config()[2]
        return chats[0] if chats else ''

    # ── public API ───────────────────────────────────────────────────────────
    def start(self):
        self._stopping = False
        self._ensure_threads()

    def _ensure_threads(self):
        alive = [t for t in self._threads if t.is_alive()]
        names = {t.name for t in alive}
        for name, target in (('alert-telegram', self._telegram_worker), ('alert-imessage', self._imessage_worker)):
            if name not in names:
                t = threading.Thread(target=target, name=name, daemon=True)
                t.start()
                alive.append(t)
        self._threads = alive

    def healthy(self):
        return len([t for t in self._threads if t.is_alive()]) == 2

    def send(self, text, kind='alarm', source=''):
        """Queue an alert for Telegram + iMessage. Returns False if de-duplicated."""
        now = self.clock()
        with self._lock:
            for k, ts in list(self._recent.items()):
                if now - ts > DEDUP_WINDOW_S:
                    del self._recent[k]
            if text in self._recent:
                log.warning('Duplicate alert suppressed (%ss window): %s', DEDUP_WINDOW_S, text[:80])
                return False
            self._recent[text] = now
        row_id = self._record(kind, source, text)
        if self.dry_run:
            log.warning('[DRY-RUN] would send %s alert (Telegram + iMessage "%s"):\n%s',
                        kind, self.imessage_chat, text)
            self.sent.append(('dry-run', text))
            self._update(row_id, 'telegram', 'dry-run')
            self._update(row_id, 'imessage', 'dry-run')
            return True
        self._tq.put((text, row_id))
        self._iq.put((text, row_id))
        return True

    def flush(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end and (self._tq.unfinished_tasks or self._iq.unfinished_tasks):
            time.sleep(0.1)

    # ── persistence ──────────────────────────────────────────────────────────
    def _record(self, kind, source, text):
        try:
            conn = dbmod.connect(self.db_path)
            try:
                cur = conn.execute('INSERT INTO alert_log (kind, source, text, dry_run) VALUES (?,?,?,?)',
                                   (kind, source, text, 1 if self.dry_run else 0))
                conn.execute("DELETE FROM alert_log WHERE timestamp < datetime('now','-60 days')")
                return cur.lastrowid
            finally:
                conn.close()
        except Exception as e:
            log.error('alert_log insert failed: %s', e)
            return None

    def _update(self, row_id, col, status):
        if row_id is None or col not in ('telegram', 'imessage'):
            return
        try:
            conn = dbmod.connect(self.db_path)
            try:
                conn.execute('UPDATE alert_log SET %s=? WHERE id=?' % col, (status[:300], row_id))
            finally:
                conn.close()
        except Exception as e:
            log.error('alert_log update failed: %s', e)

    # ── channels ─────────────────────────────────────────────────────────────
    def send_telegram_now(self, text):
        if self.dry_run:
            log.warning('[DRY-RUN] would send Telegram: %s', text)
            self.sent.append(('telegram-dry', text))
            return 'dry-run'
        enabled, token, chats = self.telegram_config()
        if not enabled:
            log.info('Telegram alerts are switched off in Settings; not sent: %s', text[:80])
            return 'disabled'
        if not token or not chats:
            log.error('Telegram not configured (token or chat id missing); not sent: %s', text[:80])
            return 'not configured'
        results = []
        for chat in chats:
            st = tgmod.send_message(token, chat, text, http_post=self._http_post)
            if st != 'ok':
                log.error('Telegram send to %s failed: %s', chat, st)
            results.append((chat, st))
        if all(st == 'ok' for _, st in results):
            log.info('Sent Telegram Alert: %s', text)
            self.sent.append(('telegram', text))
            return 'ok'
        if len(results) == 1:
            return results[0][1]
        return '; '.join('%s: %s' % r for r in results)

    def send_imessage_to(self, text, chat_name):
        """One AppleScript send to the named group. Returns (status, error|None); never notifies."""
        if self.dry_run:
            log.warning('[DRY-RUN] would send iMessage to "%s": %s', chat_name, text)
            return 'dry-run', None
        script = build_applescript(text, chat_name)
        try:
            result = self._run_cmd(['osascript', '-e', script], capture_output=True, text=True,
                                   timeout=IMESSAGE_TIMEOUT_S)
            if result.returncode != 0:
                err = (result.stderr or '').strip() or (result.stdout or '').strip() or 'unknown error'
                return 'failed', err
            return 'ok', None
        except subprocess.TimeoutExpired:
            return 'timeout', 'timed out (>%ss) - Messages.app may be hung' % IMESSAGE_TIMEOUT_S
        except Exception as e:
            return 'exception', str(e)

    def send_imessage_now(self, text):
        if self.dry_run:
            log.warning('[DRY-RUN] would send iMessage: %s', text)
            return 'dry-run'
        chat = self.imessage_chat
        status, err = self.send_imessage_to(text, chat)
        if status == 'ok':
            log.info('iMessage Group Alert sent successfully.')
            self.sent.append(('imessage', text))
            return 'ok'
        if status == 'failed':
            log.error('iMessage FAILED (chat "%s"): %s', chat, err)
            self._telegram_direct('\u26a0\ufe0f iMessage send FAILED!\nError: %s' % err)
            return 'failed: %s' % err
        if status == 'timeout':
            log.error('iMessage send TIMED OUT (>25s) - Messages.app may be hung.')
            self._telegram_direct('\u26a0\ufe0f iMessage send TIMED OUT (>25s) \u2014 Messages.app may be hung.')
            return 'timeout'
        log.error('iMessage exception: %s', err)
        self._telegram_direct('\u26a0\ufe0f iMessage send exception: %s' % err)
        return 'exception: %s' % err

    def _telegram_direct(self, text):
        # failure notices skip dedup and go straight to the Telegram queue
        self._tq.put((text, None))

    def _telegram_worker(self):
        while True:
            text, row_id = self._tq.get()
            try:
                self._update(row_id, 'telegram', self.send_telegram_now(text))
            except Exception as e:
                log.error('telegram worker error: %s', e)
            finally:
                self._tq.task_done()

    def _imessage_worker(self):
        while True:
            text, row_id = self._iq.get()
            try:
                self._update(row_id, 'imessage', self.send_imessage_now(text))
            except Exception as e:
                log.error('imessage worker error: %s', e)
            finally:
                self._iq.task_done()


class AckChecker:
    """Acknowledgement sources (v1 pump_monitor semantics):
       1. the ack file exists (written by the dashboard 'Acknowledge' button or any helper), or
       2. a message in the iMessage group (not from me) newer than the alarm start whose
          lower-cased text CONTAINS one of the keywords; with no keywords (the default)
          ANY reply in the group after the alarm started counts as an ack.
    """

    def __init__(self, ack_file=None, chat_db=None, chat_name=None, keywords=(), run_cmd=None):
        self.ack_file = ack_file or settings.ACK_FILE
        self.chat_db = chat_db or settings.CHAT_DB_PATH
        # str, or a callable returning the current Settings-page chat name (re-read on every check)
        self._chat_name = chat_name or settings.IMESSAGE_CHAT
        self.keywords = tuple(k.lower() for k in keywords)
        self._run_cmd = run_cmd or subprocess.run

    @property
    def chat_name(self):
        c = self._chat_name
        if callable(c):
            try:
                return c() or settings.IMESSAGE_CHAT
            except Exception:
                return settings.IMESSAGE_CHAT
        return c

    def file_acked(self):
        return os.path.exists(self.ack_file)

    def touch(self):
        try:
            open(self.ack_file, 'a').close()
        except Exception as e:
            log.error('Could not write ack file %s: %s', self.ack_file, e)

    def clear(self):
        try:
            if os.path.exists(self.ack_file):
                os.remove(self.ack_file)
        except Exception as e:
            log.error('Could not remove ack file %s: %s', self.ack_file, e)

    def imessage_acked(self, since_timestamp=None):
        if not os.path.exists(self.chat_db):
            return False
        try:
            time_filter = ''
            if since_timestamp is not None:
                apple_ns = int((since_timestamp - APPLE_EPOCH_OFFSET) * 1e9)
                time_filter = 'AND message.date > %d' % apple_ns
            chat = self.chat_name.replace("'", "''")
            query = f"""
            SELECT COALESCE(message.text, '(reply)')
            FROM message
            JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
            JOIN chat ON chat_message_join.chat_id = chat.ROWID
            WHERE LOWER(chat.display_name) = LOWER('{chat}')
            AND message.is_from_me = 0
            AND COALESCE(message.item_type, 0) = 0   -- skip group events (renames, members joining)
            {time_filter}
            ORDER BY message.date DESC LIMIT 10;
            """
            try:
                res = self._run_cmd(['sqlite3', '-readonly', self.chat_db, query],
                                    capture_output=True, text=True, timeout=5)
                ok, out, errtxt = res.returncode == 0, res.stdout or '', res.stderr or ''
            except FileNotFoundError:       # no sqlite3 CLI: same query in-process, read-only
                import sqlite3
                uri = 'file:%s?mode=ro' % self.chat_db
                c = sqlite3.connect(uri, uri=True, timeout=5)
                try:
                    out = '\n'.join((r[0] or '').replace('\n', ' ') for r in c.execute(query))
                    ok, errtxt = True, ''
                finally:
                    c.close()
            if ok and out:
                for line in out.strip().split('\n'):
                    t = line.lower().strip()
                    if t and (not self.keywords or any(k in t for k in self.keywords)):
                        return True
            elif not ok:
                log.warning('chat.db query failed (Full Disk Access?): %s', errtxt.strip()[:200])
        except Exception as e:
            log.error('Exception checking chat.db for ack: %s', e)
        return False


def find_chat(chat_db, chat_name):
    """Read-only look-up of group chats named chat_name (case-insensitive, same match as the ack
    reader). Needs Full Disk Access, so it runs in the poller. Returns a JSON-able dict."""
    import sqlite3
    out = {'chat_name': chat_name, 'found': False, 'matches': 0, 'participants': None,
           'last_message_at': None, 'error': None}
    if not os.path.exists(chat_db):
        out['error'] = 'Messages database not found (%s)' % chat_db
        return out
    try:
        c = sqlite3.connect('file:%s?mode=ro' % chat_db, uri=True, timeout=5)
        try:
            rows = c.execute('SELECT ROWID FROM chat WHERE LOWER(display_name) = LOWER(?)', (chat_name,)).fetchall()
            ids = [r[0] for r in rows]
            out['matches'] = len(ids)
            out['found'] = bool(ids)
            if ids:
                q = ','.join('?' * len(ids))
                r = c.execute('SELECT MAX(m.date) FROM message m JOIN chat_message_join j ON j.message_id=m.ROWID '
                              'WHERE j.chat_id IN (%s)' % q, ids).fetchone()
                if r and r[0]:
                    d = r[0] / 1e9 if r[0] > 1e12 else r[0]
                    out['last_message_at'] = datetime.datetime.utcfromtimestamp(d + APPLE_EPOCH_OFFSET).strftime(
                        '%Y-%m-%d %H:%M:%S')
                try:
                    r = c.execute('SELECT COUNT(DISTINCT handle_id) FROM chat_handle_join WHERE chat_id IN (%s)' % q,
                                  ids).fetchone()
                    out['participants'] = r[0] if r else None
                except sqlite3.Error:
                    pass
        finally:
            c.close()
    except Exception as e:
        out['error'] = 'Cannot read Messages database (Full Disk Access?): %s' % e
    return out
