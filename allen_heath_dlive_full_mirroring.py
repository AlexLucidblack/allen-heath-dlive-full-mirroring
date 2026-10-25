#!/usr/bin/env python3
"""Two independent AH-Net sessions; known service stays local, unknown passes.

Experimental transport relay, not a verified complete dLive state synchronizer.
"""
import argparse
import collections
import hashlib
import ipaddress
import json
import pathlib
import queue
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ahnet_lab import ITEMS, frame
from legacy_lab import ObservingBridge, decode_raw
from captured_rx import CapturedSubscriptions, DATA, is_telemetry, is_local_service
from surface_link import SurfaceMirror

ROOT = pathlib.Path(__file__).resolve().parent
VERSION = '0.6.6'

CATALOG_PARAMETERS = set()
for item in ITEMS.values():
    object_name = DATA['captured_registry'].get(hex(int(item['obj_a'], 16)))
    if object_name:
        CATALOG_PARAMETERS.add((object_name, int(item['target'], 16), item['n']))


class WireSplitter:
    """Reassemble F1 TCP frames, recognize E0/F0 envelopes, preserve other bytes.

E0/F0 are recognized session envelope families, never forwarded. Unknown
envelope semantics cannot be inferred from TCP recv chunk boundaries.
"""
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data, datagram=False):
        # The observed 18-byte keepalive response includes a 13-byte trailer.
        # Treating that trailer as opaque would leak service data to the peer.
        if datagram and len(data) == 18 and data.startswith(b'\xe0\x00\x01\x03\xe7'):
            return [('service', bytes(data))]
        self.buf.extend(data)
        out = []
        while self.buf:
            first = self.buf[0]
            if first == 0xf1:
                if len(self.buf) < 19:
                    break
                size = 20 + int.from_bytes(self.buf[15:19], 'big')
                if size <= 8*1024*1024+20:
                    if len(self.buf) < size:
                        break
                    if self.buf[size - 1] == 0xf8:
                        raw = bytes(self.buf[:size]); del self.buf[:size]
                        out.append(('f1', raw)); continue
                # Invalid F1 is unknown, not silently discarded.
                out.append(('opaque', bytes(self.buf[:1]))); del self.buf[:1]
            elif first in (0xe0, 0xf0):
                end = self.buf.find(0xe7 if first == 0xe0 else 0xf7, 1)
                if end < 0:
                    if len(self.buf) <= 65536:
                        break
                    # Corrupt/oversized recognized service envelope stays local.
                    out.append(('service', bytes(self.buf))); self.buf.clear(); break
                out.append(('service', bytes(self.buf[:end + 1]))); del self.buf[:end + 1]
            else:
                end = next((i for i in range(1, len(self.buf)) if self.buf[i] in (0xe0, 0xf0, 0xf1)), len(self.buf))
                out.append(('opaque', bytes(self.buf[:end]))); del self.buf[:end]
        if datagram and self.buf:
            # A truncated recognized session envelope remains local; other tails
            # are unknown and preserve their bytes within the original datagram.
            out.append(('service' if self.buf[0] in (0xe0, 0xf0) else 'opaque', bytes(self.buf)))
            self.buf.clear()
        return out


CATALOG_KEYS = {(int(x['obj_a'], 16), int(x['obj_b'], 16), int(x['target'], 16), x['n']) for x in ITEMS.values()}


def classify(raw, bridge):
    f = decode_raw(raw)
    a, b, target, op = f['a'], f['b'], f['target'], f['op']
    payload = bytes.fromhex(f['payload'])
    client = bridge._client_id
    captured = getattr(bridge, 'captured', None)
    if captured and client == a and captured.name_for(b):
        if is_local_service(captured.name_for(b), target):
            return 'service', f
        if is_telemetry(captured.name_for(b), target):
            return 'telemetry', f
        if target in (2, 6, 8, 23, 24, 27):
            return 'service', f
        if op == 3 and target >= 0x1000:
            return 'captured_parameter', f
    # These are actual session roles, not a blanket target>=0x1000 guess.
    local_objects = {x for x in (bridge._registered_object, bridge._server_object,
                                getattr(bridge, '_rx_mapper_object', None)) if x is not None}
    local_objects.update(getattr(bridge, '_probe_objects', set()))
    if client is not None and client in (a, b) and (a in local_objects or b in local_objects):
        return 'service', f
    if op in (2, 3) and (a in (0, 3) or b in (0, 3)) and target in (2, 4, 6, 8):
        return 'service', f
    if a in local_objects or b in local_objects:
        return 'service', f
    if op == 2 and (a, b, target, len(payload)) in CATALOG_KEYS:
        return 'command', f
    if op == 3 and (b, a, target, len(payload)) in CATALOG_KEYS:
        return 'notification', f
    return 'unknown', f


