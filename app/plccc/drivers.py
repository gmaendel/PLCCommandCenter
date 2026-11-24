"""One driver per PLC family, all READ-ONLY (no write methods are exposed).

Each driver: connect() / close() / connected / read(tags) -> list of (value|None, error|None)
in tag order. Address parsing + decoding is carried over verbatim from the v1 monitors.
"""
import json
import os
import re
import socket
import struct
import threading
import time
from urllib.parse import urlparse

TYPE_LABELS = {
    'allen_bradley_slc': 'Allen-Bradley SLC/MicroLogix',
    'allen_bradley_clx': 'Allen-Bradley ControlLogix',
    'siemens_s7': 'Siemens S7 device',
    'modbus_tcp': 'Modbus TCP device',
    'opc_ua': 'OPC UA server',
    'mqtt': 'MQTT broker',
}
DEFAULT_PORTS = {'allen_bradley_slc': 44818, 'allen_bradley_clx': 44818, 'siemens_s7': 102, 'modbus_tcp': 502,
                 'opc_ua': 4840, 'mqtt': 1883}
OPCUA_POLICIES = ('None', 'Basic256Sha256', 'Aes128Sha256RsaOaep', 'Aes256Sha256RsaPss')
OPCUA_MODES = ('Sign', 'SignAndEncrypt')


class DriverError(Exception):
    pass


# ── Modbus helpers (from modbus_monitor.py) ──────────────────────────────────
def modbus_parse_address(addr_str):
    s = addr_str.strip().upper()
    if s.startswith('HR'):
        return 3, int(s[2:]) - 1
    elif s.startswith('IR'):
        return 4, int(s[2:]) - 1
    elif s.startswith('CO'):
        return 1, int(s[2:]) - 1
    elif s.startswith('DI'):
        return 2, int(s[2:]) - 1
    else:
        return 3, int(s) - 1


def modbus_reg_count(data_type):
    return 2 if (data_type or 'INT').upper() in ('REAL', 'DINT') else 1


def modbus_decode(registers, data_type, word_swap=False):
    dt = (data_type or 'INT').upper()
    if word_swap and len(registers) >= 2:
        registers = [registers[1], registers[0]]
    if dt == 'REAL':
        raw = (registers[0] << 16) | registers[1]
        return float(struct.unpack('>f', struct.pack('>I', raw))[0])
    elif dt == 'DINT':
        raw = (registers[0] << 16) | registers[1]
        if raw >= 0x80000000:
            raw -= 0x100000000
        return float(raw)
    elif dt == 'INT':
        v = registers[0]
        if v >= 0x8000:
            v -= 0x10000
        return float(v)
    return float(registers[0])


# ── Siemens helpers (from siemens_monitor.py) ────────────────────────────────
def s7_parse_address(addr_str):
    s = addr_str.strip().upper()
    m = re.match(r'DB(\d+)\.(REAL|INT|WORD|DINT|DBD|DBX|BOOL)(\d+)(?:\.(\d+))?', s)
    if not m:
        raise ValueError('Unsupported address format: %r' % addr_str)
    db_num = int(m.group(1))
    type_str = m.group(2)
    byte_off = int(m.group(3))
    bit_off = int(m.group(4)) if m.group(4) is not None else 0
    if type_str == 'DBD':
        type_str = 'REAL'
    elif type_str == 'DBX':
        type_str = 'BOOL'
    return db_num, type_str, byte_off, bit_off


def s7_byte_count(data_type):
    dt = data_type.upper()
    if dt in ('REAL', 'DINT'):
        return 4
    elif dt == 'BOOL':
        return 1
    return 2


def s7_decode(data, data_type, bit_offset=0):
    from snap7.util import get_real, get_int, get_word, get_bool, get_dint
    dt = data_type.upper()
    if dt == 'REAL':
        return float(get_real(data, 0))
    elif dt == 'INT':
        return float(get_int(data, 0))
    elif dt == 'WORD':
        return float(get_word(data, 0))
    elif dt == 'DINT':
        return float(get_dint(data, 0))
    elif dt == 'BOOL':
        return float(get_bool(data, 0, bit_offset))
    return float(get_real(data, 0))


