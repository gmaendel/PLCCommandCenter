"""Telegram Bot API helpers + the live Telegram config (v2.3).

Config lives in app_settings (set from the Settings page) and falls back to .env, so an
install that never touched the Settings card keeps using TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID:
  telegram_token    bot token from @BotFather            (fallback: TELEGRAM_BOT_TOKEN)
  telegram_chats    JSON list [{"id": "-100…", "title": "Farm alarms"}]   (fallback: TELEGRAM_CHAT_ID,
                    comma-separated ids allowed)
  telegram_enabled  "1"/"0" (default on)
Alerts only go OUT through sendMessage; nothing here reads replies (acks stay iMessage-only).
'Find my chat' calls getUpdates WITHOUT an offset, so it never consumes/acknowledges updates.
"""
import json
import re

import plccc_settings as settings
from . import db as dbmod

API = 'https://api.telegram.org/bot%s/%s'
TIMEOUT_S = 10
TOKEN_RE = re.compile(r'^\d{5,15}:[A-Za-z0-9_-]{30,64}$')
CHAT_RE = re.compile(r'^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$')
MAX_CHATS = 20
TEST_MESSAGE = '\u2705 PLC Command Center test message. Telegram alerts are working.'


class TelegramError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def redact(text, token):
    """Never let a token leak into an error shown in the browser or the logs."""
    if token:
        text = text.replace(token, mask_token(token))
    return text


def valid_token(token):
    return bool(token) and bool(TOKEN_RE.match(token.strip()))


def mask_token(token):
    if not token:
        return ''
    t = token.strip()
    bot_id, _, secret = t.partition(':')
    if not secret:
        return '•' * 8
    return '%s:%s…%s' % (bot_id, secret[:2], secret[-4:]) if len(secret) > 8 else bot_id + ':••••'


def normalize_chat_id(v):
    s = str(v if v is not None else '').strip()
    if not CHAT_RE.match(s):
        raise ValueError('Chat ID must be a number like -1001234567890 (or @channelname)')
    return s


def _env_chats():
    out = []
    for part in (settings.TELEGRAM_CHAT_ID or '').split(','):
        p = part.strip()
        if p:
            out.append({'id': p, 'title': ''})
    return out


def load_config(conn=None, db_path=None):
    """Effective config: {'token', 'token_source', 'chats', 'chats_source', 'enabled'}."""
    tok = dbmod.get_setting('telegram_token', None, conn=conn, db_path=db_path)
    chats_raw = dbmod.get_setting('telegram_chats', None, conn=conn, db_path=db_path)
    enabled = dbmod.get_setting('telegram_enabled', '1', conn=conn, db_path=db_path)
    chats, chats_source = None, 'settings'
    if chats_raw is not None:
        try:
            v = json.loads(chats_raw)
            chats = [{'id': str(c.get('id')), 'title': str(c.get('title') or '')} for c in v if isinstance(c, dict)]
        except Exception:
            chats = None
    if chats is None:
        chats, chats_source = _env_chats(), ('env' if settings.TELEGRAM_CHAT_ID else 'none')
    if tok:
        token, token_source = tok, 'settings'
    elif settings.TELEGRAM_BOT_TOKEN:
        token, token_source = settings.TELEGRAM_BOT_TOKEN, 'env'
    else:
        token, token_source = '', 'none'
    return {'token': token, 'token_source': token_source, 'chats': chats, 'chats_source': chats_source,
            'enabled': str(enabled) != '0'}


def public_config(cfg):
    return {'enabled': cfg['enabled'], 'token_set': bool(cfg['token']), 'token_masked': mask_token(cfg['token']),
            'token_source': cfg['token_source'], 'chats': cfg['chats'], 'chats_source': cfg['chats_source']}


# ── Bot API ──────────────────────────────────────────────────────────────────
def _post(http_post=None):
    if http_post is not None:
        return http_post
    import requests
    return requests.post


def call(token, method, params=None, http_post=None):
    """Bot API call -> result. Raises TelegramError(description, error_code)."""
    if not token:
        raise TelegramError('No bot token set', 0)
    try:
        resp = _post(http_post)(API % (token, method), json=params or {}, timeout=TIMEOUT_S)
    except Exception as e:
        raise TelegramError('Could not reach Telegram: %s' % redact(str(e), token), 0)
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code == 200 and data.get('ok'):
        return data.get('result')
    desc = redact(str(data.get('description') or 'HTTP %s' % resp.status_code), token)
    raise TelegramError(desc, data.get('error_code') or resp.status_code)


def friendly_error(e):
    code = getattr(e, 'code', None)
    if code in (401, 404):
        return 'Telegram rejected this bot token. Copy it again from @BotFather (it looks like 123456789:AA…).'
    if code == 409:
        return ('This bot has a webhook set (another app receives its messages), so “Find my chat” can’t read them. '
                'Type the chat ID in by hand, or use a bot made just for PLC Command Center.')
    if code == 403:
        return 'The bot was removed from that chat or blocked. Add it back to the group and send a message.'
    if code == 400 and 'chat not found' in str(e).lower():
        return 'Chat not found. Make sure the bot is in that group and someone has sent a message there.'
    return str(e)


def chat_title(chat):
    if chat.get('title'):
        return chat['title']
    name = ' '.join(x for x in (chat.get('first_name'), chat.get('last_name')) if x)
    if name:
        return name + (' (@%s)' % chat['username'] if chat.get('username') else '')
    return '@' + chat['username'] if chat.get('username') else str(chat.get('id'))


def find_chats(token, http_post=None):
    """getMe + getUpdates (no offset: nothing is consumed). Returns {'bot', 'chats', 'message'}."""
    me = call(token, 'getMe', http_post=http_post)
    bot = {'username': me.get('username'), 'name': me.get('first_name')}
    try:
        updates = call(token, 'getUpdates', {'limit': 100, 'timeout': 0}, http_post=http_post) or []
    except TelegramError as e:
        if e.code == 409:
            return {'bot': bot, 'chats': [], 'webhook': True, 'message': friendly_error(e)}
        raise
    seen = {}
    for u in updates:
        for key in ('message', 'edited_message', 'channel_post', 'edited_channel_post', 'my_chat_member', 'chat_member'):
            obj = u.get(key)
            if isinstance(obj, dict) and isinstance(obj.get('chat'), dict):
                ch = obj['chat']
                seen[str(ch.get('id'))] = {'id': str(ch.get('id')), 'type': ch.get('type', ''), 'title': chat_title(ch),
                                           'update_id': u.get('update_id', 0)}
    chats = sorted(seen.values(), key=lambda c: -c['update_id'])
    for c in chats:
        c.pop('update_id')
    msg = None
    if not chats:
        msg = ('No messages yet. Add @%s to your alarm group (or open a chat with it), send any message there, '
               'wait a few seconds, then press Find again.' % (bot['username'] or 'your bot'))
    return {'bot': bot, 'chats': chats, 'webhook': False, 'message': msg}


def send_message(token, chat_id, text, http_post=None):
    """One sendMessage -> 'ok' | 'error <code>' | 'exception: …' (same status strings as v1/v2)."""
    try:
        resp = _post(http_post)(API % (token, 'sendMessage'), json={'chat_id': chat_id, 'text': text}, timeout=TIMEOUT_S)
        if resp.status_code == 200:
            return 'ok'
        return 'error %s' % resp.status_code
    except Exception as e:
        return 'exception: %s' % redact(str(e), token)