class RealBridge(ObservingBridge):
    def __init__(self, side, cfg, on_packet, log):
        super().__init__(side, cfg['ip'], cfg['tcp'], cfg['udp'], lambda *_: None, lambda *_: None)
        self.bind_ip = cfg.get('local_ip', '')
        self.disk_log_enabled = lambda:getattr(getattr(log,'__self__',None),'disk_logging',True)
        self.journal = self.journal.with_name('session_'+side+'_'+str(time.time_ns())+'.jsonl')
        self.model = cfg['model']; self.on_packet = on_packet; self.log = log
        self.splitter = WireSplitter()
        self.captured = None
        self.connection_error=None;self.unexpected_identity_reply=None

    def connect(self):
        self.connection_error=None;self.unexpected_identity_reply=None
        self.splitter = WireSplitter()
        return super().connect()

    def status(self):
        return dict(super().status(),connection_error=self.connection_error,
                    unexpected_identity_reply=self.unexpected_identity_reply)

    def send_f1_batch(self, frames):
        # Ordered native F1 frames; one write per object, no root lookup pipelining.
        if frames:self.send_raw(b''.join(frames),'tcp')

    def disconnect(self):
        if getattr(self, 'captured', None):
            self.captured.cancelled.set()
        return super().disconnect()

    def probe_request(self, profile='capture_input1'):
        if profile.startswith('capture_'):
            if self.captured is not None:
                raise RuntimeError('Переподключи MixRack перед повторной подпиской')
            self.captured = CapturedSubscriptions(self, profile)
            self.captured.start()
            return self.captured.status()
        return super().probe_request(profile)

    def probe_status(self):
        return self.captured.status() if self.captured else super().probe_status()

    def _handle_probe_frame(self, f):
        if self.captured:
            self.captured.observe(f)
        return super()._handle_probe_frame(f)

    def observe_raw_tcp(self, data, direction='incoming'):
        self.log('tcp_' + direction, self.side, hex=data.hex(' '), n=len(data))
        if direction != 'incoming':
            return
        parts = self.splitter.feed(data)
        # CDM32 identity must be tied to this session's discovery reply. The
        # legacy base recognizes DM64 text only; this adds the selected model.
        for kind, raw in parts:
            if kind != 'f1':
                continue
            f = decode_raw(raw)
            if (not self._identified.is_set() and f['op']==4 and f['b']==0 and f['target']==2 and
                    f['a'] in (3,self._client_id)):
                self.unexpected_identity_reply=f
                code=int.from_bytes(bytes.fromhex(f['payload']),'big')
                self.connection_error='MixRack ответил op=4 при регистрации; данные '+str(code)+' (назначение ответа не установлено)'
                self.log('unexpected_identity_reply',self.side,op=4,code=code,raw=f['raw'],error=self.connection_error)
            if (self._server_object is not None and f['op'] == 3 and
                    f['a'] == self._client_id and f['b'] == self._server_object and
                    f['target'] == 2 and self.model.encode('ascii') in bytes.fromhex(f['payload'])):
                with self.lock:
                    self._identified.set(); self._endpoint_state = 'identified'
        self.on_packet(self.side, 'tcp', parts, source=self)

    def _run_udp(self, gen, sock):
        marker = b'\xe0\x00\x01\x03\xe7'
        next_send = 0
        try:
            while not self._closing.is_set() and gen == self._generation:
                if time.monotonic() >= next_send:
                    sock.send(marker)
                    with self.lock:
                        self._udp_stats['to_dm64'] += 1; self._udp_stats['last_to'] = time.time()
                    self.log('udp_outgoing', self.side, hex=marker.hex(' '))
                    next_send = time.monotonic() + 1
                try:
                    data = sock.recv(65535)
                except socket.timeout:
                    continue
                with self.lock:
                    self._udp_stats['from_dm64'] += 1; self._udp_stats['last_from'] = time.time()
                self.log('udp_incoming', self.side, hex=data.hex(' '), n=len(data))
                parts = WireSplitter().feed(data, datagram=True)
                if self.captured:
                    for kind, raw in parts:
                        if kind == 'f1':
                            self.captured.observe(decode_raw(raw))
                self.on_packet(self.side, 'udp', parts, source=self)
        except OSError as ex:
            if not self._closing.is_set():
                self.log('udp_error', self.side, error=str(ex))
        finally:
            with self.lock:
                if self._udp_sock is sock:
                    self._udp_sock = None; self._udp_stats['running'] = False
            sock.close()

    def send_raw(self, data, transport):
        if not self.status()['connected']:
            raise RuntimeError('MixRack ' + self.side + ' disconnected')
        if transport == 'udp':
            with self.lock:
                sock = self._udp_sock
            if sock is None:
                raise RuntimeError('UDP disconnected')
            sock.send(data)
            self.log('udp_outgoing', self.side, hex=data.hex(' '), n=len(data))
        else:
            with self._tx_lock:
                sock = self._tx_sock
                if sock is None:
                    raise RuntimeError('TCP disconnected')
                self._send_tcp(sock, data)


