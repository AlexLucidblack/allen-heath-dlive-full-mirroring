"""Observed object subscriptions from the user's initialization capture.

This replays request templates, not a verified implementation of all AH-Net
semantics. Discovery is serialized so identical assignment replies have an
unambiguous pending name. No captured Object ID is assumed valid at runtime.
"""
import json
import pathlib
import threading
import time

from ahnet_lab import frame

DATA = json.loads((pathlib.Path(__file__).parent / 'capture_subscriptions.json').read_text(encoding='utf-8'))
TEMPO=json.loads((pathlib.Path(__file__).parent/'global_tempo_subscription.json').read_text(encoding='utf-8'))
DATA['objects'].append(TEMPO)


def is_telemetry(name, target):
    # Exact object/target pairs only: metering readback 0x1000/0x1001 was
    # confirmed in the 2026-10-22 incident log (1280/1024-byte UDP arrays).
    # Never turn these level arrays into op=2 writes on the other rack.
    # RTA controls and unknown targets/objects deliberately remain eligible.
    return ((name == 'RTA Control Manager' and target in (0x1009, 0x100a)) or
            (name == 'Metering Sources' and 0x1000 <= target <= 0x1008))


def is_local_service(name, target):
    # This is an event stream, not a writable audio parameter. Sending it as
    # op=2 produces another "Unknown Method" event and a cross-rack loop.
    return name == 'Event Log Server' and target == 0x1000


