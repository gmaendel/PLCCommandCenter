"""Isolated test environment. Import FIRST in every test module.

Points settings at a temp dir, forces dry-run, never loads the real .env
(so no real Telegram token / password is ever used by tests).
"""
import os
import sys
import logging
import tempfile

TMP = tempfile.mkdtemp(prefix='plccc-test-')
os.environ.update({
    'PLCCC_ENV_FILE': os.path.join(TMP, 'no.env'),
    'PLCCC_DRY_RUN': '1',
    'PLCCC_DB_PATH': os.path.join(TMP, 'test.db'),
    'PLCCC_CONFIG_PATH': os.path.join(TMP, 'pump_config.json'),
    'PLCCC_ACK_FILE': os.path.join(TMP, 'ack'),
    'PLCCC_CHAT_DB': os.path.join(TMP, 'chat.db'),
    'DASHBOARD_PASSWORD': 'test-pw',
    'TELEGRAM_BOT_TOKEN': 'TEST',
    'TELEGRAM_CHAT_ID': '0',
    'PLCCC_SEED_V1_DEVICES': '1',     # tests use the classic two-device layout
    'PLCCC_IMESSAGE_CHAT': 'Recharge Pump Alarm',
})
logging.disable(logging.INFO)
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')
if APP not in sys.path:
    sys.path.insert(0, APP)


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeAck:
    def __init__(self):
        self.file = False
        self.imsg = False
        self.touched = 0
        self.cleared = 0
        self.since = []

    def file_acked(self):
        return self.file

    def imessage_acked(self, since_timestamp=None):
        self.since.append(since_timestamp)
        return self.imsg

    def touch(self):
        self.touched += 1
        self.file = True

    def clear(self):
        self.cleared += 1
        self.file = False