DEFAULT_CONFIG = {
    'A': {'ip': '192.168.1.70', 'tcp': 51321, 'udp': 51327, 'model': 'DM64',
          'surface_ip': '192.168.1.71', 'surface_model': 'S5000', 'local_ip': '192.168.1.200'},
    'B': {'ip': '192.168.2.70', 'tcp': 51321, 'udp': 51327, 'model': 'DM64',
          'surface_ip': '192.168.2.71', 'surface_model': 'C3500', 'local_ip': '192.168.2.200'},
    'direction': 'both', 'convert_known_notifications': True,
    'probe': 'capture_all', 'echo_seconds': 5,
    'recovery_compact': False,
    'auto_redundancy': False, 'surface_observe': False, 'surface_banks': False, 'surface_experimental': False, 'scene_sync': False, 'surface_controls_observe': False,
}


FINAL_OPTIONS = dict(probe='capture_all', convert_known_notifications=True,
    auto_redundancy=True, recovery_compact=True, surface_observe=True,
    surface_banks=True, surface_experimental=True, surface_controls_observe=True,
    scene_sync=False)
SURFACE_MODELS = ('S7000', 'S5000', 'S3000', 'C3500', 'C2500', 'C1500', 'CTi1500')


def final_config(value, migrate=False):
    cfg = json.loads(json.dumps(value))
    if migrate and cfg.get('direction') == 'both':
        cfg['direction'] = 'A_to_B'
    if cfg.get('direction') not in ('A_to_B', 'B_to_A'):
        raise ValueError('Выбери направление 1 → 2 или 2 → 1')
    cfg.update(FINAL_OPTIONS)
    for side in ('A', 'B'):
        model = cfg[side].get('surface_model')
        if model not in SURFACE_MODELS:
            if migrate:
                cfg[side]['surface_model'] = DEFAULT_CONFIG[side]['surface_model']
            else:
                raise ValueError('Неверная модель Surface')
    return cfg


