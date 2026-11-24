"""EscalatingAlarm (recharge pump) unit tests with a fake clock."""
import unittest

from tests._env import FakeClock, FakeAck  # noqa: F401  (sets env first)
from plccc.alarms import EscalatingAlarm
from plccc.db import PUMP_ALARM_CONFIG


class Harness:
    def __init__(self, cfg=PUMP_ALARM_CONFIG):
        self.clock = FakeClock()
        self.ack = FakeAck()
        self.sent = []
        self.states = []
        self.alarm = EscalatingAlarm(cfg, clock=self.clock, send=lambda t, k: self.sent.append((k, t)),
                                     ack=self.ack, on_state=lambda **kw: self.states.append(kw))

    def poll(self, value, threshold=2000, enabled=True, step=60):
        ev = self.alarm.evaluate(value=value, threshold=threshold, alarm_enabled=enabled, display_value=value)
        self.clock.advance(step)
        return ev

    def kinds(self):
        return [k for k, _ in self.sent]


class EscalationTests(unittest.TestCase):
    def test_startup_clears_ack_file(self):
        h = Harness()
        self.assertEqual(h.ack.cleared, 1)

    def test_alarm_message_text_matches_v1(self):
        h = Harness()
        h.poll(490)
        kind, text = h.sent[0]
        self.assertEqual(kind, 'alarm')
        self.assertTrue(text.startswith('\u26a0\ufe0f ALARM: Recharge Pump GPM is below 2000 GPM!\nCurrent GPM: 490\nTime: '))
        self.assertTrue(text.endswith("\n\n(Reply to this message to acknowledge)"))
        self.assertEqual(h.ack.cleared, 2)        # new alarm clears the ack file

    def test_no_alarm_when_normal_or_equal(self):
        h = Harness()
        for v in (2000, 2500, 3000):
            h.poll(v)
        self.assertEqual(h.sent, [])

    def test_escalation_timing_60s_polls(self):
        h = Harness()
        times = {}
        for i in range(20):
            for ev in h.poll(500):
                times.setdefault(ev, i)
        self.assertEqual(h.kinds(), ['alarm', 'escalation_1', 'final'])
        # v1: escalation only when >= 400 s since last; with 60 s polls -> 7th poll after (420 s)
        self.assertEqual(times['alarm'], 0)
        self.assertEqual(times['escalation_1'], 7)
        self.assertEqual(times['final'], 14)
        self.assertIn('ESCALATION 1: Recharge Pump still below 2000!', h.sent[1][1])
        self.assertIn('FINAL WARNING: Recharge Pump still below 2000!', h.sent[2][1])

    def test_exactly_400s_escalates(self):
        h = Harness()
        h.poll(500, step=400)
        h.poll(500, step=1)
        self.assertEqual(h.kinds(), ['alarm', 'escalation_1'])

    def test_399s_does_not_escalate(self):
        h = Harness()
        h.poll(500, step=399)
        h.poll(500, step=1)
        self.assertEqual(h.kinds(), ['alarm'])

    def test_file_ack_silences(self):
        h = Harness()
        h.poll(500)
        h.ack.file = True
        for _ in range(30):
            h.poll(500)
        self.assertEqual(h.kinds(), ['alarm'])
        self.assertEqual(h.alarm.stage, 4)

    def test_imessage_ack_silences_and_touches_file(self):
        h = Harness()
        start = h.clock()
        h.poll(500)
        h.poll(500)
        h.ack.imsg = True
        h.poll(500)
        self.assertEqual(h.ack.touched, 1)
        for _ in range(20):
            h.poll(500)
        self.assertEqual(h.kinds(), ['alarm'])
        self.assertTrue(all(s == start for s in h.ack.since))   # only replies since the alarm began

    def test_ack_after_escalation_1_stops_final(self):
        h = Harness()
        for _ in range(8):
            h.poll(500)
        self.assertEqual(h.kinds(), ['alarm', 'escalation_1'])
        h.ack.file = True
        for _ in range(20):
            h.poll(500)
        self.assertEqual(h.kinds(), ['alarm', 'escalation_1'])

    def test_ack_on_same_poll_as_escalation_wins(self):
        h = Harness()
        for _ in range(7):
            h.poll(500)
        h.ack.file = True
        h.poll(500)      # 420 s: ack is checked before escalation, like v1
        self.assertEqual(h.kinds(), ['alarm'])

    def test_recovery_cooldown_and_rearm(self):
        h = Harness()
        h.poll(500)                       # t=0 alarm
        ev = h.poll(2500)                 # t=60 normal -> cooldown starts (no message: v1 sends none)
        self.assertEqual(ev, ['cooldown_start'])
        self.assertEqual(h.kinds(), ['alarm'])
        self.assertEqual(h.poll(500), ['cooldown'])    # t=120: still cooling, no evaluation
        self.assertEqual(h.poll(500), ['rearmed'])     # t=180: 120 s elapsed -> re-armed (v1: no read here)
        self.assertEqual(h.poll(500), ['alarm'])       # t=240: v1 evaluates again on this poll
        self.assertEqual(h.kinds(), ['alarm', 'alarm'])

    def test_switch_off_during_alarm_behaves_like_normal(self):
        h = Harness()
        h.poll(500)
        self.assertEqual(h.poll(500, enabled=False), ['cooldown_start'])
        for _ in range(10):
            h.poll(500, enabled=False)
        self.assertEqual(h.kinds(), ['alarm'])
        self.assertFalse(h.alarm.active)

    def test_switch_off_never_alarms(self):
        h = Harness()
        for _ in range(30):
            h.poll(10, enabled=False)
        self.assertEqual(h.sent, [])

    def test_threshold_change_is_used_in_messages(self):
        h = Harness()
        h.poll(1200, threshold=1500)
        self.assertIn('below 1500 GPM', h.sent[0][1])
        for _ in range(7):
            h.poll(1200, threshold=1300)
        self.assertIn('still below 1300!', h.sent[1][1])

    def test_rearm_clears_ack_so_next_alarm_escalates(self):
        h = Harness()
        h.poll(500)
        h.ack.file = True
        h.poll(500)
        h.poll(2500)
        h.poll(2500)
        h.poll(2500)                  # rearmed, ack cleared
        self.assertFalse(h.ack.file)
        for _ in range(8):
            h.poll(500)
        self.assertEqual(h.kinds(), ['alarm', 'alarm', 'escalation_1'])

    def test_state_callbacks(self):
        h = Harness()
        h.poll(500)
        self.assertTrue(h.states[0]['active'] and h.states[0]['new'])
        h.poll(2500)
        self.assertFalse(h.states[-1]['active'])


if __name__ == '__main__':
    unittest.main()
