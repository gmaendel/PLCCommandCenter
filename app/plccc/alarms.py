"""Alarm state machines. Pure logic: time comes from an injected clock and every side
effect goes through callbacks, so they are unit-testable with a fake clock.

ThresholdAlarm   == v1 modbus/siemens/ab check_alarm(): alert once on entering alarm,
                    CLEAR once on leaving it, nothing in between, no escalation.
EscalatingAlarm  == v1 pump_monitor.py: ALARM -> (400 s) ESCALATION 1 -> (400 s) FINAL WARNING,
                    'ack' silences further escalations, return to normal (or alarm switch
                    turned off) -> 120 s cooldown with no alarm evaluation -> re-armed.
CommMonitor      == offline / recovery detection (v1 generic: 3 failed polls; v1 pump:
                    10 failed reads + TCP port probe).
"""
import datetime
import logging

log = logging.getLogger('plccc.alarms')


def now_str(clock_value=None):
    if clock_value is None:
        return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return datetime.datetime.fromtimestamp(clock_value).strftime('%Y-%m-%d %H:%M:%S')


class SafeDict(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def render(template, **kw):
    try:
        return template.format_map(SafeDict(**kw))
    except Exception:
        return template


# ─────────────────────────────────────────────────────────────────────────────
class ThresholdAlarm:
    """Exact v1 check_alarm() semantics (state starts inactive on every service start)."""

    def __init__(self):
        self.active = False

    def evaluate(self, *, value, alarm_enabled, alarm_low, alarm_high, alarm_message,
                 device_name, tag_name, units):
        """Returns ('alarm'|'clear'|None, message|None)."""
        if not alarm_enabled:
            return None, None
        in_alarm = False
        reason = ''
        if alarm_low is not None and value < alarm_low:
            in_alarm, reason = True, 'below low limit of %s %s' % (alarm_low, units)
        elif alarm_high is not None and value > alarm_high:
            in_alarm, reason = True, 'above high limit of %s %s' % (alarm_high, units)
        if in_alarm and not self.active:
            self.active = True
            msg = alarm_message.strip() if alarm_message and alarm_message.strip() else (
                '\u26a0\ufe0f ALARM [%s] %s = %s %s \u2014 %s\n' % (device_name, tag_name, value, units, reason)
                + "(Reply to this message to acknowledge)")
            return 'alarm', msg
        if not in_alarm and self.active:
            self.active = False
            return 'clear', '\u2705 CLEAR [%s] %s = %s %s \u2014 alarm cleared' % (device_name, tag_name, value, units)
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
class EscalatingAlarm:
    STAGE_IDLE, STAGE_ALARM, STAGE_ESC1, STAGE_FINAL, STAGE_ACKED = 0, 1, 2, 3, 4
    COOLDOWN_TOLERANCE_S = 2.0   # absorbs poll-timer jitter so re-arm lands on the same poll as v1

    def __init__(self, config, *, clock, send, ack, on_state=None, name='Pump GPM'):
        self.cfg = config or {}
        self.clock = clock
        self.send = send            # send(text, kind)
        self.ack = ack              # AckChecker-like: file_acked(), imessage_acked(since), touch(), clear()
        self.on_state = on_state or (lambda **kw: None)
        self.name = name
        self.wait = float(self.cfg.get('escalation_wait_s', 400))
        self.cooldown = float(self.cfg.get('cooldown_s', 120))
        self.msgs = self.cfg.get('messages') or {}
        self.direction = self.cfg.get('direction', 'low')
        self.notify_clear = bool(self.cfg.get('notify_clear', False))
        self.active = False
        self.stage = 0
        self.last_escalation_time = 0.0
        self.alarm_start_time = 0.0
        self.cooldown_until = None
        # v1: "Ensure ACK file doesn't exist on startup"
        self.ack.clear()

    @property
    def in_cooldown(self):
        return self.cooldown_until is not None

    def _breached(self, value, threshold):
        return value > threshold if self.direction == 'high' else value < threshold

    def _msg(self, key, **kw):
        return render(self.msgs.get(key, key.upper() + ': {tag} {value} (threshold {threshold})'), **kw)

    def evaluate(self, *, value, threshold, alarm_enabled, display_value=None, extra=None):
        """Call once per successful read. Returns a list of event names (for logs/tests)."""
        now = self.clock()
        events = []
        shown = value if display_value is None else display_value
        kw = dict(value=shown, threshold=threshold, time=now_str(now), tag=self.name)
        kw.update(extra or {})

        if self.cooldown_until is not None:
            if now + self.COOLDOWN_TOLERANCE_S >= self.cooldown_until:
                # v1: after the cooldown sleep -> reset, remove ack file, "re-armed";
                # the next evaluation happens on the following poll.
                self.cooldown_until = None
                self.active = False
                self.stage = 0
                self.ack.clear()
                log.info('Cooldown complete. Monitoring re-armed.')
                self.on_state(active=False, stage=None, acked=False, value=value)
                events.append('rearmed')
            else:
                events.append('cooldown')
            return events

        if self._breached(value, threshold) and alarm_enabled:
            if not self.active:
                self.active = True
                self.stage = self.STAGE_ALARM
                self.ack.clear()
                self.send(self._msg('alarm', **kw), 'alarm')
                self.last_escalation_time = now
                self.alarm_start_time = now
                self.on_state(active=True, stage=self.stage, acked=False, value=value, new=True)
                events.append('alarm')
            else:
                since = now - self.last_escalation_time
                user_acked = False
                if self.ack.file_acked():
                    user_acked = True
                elif self.ack.imessage_acked(since_timestamp=self.alarm_start_time):
                    user_acked = True
                    self.ack.touch()
                if user_acked and self.stage != self.STAGE_ACKED:
                    log.info('Alarm acknowledged. Silencing further escalations.')
                    self.stage = self.STAGE_ACKED
                    self.on_state(active=True, stage=self.stage, acked=True, value=value)
                    events.append('acked')
                if self.stage == self.STAGE_ALARM and since >= self.wait:
                    self.send(self._msg('escalation_1', **kw), 'escalation_1')
                    self.stage = self.STAGE_ESC1
                    self.last_escalation_time = now
                    self.on_state(active=True, stage=self.stage, acked=False, value=value)
                    events.append('escalation_1')
                elif self.stage == self.STAGE_ESC1 and since >= self.wait:
                    self.send(self._msg('final', **kw), 'final')
                    self.stage = self.STAGE_FINAL
                    self.last_escalation_time = now
                    self.on_state(active=True, stage=self.stage, acked=False, value=value)
                    events.append('final')
        else:
            if self.active:
                log.info('%s normalized (%s). Entering %d-second cooldown...', self.name, shown, int(self.cooldown))
                self.cooldown_until = now + self.cooldown
                if self.notify_clear and 'clear' in self.msgs:
                    self.send(self._msg('clear', **kw), 'clear')
                self.on_state(active=False, stage=0, acked=False, value=value, cooling=True)
                events.append('cooldown_start')
        return events


# ─────────────────────────────────────────────────────────────────────────────
class CommMonitor:
    """Per-device offline detection.

    generic (v1 modbus/siemens/ab):  offline_after=3, probe_port=False.
        A failed poll increments the count; at >= 3 one COMM LOSS alert; RECOVERY on the
        next successful poll.
    pump (v1 pump_monitor):          offline_after=10, probe_port=True.
        At >= 10 failures probe host:port. Port open -> 'driver wedged': caller rebuilds
        the driver, count resets, no alert. Port closed -> one COMM LOSS (count stays >= 10,
        so it re-probes every poll); RECOVERY on the next successful read.
    """

    def __init__(self, *, device_name, type_label, host, port, config=None, send, probe, clock):
        cfg = config or {}
        self.name = device_name
        self.type_label = type_label
        self.host, self.port = host, port
        self.offline_after = int(cfg.get('offline_after', 3))
        self.probe_port = bool(cfg.get('probe_port', False))
        self.msgs = cfg.get('messages') or {}
        self.send = send
        self.probe = probe
        self.clock = clock
        self.fail_count = 0
        self.offline = False

    def _fmt(self, key, default):
        return render(self.msgs.get(key, default), name=self.name, host=self.host, port=self.port,
                      time=now_str(self.clock()), type=self.type_label)

    def success(self):
        events = []
        if self.offline:
            self.offline = False
            self.send(self._fmt('recovery', '\u2705 RECOVERY [{name}]\n{type} back online at {host}:{port}\nTime: {time}'),
                      'recovery')
            events.append('recovery')
        self.fail_count = 0
        return events

    def failure(self):
        """Returns events; 'rebuild_driver' tells the caller to recreate the driver."""
        self.fail_count += 1
        events = []
        if self.fail_count >= self.offline_after:
            if self.probe_port:
                log.warning('[%s] %d consecutive read failures. Pinging PLC...', self.name, self.fail_count)
                if self.probe(self.host, self.port):
                    log.warning('[%s] Ping successful! Driver seems wedged. Restarting driver...', self.name)
                    self.fail_count = 0
                    events.append('rebuild_driver')
                    return events
                log.warning('[%s] Ping failed. PLC might be offline. Retrying normally...', self.name)
            if not self.offline:
                self.offline = True
                self.send(self._fmt('loss', '\U0001f534 COMM LOSS [{name}]\n{type} unreachable at {host}:{port}\n'
                                            'Time: {time}\nMonitoring will resume automatically when it comes back online.'),
                          'comm_loss')
                events.append('comm_loss')
        return events