def validate_config(value):
    cfg = json.loads(json.dumps(value))
    for side in ('A', 'B'):
        x = cfg[side]
        x['ip'] = str(ipaddress.IPv4Address(x['ip'].strip()))
        for field in ('surface_ip', 'local_ip'):
            x[field] = x.get(field, '').strip()
            if x[field]:
                address = ipaddress.IPv4Address(x[field])
                if address.is_unspecified or address.is_multicast:
                    raise ValueError('Неверный IP: ' + field)
                x[field] = str(address)
        x['surface_model'] = str(x.get('surface_model', 'Surface'))[:40]
        addresses = [x[k] for k in ('ip', 'surface_ip', 'local_ip') if x[k]]
        if len(set(addresses)) != len(addresses):
            raise ValueError('В одной системе Surface, MixRack и MacBook должны иметь разные IP')
        if ipaddress.ip_address(x['ip']).is_unspecified or ipaddress.ip_address(x['ip']).is_multicast:
            raise ValueError('Нужен адрес конкретного MixRack')
        for field in ('tcp', 'udp'):
            x[field] = int(x[field])
            if not 1 <= x[field] <= 65535:
                raise ValueError('Неверный порт')
        for field, default in (('surface_tcp',51321),('surface_udp',51327)):
            x[field]=int(x.get(field,default))
            if not 1 <= x[field] <= 65535:
                raise ValueError('Неверный порт Surface')
        if x['model'] not in ('DM64', 'CDM32', 'DM32', 'DM48', 'CDM48', 'CDM64'):
            raise ValueError('Неверная модель MixRack')
    if (cfg['A']['ip'], cfg['A']['tcp']) == (cfg['B']['ip'], cfg['B']['tcp']):
        raise ValueError('A и B должны быть разными MixRack')
    if cfg['direction'] not in ('A_to_B', 'B_to_A', 'both'):
        raise ValueError('Неверное направление')
    if cfg['probe'] not in ('none', 'selector', 'mapper8', 'mapper32', 'capture_input1', 'capture_catalog', 'capture_all'):
        raise ValueError('Неверная подписка')
    cfg['echo_seconds'] = float(cfg.get('echo_seconds', 5))
    if not 0.1 <= cfg['echo_seconds'] <= 30:
        raise ValueError('Неверное окно антиэха')
    cfg['convert_known_notifications'] = bool(cfg['convert_known_notifications'])
    cfg['auto_redundancy']=bool(cfg.get('auto_redundancy',False))
    if cfg['auto_redundancy'] and (cfg['direction']=='both' or cfg['probe']!='capture_all' or not cfg.get('surface_observe')):
        raise ValueError('Авто резерв: нужны Surface, максимальный профиль и одно начальное направление')
    cfg['surface_observe'] = bool(cfg.get('surface_observe',False))
    cfg['surface_banks'] = bool(cfg.get('surface_banks',False))
    cfg['surface_experimental'] = bool(cfg.get('surface_experimental',False))
    cfg['surface_controls_observe'] = bool(cfg.get('surface_controls_observe',False))
    cfg['recovery_compact'] = bool(cfg.get('recovery_compact',False))
    # Scene/Show transfer development was discontinued by the operator.
    # Old configuration files cannot silently reactivate the experiment.
    cfg['scene_sync'] = False
    if (cfg['surface_banks'] or cfg['surface_experimental'] or cfg['scene_sync'] or cfg['surface_controls_observe']) and not cfg['surface_observe']:
        raise ValueError('Для передачи банков включи подключения к Surface')
    if cfg['surface_observe']:
        if not all(cfg[s]['surface_ip'] for s in ('A','B')):
            raise ValueError('Нужны IP обеих Surface')
        endpoints=[cfg[s][field] for s in ('A','B') for field in ('ip','surface_ip')]
        if len(set(endpoints)) != 4:
            raise ValueError('Для четырёх сессий нужны разные адреса обоих MixRack и Surface')
    return cfg