def tcp_probe(host, port, timeout=3):
    """v1 pump_monitor.ping_plc(): TCP connect to the PLC port (ICMP is blocked)."""
    try:
        ip = socket.gethostbyname(host)
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def device_options(dev):
    """devices.options JSON (v2.2: credentials/TLS/security for OPC UA + MQTT) as a dict."""
    try:
        raw = dev['options'] if 'options' in dev.keys() else None
    except Exception:
        raw = None
    if not raw:
        return {}
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def to_number(v):
    """Coerce an OPC UA / MQTT value to float (bools -> 0/1). Raises ValueError if not numeric."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float('inf'), float('-inf')):
            raise ValueError('value is not a finite number')
        return f
    if isinstance(v, bytes):
        v = v.decode('utf-8', 'replace')
    if isinstance(v, str):
        s = v.strip()
        low = s.lower()
        if low in ('true', 'on', 'yes', 'open', 'running'):
            return 1.0
        if low in ('false', 'off', 'no', 'closed', 'stopped'):
            return 0.0
        try:
            f = float(s)
        except ValueError:
            raise ValueError('non-numeric value %r' % s[:40])
        return to_number(f)
    if hasattr(v, 'value') and not isinstance(v, (list, tuple, dict)):     # enums / Variant-like
        return to_number(v.value)
    raise ValueError('non-numeric value %r' % (v,))


class BaseDriver:
    persistent = True       # keep the connection open between polls
    close_on_error = True   # poller drops the connection after a failed poll

    def __init__(self, dev):
        self.dev = dev
        self.host = dev['host']
        self.port = dev['port'] or DEFAULT_PORTS.get(dev['type'], 0)
        self._client = None

    @property
    def connected(self):
        return self._client is not None

    def connect(self):
        raise NotImplementedError

    def close(self):
        c, self._client = self._client, None
        if c is not None:
            try:
                self._close_client(c)
            except Exception:
                pass

    def _close_client(self, c):
        c.close()

    def read(self, tags):
        raise NotImplementedError


class _PycommDriver(BaseDriver):
    driver_cls = None

    def connect(self):
        if self._client is None:
            c = self.driver_cls('%s:%s' % (self.host, self.port))
            c.open()
            self._client = c
        elif not self._client.connected:
            self._client.open()

    def read(self, tags):
        addrs = [t['address'] for t in tags]
        results = self._client.read(*addrs)
        if not isinstance(results, (list, tuple)):
            results = [results]
        if len(results) != len(addrs):
            raise DriverError('expected %d results, got %d' % (len(addrs), len(results)))
        out = []
        for r in results:
            if r is None:
                out.append((None, 'no result'))
            elif r.error:
                out.append((None, str(r.error)))
            elif r.value is None:
                out.append((None, 'no value'))
            else:
                out.append((r.value, None))
        return out


class SLCDriver(_PycommDriver):
    """MicroLogix / SLC-500 (pycomm3 SLCDriver). Batch read in one request, like v1."""
    @property
    def driver_cls(self):
        from pycomm3 import SLCDriver as D
        return D


class LogixDriver(_PycommDriver):
    """ControlLogix / CompactLogix symbolic tags (pycomm3 LogixDriver)."""
    @property
    def driver_cls(self):
        from pycomm3 import LogixDriver as D
        return D


class ModbusDriver(BaseDriver):
    # v1 opened a fresh TCP connection every poll; some devices drop idle sockets.
    persistent = False

    def connect(self):
        if self._client is None:
            from pymodbus.client import ModbusTcpClient
            c = ModbusTcpClient(self.host, port=self.port, timeout=5)
            if not c.connect():
                try:
                    c.close()
                except Exception:
                    pass
                raise DriverError('Could not connect to %s:%s' % (self.host, self.port))
            self._client = c

    def read(self, tags):
        unit_id = self.dev['unit_id'] if self.dev['unit_id'] is not None else 1
        word_swap = bool(self.dev['word_swap']) if 'word_swap' in self.dev.keys() else False
        out = []
        for tag in tags:
            try:
                fc, reg = modbus_parse_address(tag['address'])
                dt = tag['data_type'] or 'INT'
                count = modbus_reg_count(dt)
                c = self._client
                if fc == 3:
                    rr = c.read_holding_registers(address=reg, count=count, slave=unit_id)
                elif fc == 4:
                    rr = c.read_input_registers(address=reg, count=count, slave=unit_id)
                elif fc == 1:
                    rr = c.read_coils(address=reg, count=1, slave=unit_id)
                else:
                    rr = c.read_discrete_inputs(address=reg, count=1, slave=unit_id)
                if rr.isError():
                    out.append((None, 'Modbus error - %s' % rr))
                    continue
                if fc in (1, 2):
                    out.append((float(rr.bits[0]), None))
                else:
                    out.append((round(modbus_decode(rr.registers, dt, word_swap=word_swap), 4), None))
            except Exception as e:
                out.append((None, str(e)))
        return out


class S7Driver(BaseDriver):
    """Siemens S7 via python-snap7; persistent client like v1."""

    def connect(self):
        import snap7
        if self._client is None:
            self._client = snap7.client.Client()
        if not self._client.get_connected():
            ip = socket.gethostbyname(self.host)
            rack = self.dev['rack'] if self.dev['rack'] is not None else 0
            slot = self.dev['slot'] if self.dev['slot'] is not None else 1
            try:
                self._client.connect(ip, rack=rack, slot=slot, tcp_port=self.port)
            except Exception:
                self.close()
                raise

    def _close_client(self, c):
        c.disconnect()
        try:
            c.destroy()
        except Exception:
            pass

    def read(self, tags):
        out = []
        for i, tag in enumerate(tags):
            try:
                db_num, dt, byte_off, bit_off = s7_parse_address(tag['address'])
            except Exception as e:
                out.append((None, str(e)))
                continue
            try:
                data = self._client.db_read(db_num, byte_off, s7_byte_count(dt))
                out.append((round(s7_decode(data, dt, bit_offset=bit_off), 4), None))
            except Exception as e:
                # v1: a failed db_read means the socket is likely dead -> drop the client
                # and skip the remaining tags; reconnect next poll.
                out.append((None, str(e)))
                out.extend([(None, 'skipped after previous read error')] * (len(tags) - i - 1))
                self.close()
                break
        return out


# ── OPC UA (asyncua sync wrapper) ────────────────────────────────────────────
def opcua_endpoint(host, port=None):
    """Accepts 'opc.tcp://host:4840/path' or a bare host -> (url, host, port)."""
    h = (host or '').strip()
    if not h.lower().startswith('opc.tcp://'):
        h = 'opc.tcp://%s:%s' % (h, port or DEFAULT_PORTS['opc_ua'])
    u = urlparse(h)
    if not u.hostname:
        raise DriverError('Invalid OPC UA endpoint %r' % host)
    return h, u.hostname, u.port or DEFAULT_PORTS['opc_ua']


class OpcUaDriver(BaseDriver):
    """OPC UA client (asyncua.sync). Tags are NodeIds, e.g. ns=2;s=Pump.GPM or i=2258.
    Options: username, password, security_policy (None default), security_mode (Sign|SignAndEncrypt)."""
    timeout = 5

    def __init__(self, dev, client_factory=None):
        super().__init__(dev)
        self.opts = device_options(dev)
        self.url, self.probe_host, self.port = opcua_endpoint(dev['host'], dev['port'])
        self.host = self.probe_host
        self._factory = client_factory

    def _make_client(self):
        if self._factory:
            return self._factory(self.url)
        from asyncua.sync import Client
        return Client(self.url, timeout=self.timeout)

    def _setup_security(self, c):
        policy = (self.opts.get('security_policy') or 'None').strip()
        if policy == 'None':
            return
        if policy not in OPCUA_POLICIES:
            raise DriverError('Unsupported OPC UA security policy %r' % policy)
        mode = self.opts.get('security_mode') or 'SignAndEncrypt'
        if mode not in OPCUA_MODES:
            raise DriverError('Unsupported OPC UA security mode %r' % mode)
        cert, key = opcua_client_cert()
        c.set_security_string('%s,%s,%s,%s' % (policy, mode, cert, key))

    def connect(self):
        if self._client is not None:
            return
        c = self._make_client()
        if self.opts.get('username'):
            c.set_user(self.opts['username'])
            c.set_password(self.opts.get('password') or '')
        self._setup_security(c)
        try:
            c.connect()
        except Exception as e:
            try:
                c.disconnect()
            except Exception:
                pass
            raise DriverError('OPC UA connect to %s failed: %s' % (self.url, e or type(e).__name__))
        self._client = c

    def _close_client(self, c):
        c.disconnect()

    def read(self, tags):
        c = self._client
        nodes, out = [], [None] * len(tags)
        for i, t in enumerate(tags):
            try:
                nodes.append((i, c.get_node(t['address'].strip())))
            except Exception as e:
                out[i] = (None, 'bad NodeId: %s' % e)
        dvs = None
        if nodes:
            try:
                dvs = c.read_attributes([n for _, n in nodes])     # one request; DataValues carry per-node status
            except Exception:
                dvs = None          # batch failed: fall back to per-node reads
        conn_errors = 0
        for k, (i, n) in enumerate(nodes):
            try:
                if dvs is not None:
                    dv = dvs[k]
                    sc = getattr(dv, 'StatusCode', None)
                    if sc is not None and not sc.is_good():
                        out[i] = (None, 'OPC UA status %s' % (getattr(sc, 'name', None) or sc))
                        continue
                    v = dv.Value.Value if dv.Value is not None else None
                else:
                    v = n.read_value()
                if v is None:
                    out[i] = (None, 'no value')
                    continue
                out[i] = (to_number(v), None)
            except (ConnectionError, TimeoutError, OSError) as e:
                conn_errors += 1
                out[i] = (None, 'connection: %s' % (e or type(e).__name__))
            except Exception as e:
                out[i] = (None, str(e) or type(e).__name__)
        if nodes and conn_errors == len(nodes):
            raise DriverError('OPC UA connection lost: %s' % out[nodes[0][0]][1])
        return out


def opcua_client_cert():
    """Self-signed client certificate for OPC UA security policies (created once in data/opcua/)."""
    import plccc_settings as settings
    d = os.path.join(settings.DATA_DIR, 'opcua')
    cert, key = os.path.join(d, 'plccc-client-cert.der'), os.path.join(d, 'plccc-client-key.pem')
    if not (os.path.exists(cert) and os.path.exists(key)):
        os.makedirs(d, exist_ok=True)
        import asyncio
        from pathlib import Path
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        from cryptography.x509.oid import ExtendedKeyUsageOID
        host = socket.gethostname()
        asyncio.run(setup_self_signed_certificate(
            Path(key), Path(cert), 'urn:%s:PLCCommandCenter' % host, host,
            [ExtendedKeyUsageOID.CLIENT_AUTH], {'commonName': 'PLCCommandCenter'}))
        os.chmod(key, 0o600)
    return cert, key


# ── MQTT (paho-mqtt, persistent subscriber with last-value cache) ────────────
def mqtt_split_address(addr):
    """'farm/gen/state|temp' -> ('farm/gen/state', 'temp'); no '|' -> (topic, '')."""
    topic, _, path = (addr or '').partition('|')
    return topic.strip(), path.strip()


_PATH_RE = re.compile(r'([^.\[\]]+)|\[(\d+)\]')


def json_path_get(obj, path):
    """Minimal JSON path: 'temp', 'data.sensors[0].value', '$.a.b'."""
    p = path.strip()
    if p.startswith('$'):
        p = p[1:].lstrip('.')
    for m in _PATH_RE.finditer(p):
        key, idx = m.group(1), m.group(2)
        if idx is not None:
            obj = obj[int(idx)]
        else:
            if isinstance(obj, list) and key.isdigit():
                obj = obj[int(key)]
            else:
                obj = obj[key]
    return obj


def mqtt_decode(payload, path=''):
    text = payload.decode('utf-8', 'replace') if isinstance(payload, (bytes, bytearray)) else str(payload)
    if path:
        try:
            obj = json.loads(text)
        except ValueError:
            raise ValueError('payload is not JSON')
        try:
            return to_number(json_path_get(obj, path))
        except (KeyError, IndexError, TypeError):
            raise ValueError('JSON path %r not found' % path)
    try:
        return to_number(text)
    except ValueError:
        try:
            return to_number(json.loads(text))
        except Exception:
            raise ValueError('non-numeric payload %r' % text[:40])


class MqttDriver(BaseDriver):
    """Subscribes to every tag topic and caches the last message + arrival time.
    read() returns the cached value; a tag whose last message is older than stale_after
    (default 300 s) errors, and when every tag is stale/unconnected the poll fails (-> offline)."""
    close_on_error = False      # keep the subscription alive across failed/stale polls
    connect_wait = 5.0
    first_message_grace = 1.0

    def __init__(self, dev, client_factory=None, clock=time.time):
        super().__init__(dev)
        self.opts = device_options(dev)
        self.tls = bool(self.opts.get('tls'))
        if not dev['port']:
            self.port = 8883 if self.tls else 1883
        self.probe_host = self.host
        self.stale_after = float(self.opts.get('stale_after') or 300)
        self._factory = client_factory
        self.clock = clock
        self._lock = threading.Lock()
        self._cache = {}            # topic -> (payload bytes, arrival time)
        self._topics = set()
        self._connected = threading.Event()
        self._connected_at = None
        self._last_rc = None

    # paho callbacks (VERSION2 signatures)
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = not getattr(reason_code, 'is_failure', bool(reason_code))
        self._last_rc = str(reason_code)
        if ok:
            with self._lock:
                topics = sorted(self._topics)
            for t in topics:
                client.subscribe(t, qos=0)
            self._connected_at = self.clock()
            self._connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected.clear()
        self._last_rc = str(reason_code)

    def _on_message(self, client, userdata, msg):
        with self._lock:
            self._cache[msg.topic] = (bytes(msg.payload), self.clock())

    def _make_client(self):
        if self._factory:
            return self._factory(self)
        import paho.mqtt.client as mqtt
        cid = self.opts.get('client_id') or 'plccc-%s-%s' % (self.dev['id'], socket.gethostname().split('.')[0])
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=cid,
                        protocol=mqtt.MQTTv311, clean_session=True)
        if self.opts.get('username'):
            c.username_pw_set(self.opts['username'], self.opts.get('password') or None)
        if self.tls:
            import ssl
            c.tls_set(cert_reqs=ssl.CERT_NONE if self.opts.get('tls_insecure') else ssl.CERT_REQUIRED)
            if self.opts.get('tls_insecure'):
                c.tls_insecure_set(True)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.on_connect, c.on_disconnect, c.on_message = self._on_connect, self._on_disconnect, self._on_message
        c.connect_async(self.host, int(self.port), keepalive=30)
        c.loop_start()
        return c

    @property
    def connected(self):
        return self._client is not None and self._connected.is_set()

    def connect(self):
        if self._client is None:
            self._client = self._make_client()
        if not self._connected.wait(self.connect_wait):
            raise DriverError('MQTT broker %s:%s not connected (%s)' % (self.host, self.port, self._last_rc or 'no answer'))

    def _close_client(self, c):
        self._connected.clear()
        try:
            c.disconnect()
        finally:
            c.loop_stop()

    def subscribe(self, topics):
        new = []
        with self._lock:
            for t in topics:
                if t and t not in self._topics:
                    self._topics.add(t)
                    new.append(t)
        if new and self._client is not None and self._connected.is_set():
            for t in new:
                self._client.subscribe(t, qos=0)
            time.sleep(self.first_message_grace if self._factory is None else 0)   # retained messages
        return new

    def read(self, tags):
        parsed = [mqtt_split_address(t['address']) for t in tags]
        self.subscribe([tp for tp, _ in parsed])
        if not self._connected.is_set():
            raise DriverError('MQTT broker %s:%s disconnected' % (self.host, self.port))
        now = self.clock()
        out = []
        for topic, path in parsed:
            if not topic:
                out.append((None, 'empty topic'))
                continue
            with self._lock:
                hit = self._cache.get(topic)
            if hit is None:
                out.append((None, 'no message yet on %s' % topic))
                continue
            payload, ts = hit
            age = now - ts
            if age > self.stale_after:
                out.append((None, 'stale: last message %ds ago' % int(age)))
                continue
            try:
                out.append((mqtt_decode(payload, path), None))
            except ValueError as e:
                out.append((None, str(e)))
        return out

    def cache_age(self, topic):
        with self._lock:
            hit = self._cache.get(topic)
        return None if hit is None else self.clock() - hit[1]


DRIVERS = {
    'allen_bradley_slc': SLCDriver,
    'allen_bradley_clx': LogixDriver,
    'modbus_tcp': ModbusDriver,
    'siemens_s7': S7Driver,
    'opc_ua': OpcUaDriver,
    'mqtt': MqttDriver,
}


def make_driver(dev):
    cls = DRIVERS.get(dev['type'])
    if cls is None:
        raise DriverError('Unsupported device type %r' % dev['type'])
    return cls(dev)


def apply_scaling(value, tag):
    """v1 scaling: result = raw*factor + offset (rounded to 4) only when non-default."""
    keys = tag.keys()
    sf = tag['scale_factor'] if 'scale_factor' in keys and tag['scale_factor'] is not None else 1.0
    so = tag['scale_offset'] if 'scale_offset' in keys and tag['scale_offset'] is not None else 0.0
    if sf != 1.0 or so != 0.0:
        return round(float(value) * sf + so, 4)
    return value