class CapturedSubscriptions:
    def __init__(self, bridge, profile, specs=None, lookup_timeout=8):
        self.bridge = bridge; self.profile = profile
        candidates = DATA['objects'] if specs is None else specs
        if profile == 'capture_input1':
            candidates = [s for s in candidates if s['name'] == 'Input Mixer Module, Number 01']
            # First logical descriptor from the captured 36-descriptor packet,
            # plus its observed 0x18 request. This isolates the 0x1280 group.
            candidates = [{**s, 'requests': [
                {'target': 23, 'payload': bytes.fromhex(next(r['payload'] for r in s['requests'] if r['target'] == 23))[:21].hex(' ')},
                next(r for r in s['requests'] if r['target'] == 24)]} for s in candidates]
        elif profile == 'capture_catalog':
            candidates = [s for s in candidates if s.get('selected')]
        priority=list(getattr(bridge,'recovery_priority_names',()))
        if priority:
            rank={name:i for i,name in enumerate(priority)}
            candidates=sorted(candidates,key=lambda spec:rank.get(spec['name'],len(rank)))
        self.specs = candidates
        self.lookup_timeout = lookup_timeout
        self.lock = threading.RLock(); self.cancelled = threading.Event(); self.reply = threading.Event()
        self.pending = None; self.assigned = None
        self.by_name = {}; self.by_id = {};self.write_ready=set()
        self.state = dict(phase='idle', profile=profile, name='Captured object subscriptions',
                          ack_count=0, registrations_sent=0, registration_batches=0, expected_acks=0, errors=[],
                          objects_total=len(candidates), objects_resolved=0, objects_done=0,
                          parameter_events=0, initial_snapshots=0, last_parameter_at=None,
                          telemetry_events=0, service_events=0, unavailable_objects=[],
                          skipped_subscriptions=[],
                          first_parameter=None, source=DATA['source'], hardware_verified=False)
        self.state.update(global_tempo_events=0,last_global_period_ms=None)
        self.state['priority_objects']=len(priority)

    def start(self):
        with self.lock:
            self.state['phase'] = 'discovering'; self.state['started_at'] = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True, name='captured-rx-' + self.bridge.side)
        self.thread.start()

    def status(self):
        with self.lock:
            return {**self.state, 'errors': list(self.state['errors']),
                    'skipped_subscriptions': list(self.state['skipped_subscriptions']),
                    'object_ids': dict(self.by_name),
                    'global_tempo':dict(object_id=self.by_name.get(TEMPO['name']),
                                        requests_sent=TEMPO['name'] in self.write_ready,
                                        incoming_events=self.state['global_tempo_events'],
                                        refresh_read_interval_ms=250,
                                        last_received_period_ms=self.state['last_global_period_ms'])}

    def observe(self, f):
        payload = bytes.fromhex(f['payload'])
        with self.lock:
            if (self.pending is not None and f['op'] == 3 and f['a'] == 3 and f['b'] == 0 and
                    f['target'] == 2 and len(payload) == 4):
                self.assigned = int.from_bytes(payload, 'big')
                if 0 < self.assigned < 65536:
                    self.by_name[self.pending] = self.assigned
                    self.by_id[self.assigned] = self.pending
                    self.state['objects_resolved'] += 1
                self.pending = None; self.reply.set()
                return
            if f['op'] != 3 or f['a'] != self.bridge._client_id or f['b'] not in self.by_id:
                return
            if f['target'] == 24:
                self.state['ack_count'] += 1
            elif f['target'] == 8:
                self.state['ack_count'] += 1
            elif f['target'] == 27:
                self.state['initial_snapshots'] += 1
            elif is_local_service(self.by_id[f['b']], f['target']):
                self.state['service_events'] += 1
            elif is_telemetry(self.by_id[f['b']], f['target']):
                self.state['telemetry_events'] += 1
            elif f['target'] >= 0x1000:
                self.state['parameter_events'] += 1; self.state['last_parameter_at'] = time.time()
                if self.by_id[f['b']]==TEMPO['name'] and f['target']==0x1000 and len(payload)==2:
                    self.state['global_tempo_events']+=1;self.state['last_global_period_ms']=int.from_bytes(payload,'big')
                self.state['first_parameter'] = self.state['first_parameter'] or dict(
                    name=self.by_id[f['b']], target=hex(f['target']), payload=f['payload'])

    def name_for(self, obj):
        with self.lock:
            return self.by_id.get(obj)

    def id_for(self, name):
        with self.lock:
            return self.by_name.get(name)

    def writable_id_for(self,name):
        with self.lock:
            return self.by_name.get(name) if name in self.write_ready else None

    def _run(self):
        generation = self.bridge._generation
        try:
            for spec in self.specs:
                if self.cancelled.is_set() or generation != self.bridge._generation:
                    with self.lock:
                        self.state['phase'] = 'cancelled'
                    return
                name = spec['name']
                with self.lock:
                    self.state.update(phase='discovering', current_object=name)
                    self.pending = name; self.assigned = None; self.reply.clear()
                self.bridge.send_f1(frame(2, 0, 3, 4, (name + '\0').encode('ascii')))
                deadline = time.monotonic() + self.lookup_timeout
                while not self.reply.wait(.05):
                    if self.cancelled.is_set() or generation != self.bridge._generation:
                        with self.lock:
                            self.state['phase'] = 'cancelled'
                        return
                    if time.monotonic() >= deadline:
                        # Never issue another lookup after timeout: a late reply
                        # could be incorrectly bound to the next object's name.
                        raise RuntimeError('Object discovery timeout: ' + name)
                with self.lock:
                    obj = self.assigned
                    self.state['phase'] = 'registering'
                if not obj or obj >= 65536:
                    self.bridge.log('captured_object_unavailable', self.bridge.side, name=name)
                    with self.lock:
                        self.state['objects_done'] += 1
                        self.state['unavailable_objects'].append(name)
                    continue
                if name == 'Metering Sources':
                    # We mirror commands; the Surface receives its levels from
                    # its own MixRack. Do not request the nine continuous meter
                    # streams. Keep name/ID discovery so stray readback is still
                    # recognized by the routing filter. Global Metering Sources
                    # is a separate object and is deliberately unaffected.
                    self.bridge.log('captured_subscription_skipped', self.bridge.side,
                                    name=name, reason='native metering stays on each console')
                    with self.lock:
                        self.state['skipped_subscriptions'].append(name)
                        self.state['objects_done'] += 1
                    continue
                batch=[]
                for request in spec['requests']:
                    if self.cancelled.is_set() or generation != self.bridge._generation:
                        with self.lock:
                            self.state['phase'] = 'cancelled'
                        return
                    target = request['target']; payload = bytes.fromhex(request['payload'])
                    if target in (6,23):
                        payload = payload[:4] + self.bridge._client_id.to_bytes(2, 'big') + payload[6:]
                        recipient = 3 if target==6 else 0xffffffff
                    else:
                        recipient = self.bridge._client_id
                    batch.append(frame(2, obj, recipient, target, payload))
                sender=getattr(self.bridge,'send_f1_batch',None)
                if sender:sender(batch)
                else:
                    for raw in batch:self.bridge.send_f1(raw)
                with self.lock:
                    self.state['registrations_sent']+=len(batch)
                    self.state['expected_acks']+=sum(r['target'] in (8,24) for r in spec['requests'])
                    self.state['registration_batches']+=1
                    self.state['objects_done'] += 1
                    self.write_ready.add(name)
            with self.lock:
                self.state['phase'] = 'requests_sent'; self.state['finished_at'] = time.time()
        except (OSError, RuntimeError, ValueError) as ex:
            with self.lock:
                self.pending = None
                self.state['phase'] = 'partial_requests_sent' if self.state['objects_done'] else 'error'
                self.state['errors'].append(str(ex))
            self.bridge.log('captured_subscription_error', self.bridge.side, error=str(ex))