class DirectMirror:
    def __init__(self, config_path=None, bridge_factory=RealBridge, log_dir=None):
        self.config_path = pathlib.Path(config_path or ROOT / 'connection.json')
        self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if self.config_path.exists():
            self.cfg.update(json.loads(self.config_path.read_text(encoding='utf-8')))
        self.cfg = final_config(self.cfg, migrate=True)
        log_dir = pathlib.Path(log_dir or ROOT / 'logs'); log_dir.mkdir(exist_ok=True, parents=True)
        self.log_path = log_dir / ('direct_' + time.strftime('%Y%m%d_%H%M%S') + '_' + secrets.token_hex(3) + '.jsonl')
        self.disk_logging = True
        self.log_cleanup = dict(count=0,deleted_files=0,last_reason=None,error=None)
        self.events = collections.deque(maxlen=100); self.counts = collections.Counter()
        self.log_lock = threading.RLock(); self.gate = threading.RLock(); self.control = threading.Lock()
        self.bridges = {}; self.factory = bridge_factory; self.running = False; self.epoch = 0
        self.expected = {}; self.recent = {}; self.log_error = None
        self.started_at = None; self.last_sides = {};self.global_seen={};self.global_poll_last={}
        self.surfaces = SurfaceMirror(self.event)
        from scene_sync import SceneSync
        self.scene_sync=SceneSync(self)
        self.surfaces.on_scene=self.scene_sync.updated
        from redundancy import Redundancy
        self.redundancy=Redundancy(self)
        self.surfaces.on_frame=lambda side,f,source:(self.redundancy.observe_reply(side,'surface',source,f) or self.scene_sync.observe(side,'surface',source,f))
        self.surfaces.recovery_handler=self.redundancy.surface
        self.unrecorded = collections.Counter()
        self.jobs = queue.Queue(maxsize=4096); self.closed = threading.Event()
        self.worker = threading.Thread(target=self._worker, daemon=True); self.worker.start()

    def event(self, kind, side=None, **fields):
        with self.log_lock:
            item = dict(t=time.time(), type=kind, side=side, **fields)
            self.events.append(item); self.counts[kind] += 1
            if not self.disk_logging:return
            try:
                with self.log_path.open('a', encoding='utf-8') as out:
                    out.write(json.dumps(item, ensure_ascii=False) + '\n')
            except OSError as ex:
                self.log_error = str(ex)

    def connect(self, cfg):
        cfg = validate_config(cfg)
        with self.control:
            self.disconnect()
            self.disk_logging = True
            self.cfg = cfg
            tmp = self.config_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(self.config_path)
            for side in ('A', 'B'):
                self.bridges[side] = self.factory(side, cfg[side], self.packet, self.event)
            results = {}
            # Connect independently: a failed side does not hide the other status.
            for side, bridge in self.bridges.items():
                try:
                    bridge.serve(); results[side] = bridge.connect()
                    if cfg['probe'] != 'none' and bridge.status()['connected']:
                        bridge.probe_request(cfg['probe'])
                except (OSError, RuntimeError, ValueError) as ex:
                    results[side] = {'error': str(ex)}
                    self.event('connect_error', side, error=str(ex))
            if cfg['surface_observe']:
                results['surfaces'] = self.surfaces.connect(cfg)
            return results

    def start(self):
        with self.control, self.gate:
            if self.running:
                return {'running': True}
            self.global_seen={s:b.probe_status().get('last_global_period_ms') for s,b in self.bridges.items() if b.probe_status().get('last_global_period_ms') is not None}
            if self.cfg.get('auto_redundancy'):
                self.redundancy.start();self.epoch+=1;self.running=True;self.started_at=time.time()
                self.event('started_redundancy',master=self.redundancy.master);return {'running':True,'master':self.redundancy.master}
            if len(self.bridges) != 2 or not all(b.status()['connected'] for b in self.bridges.values()):
                raise RuntimeError('Сначала подключи оба MixRack')
            if self.cfg['probe'].startswith('capture_'):
                if any(b.probe_status()['phase'] not in ('requests_sent', 'partial_requests_sent') for b in self.bridges.values()):
                    raise RuntimeError('Дождись завершения запросов подписки на обеих сторонах')
            if self.cfg.get('scene_sync'):self.scene_sync.require_ready()
            if self.cfg['surface_banks'] or self.cfg.get('surface_experimental') or self.cfg.get('scene_sync'):
                self.surfaces.start()
            self.epoch += 1; self.expected.clear(); self.recent.clear(); self.running = True
            self.started_at = time.time()
            self.event('started', direction=self.cfg['direction'])
            return {'running': True, 'direction': self.cfg['direction']}

    def stop(self):
        with self.gate:
            self.redundancy.stop()
            self.scene_sync.stop()
            self.surfaces.stop()
            self.running = False; self.epoch += 1
            self.expected.clear(); self.recent.clear()
            self.event('stopped')

    def clear_log_files(self, reason):
        # Call after fencing queue work. Only application-owned log names;
        # configuration, user exports and unrelated files are never targets.
        with self.log_lock:
            self.disk_logging = False
            roots={self.log_path.parent, ROOT/'logs'}
            deleted=0;errors=[]
            for root in roots:
                for pattern in ('direct_*.jsonl','session_*.jsonl','mirror_*.jsonl',
                                'recovery_*.sqlite3','recovery_*.sqlite3-wal',
                                'recovery_*.sqlite3-shm','scene_backup_*.json',
                                'recovery_scene_backup_*.json'):
                    for path in root.glob(pattern):
                        try:
                            path.unlink(missing_ok=True);deleted+=1
                        except OSError as ex:errors.append(str(ex))
            self.log_cleanup.update(count=self.log_cleanup['count']+1,
                deleted_files=self.log_cleanup['deleted_files']+deleted,
                last_reason=reason,error='; '.join(errors) or None)
            if errors:self.log_error='Очистка логов: '+'; '.join(errors)
            return not errors

    def disconnect(self):
        self.stop()
        self.surfaces.disconnect()
        self.scene_sync.reset()
        for bridge in list(self.bridges.values()):
            self.last_sides[bridge.side] = dict(bridge.status(), probe=bridge.probe_status(), ip=bridge.target,
                                               tcp=bridge.port, udp_port=bridge.udp_port)
            bridge.disk_log_enabled=lambda:False
            bridge.disconnect()
        self.redundancy.purge_logs(manual=True)
        self.bridges.clear()

    def packet(self, side, transport, parts, source=None):
        if not parts:
            return
        with self.gate:
            bridge = self.bridges.get(side)
            if bridge is None:
                return
            if source is not None and source is not bridge:
                return
            parts=[(kind,raw) for kind,raw in parts if not (kind=='f1' and (self.redundancy.observe_reply(side,'rack',bridge,decode_raw(raw)) or self.scene_sync.observe(side,'rack',bridge,decode_raw(raw))))]
            if not parts:return
            self.event('received', side, transport=transport, parts=len(parts))
            if not self.running:
                return
            if self.cfg.get('auto_redundancy'):
                try:self.redundancy.rack(side,parts,bridge,transport)
                except Exception as ex:self.redundancy._fault(str(ex))
                return
            generations = tuple(self.bridges[s]._generation for s in ('A', 'B'))
            try:
                self.jobs.put_nowait((self.epoch, generations, side, transport, parts))
            except queue.Full:
                # Continuing with holes in a command stream would be misleading.
                self.running = False; self.epoch += 1
                self.surfaces.stop()
                self.event('queue_overflow_stopped', side)

    def _worker(self):
        while not self.closed.is_set():
            try:
                job = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                self._route(job)
            except Exception as ex:
                with self.gate:
                    self.running = False; self.epoch += 1
                    self.surfaces.stop()
                self.event('relay_error_stopped', error=str(ex))
            finally:
                self.jobs.task_done()

    @staticmethod
    def signature(kind, raw, f=None):
        if f is not None:
            # Writes and their reversed op3 echoes identify the same value.
            return ('f1', min(f['a'], f['b']), max(f['a'], f['b']), f['target'], f['payload'])
        return (kind, hashlib.sha256(raw).digest())

    def poll_global_tempo(self):
        # 0x08 is the captured read-current request. Polling also covers firmware
        # that does not broadcast global-clock changes to this subscriber.
        with self.gate:
            if not self.running:return
            sides=([self.redundancy.master] if self.cfg.get('auto_redundancy') else
                   ['A'] if self.cfg['direction']=='A_to_B' else ['B'] if self.cfg['direction']=='B_to_A' else ['A','B'])
            for side in sides:
                if self.cfg.get('auto_redundancy') and not self.redundancy._ready(side):continue
                b=self.bridges.get(side)
                if not b or not b.status()['connected'] or not getattr(b,'captured',None):continue
                lookup=getattr(b.captured,'writable_id_for',None)
                obj=lookup('FX Global Tap Tempo') if lookup else None
                if not obj:continue
                last=self.global_poll_last.get(side)
                if last and last[0] is b and time.monotonic()-last[1]<.24:continue
                self.global_poll_last[side]=(b,time.monotonic())
                try:b.send_raw(frame(2,obj,b._client_id,8,b''),'tcp')
                except (OSError,RuntimeError) as ex:self.event('global_tempo_poll_error',side,error=str(ex))

    def _route(self, job):
        epoch, generations, side, transport, parts = job
        dest = 'B' if side == 'A' else 'A'
        with self.gate:
            if not self.running or epoch != self.epoch:
                return
            if tuple(self.bridges[s]._generation for s in ('A', 'B')) != generations:
                self.stop(); self.event('generation_changed_stopped'); return
            if not all(b.status()['connected'] for b in self.bridges.values()):
                self.stop(); self.event('connection_lost_stopped'); return
            direction = self.cfg['direction']
            if (side == 'A' and direction == 'B_to_A') or (side == 'B' and direction == 'A_to_B'):
                return
            now = time.monotonic()
            self.expected = {k: v for k, v in self.expected.items() if v > now}
            self.recent = {k: v for k, v in self.recent.items() if v[1] > now}
            outgoing = []
            for kind, raw in parts:
                f = None
                if kind == 'service':
                    self.event('service_blocked', side, transport=transport, hex=raw.hex(' ')); continue
                label = 'unknown'
                if kind == 'f1':
                    label, f = classify(raw, self.bridges[side])
                    from safety_filter import phantom,object_name
                    cp=self.bridges[side].captured
                    pname=object_name(cp,f)
                    value=bytes.fromhex(f['payload'])
                    if phantom(pname,f['target'],value) or (not pname and f['target']==0x1003 and len(value)==1):
                        self.event('phantom_blocked',side,name=pname,target=hex(f['target']));continue
                    if (self.bridges[side].captured and
                            object_name(self.bridges[side].captured,f) in ('StageBox Scene Manager','Last Show Recalled Manager')):
                        self.event('scene_status_local',side);continue
                    if label == 'service':
                        self.event('service_blocked', side, transport=transport, hex=raw.hex(' ')); continue
                    if label == 'telemetry':
                        self.event('telemetry_blocked', side, transport=transport, target=hex(f['target']), n=len(bytes.fromhex(f['payload']))); continue
                captured_name = None
                if label == 'captured_parameter':
                    captured_name = self.bridges[side].captured.name_for(f['b'])
                    sig = ('captured', captured_name, f['target'], f['payload'])
                else:
                    sig = self.signature(kind, raw, f)
                if captured_name=='FX Global Tap Tempo' and f['target']==0x1000 and len(bytes.fromhex(f['payload']))==2:
                    period=int.from_bytes(bytes.fromhex(f['payload']),'big')
                    previous=self.global_seen.get(side);self.global_seen[side]=period
                    if previous is None or previous==period:continue
                    if self.expected.get((side,sig),0)<=now:
                        for old in list(self.expected):
                            if old[0]==side and old[1][:-1]==sig[:-1]:self.expected.pop(old,None)
                if self.expected.get((side, sig), 0) > now:
                    self.event('echo_suppressed', side, transport=transport); continue
                address_key = sig[:-1] if f is not None else sig
                recent_key = (side, address_key)
                previous = self.recent.get(recent_key)
                if previous and previous[0] == sig and previous[1] > now:
                    self.event('duplicate_suppressed', side, transport=transport); continue
                # Short duplicate window; differing values are never coalesced.
                self.recent[recent_key] = (sig, now + .05)
                if label == 'captured_parameter' and (captured_name, f['target'], len(bytes.fromhex(f['payload']))) not in CATALOG_PARAMETERS:
                    key = (captured_name, f['target'], len(bytes.fromhex(f['payload'])))
                    if len(self.unrecorded) < 2000 or key in self.unrecorded:
                        self.unrecorded[key] += 1
                    self.event('unrecorded_parameter_forwarding', side, name=captured_name,
                               target=hex(f['target']), payload_bytes=key[2])
                if label == 'notification' and self.cfg['convert_known_notifications']:
                    raw = frame(2, f['b'], f['a'], f['target'], bytes.fromhex(f['payload']))
                    self.event('known_notification_to_write', side, target=hex(f['target']))
                elif label == 'captured_parameter' and self.cfg['convert_known_notifications']:
                    target_bridge = self.bridges[dest]
                    target_capture = getattr(target_bridge, 'captured', None)
                    target_obj = target_capture.id_for(captured_name) if target_capture else None
                    if target_obj:
                        raw = frame(2, target_obj, target_bridge._client_id, f['target'], bytes.fromhex(f['payload']))
                        self.event('object_readdressed', side, name=captured_name,
                                   source_object=f['b'], destination_object=target_obj, target=hex(f['target']))
                    else:
                        self.event('mapping_unavailable_passthrough', side, name=captured_name)
                if captured_name=='FX Global Tap Tempo' and f['target']==0x1000:
                    # Only the latest injected period may be an echo. Keeping
                    # earlier periods would suppress a real operator reverting BPM.
                    for old in list(self.expected):
                        if old[0]==dest and old[1][:-1]==sig[:-1]:self.expected.pop(old,None)
                self.expected[(dest, sig)] = now + self.cfg['echo_seconds']
                outgoing.append(raw)
                self.event('forward_' + label, side, destination=dest, transport=transport, hex=raw.hex(' '))
            if outgoing:
                # Filter UDP contents, retain one datagram and original order.
                data = b''.join(outgoing)
                self.bridges[dest].send_raw(data, transport)
                self.event('sent_' + side + '_to_' + dest, side, transport=transport, n=len(data), parts=len(outgoing))

    def status(self):
        with self.gate:
            surface_status=self.surfaces.status()
            sides = {s: dict(b.status(), ip=b.target, tcp=b.port, udp_port=b.udp_port,
                             probe=b.probe_status()) for s, b in list(self.bridges.items())}
            # Detect idle disconnects even when no command arrives.
            if self.running and not self.cfg.get('auto_redundancy') and not all(b['connected'] for b in sides.values()):
                self.stop(); self.event('connection_lost_stopped')
            with self.log_lock:
                return dict(version=VERSION, running=self.running, config=self.cfg, sides=sides,
                            surfaces=surface_status,scene_sync=self.scene_sync.status(),redundancy=self.redundancy.status(),
                            last_sides=self.last_sides, started_at=self.started_at,
                            coverage={'source':'named objects and request descriptors from capture 001; no live namespace enumeration',
                                      'available_templates':len(DATA['objects']),
                                      'unrecorded_parameters':[{'name':k[0],'target':hex(k[1]),'payload_bytes':k[2],'events':v} for k,v in self.unrecorded.most_common(100)],
                                      'unknown_parameter_allowlist':False},
                            queue=self.jobs.qsize(), counts=dict(self.counts), events=list(self.events),
                            log_error=self.log_error, log_path=str(self.log_path),
                            disk_logging=self.disk_logging,log_cleanup=dict(self.log_cleanup),
                            unknown_policy='unmapped_bytes_passthrough; discovered_parameter_headers_remapped_when_enabled', hardware_verified=False)

    def close(self):
        self.disconnect(); self.redundancy.close(); self.scene_sync.close(); self.closed.set(); self.worker.join(timeout=2)


ENGINE = None
TOKEN = secrets.token_urlsafe(24)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, obj, status=200, content_type='application/json; charset=utf-8'):
        blob = json.dumps(obj, ensure_ascii=False).encode() if isinstance(obj, (dict, list)) else obj
        if isinstance(blob, str):
            blob = blob.encode()
        self.send_response(status); self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(blob))); self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff'); self.end_headers(); self.wfile.write(blob)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/':
            return self.send((ROOT / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', TOKEN), content_type='text/html; charset=utf-8')
        if path in ('/api/status', '/api/diagnostics'):
            return self.send(ENGINE.status())
        if path == '/api/traffic':
            with ENGINE.log_lock:
                data = ENGINE.log_path.read_bytes() if ENGINE.log_path.exists() else b''
            return self.send(data, content_type='application/x-ndjson')
        return self.send({'error': 'Not found'}, 404)

    def do_POST(self):
        if self.headers.get('X-Mirror-Token') != TOKEN:
            return self.send({'error': 'Request token mismatch'}, 403)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 16384:
                raise ValueError('Request too large')
            data = json.loads(self.rfile.read(size) or b'{}')
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/connect':
                result = ENGINE.connect(final_config(data))
            elif path == '/api/start':
                result = ENGINE.start()
            elif path == '/api/stop':
                ENGINE.stop(); result = {'running': False}
            elif path == '/api/disconnect':
                with ENGINE.control:
                    ENGINE.disconnect()
                result = {'disconnected': True}
            elif path == '/api/mark':
                label=str(data.get('label',''))[:120]
                ENGINE.event('operator_marker', label=label)
                result={'label':label}
            else:
                return self.send({'error': 'Not found'}, 404)
            return self.send({'ok': True, 'result': result})
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as ex:
            return self.send({'ok': False, 'error': str(ex)}, 400)


def main():
    global ENGINE
    parser = argparse.ArgumentParser()
    parser.add_argument('--ui-port', type=int, default=8780)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--config', default=str(ROOT / 'connection.json'))
    args = parser.parse_args()
    ENGINE = DirectMirror(args.config)
    server = ThreadingHTTPServer(('127.0.0.1', args.ui_port), Handler)
    url = f'http://127.0.0.1:{server.server_port}/'
    print('Allen&heath Dlive v2.12 Full Mirroring', '| build', VERSION, url, '| Known service local; unknown bytes pass through')
    if not args.no_browser:
        import webbrowser
        threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close(); ENGINE.close()


if __name__ == '__main__':
    main()
