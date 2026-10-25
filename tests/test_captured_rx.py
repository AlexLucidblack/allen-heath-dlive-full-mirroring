import copy
import pathlib
import tempfile
import unittest

from ahnet_lab import frame
from captured_rx import DATA, CapturedSubscriptions
from mirror_app import DirectMirror, DEFAULT_CONFIG
from test_direct import CapturingRack, wait_for


class ObjectRack(CapturingRack):
    """Synthetic object server with deliberately different IDs on each rack.

This verifies addressing and transport logic, not the hardware's interpretation
of the captured requests.
"""
    def __init__(self, obj):
        self.obj = obj; self.subscriber = None; self.bulk_requests = []
        super().__init__('DM64')

    def _process(self, conn, f):
        payload = bytes.fromhex(f['payload'])
        if f['target'] == 4 and payload == b'Input Mixer Module, Number 01\0':
            self._send(conn, frame(3, 3, 0, 2, self.obj.to_bytes(4, 'big')))
            return
        if f['a'] == self.obj and f['target'] == 23:
            self.subscriber = int.from_bytes(payload[4:6], 'big')
            self.bulk_requests.append(f)
            return
        if f['a'] == self.obj and f['b'] == self.subscriber and f['target'] == 24:
            self.bulk_requests.append(f)
            self._send(conn, frame(3, self.subscriber, self.obj, 24, payload))
            self._send(conn, frame(3, self.subscriber, self.obj, 27, b'initial snapshot'))
            return
        return super()._process(conn, f)

    def operator(self, target, payload):
        self._broadcast(frame(3, self.subscriber, self.obj, target, payload))


class CapturedRXTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = ObjectRack(0x130); self.b = ObjectRack(0x230)
        self.a.start(); self.b.start()
        self.engine = DirectMirror(pathlib.Path(self.tmp.name) / 'connection.json', log_dir=self.tmp.name)
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        for side, rack, local in [('A', self.a, '127.0.0.2'), ('B', self.b, '127.0.0.3')]:
            cfg[side].update(ip='127.0.0.1', tcp=rack.port, udp=rack.udp_port,
                             model='DM64', local_ip=local, surface_ip='')
        cfg.update(probe='capture_input1', direction='both')
        self.engine.connect(cfg)
        wait_for(lambda: all(b.probe_status()['phase'] == 'requests_sent' and
                             b.probe_status()['initial_snapshots'] for b in self.engine.bridges.values()))

    def tearDown(self):
        self.engine.close(); self.a.stop(); self.b.stop(); self.tmp.cleanup()

    def test_real_capture_descriptor_is_used_with_fresh_subscriber(self):
        spec = next(s for s in DATA['objects'] if s['name'] == 'Input Mixer Module, Number 01')
        original = bytes.fromhex(next(r['payload'] for r in spec['requests'] if r['target'] == 23))[:21]
        self.assertEqual(original.hex(' '), '00 00 00 00 00 2e 01 00 00 12 80 00 00 12 80 00 00 00 30 00 ff')
        for side, rack in [('A', self.a), ('B', self.b)]:
            self.assertEqual([r['target'] for r in rack.bulk_requests], [23, 24])
            payload = bytes.fromhex(rack.bulk_requests[0]['payload'])
            self.assertEqual(payload[6:], original[6:])
            self.assertEqual(int.from_bytes(payload[4:6], 'big'), self.engine.bridges[side]._client_id)
            self.assertEqual(rack.bulk_requests[0]['a'], rack.obj)
            self.assertEqual(rack.bulk_requests[1]['b'], rack.subscriber)
            self.assertEqual(rack.bulk_requests[1]['payload'], '00 00 00 30')

    def test_fader_capture_value_readdresses_and_echo_stops(self):
        self.engine.start()
        # First observed value in capture #002, with live session addresses.
        self.a.operator(0x1280, bytes.fromhex('6c 52'))
        wait_for(lambda: any(f['target'] == 0x1280 for f in self.b.received))
        out = next(f for f in self.b.received if f['target'] == 0x1280)
        self.assertEqual((out['op'], out['a'], out['b'], out['payload']),
                         (2, self.b.obj, self.b.subscriber, '6c 52'))
        wait_for(lambda: self.engine.status()['counts'].get('echo_suppressed', 0) > 0)
        self.assertFalse(any(f['target'] == 0x1280 for f in self.a.received))

    def test_mute_and_unknown_parameter_payload_are_readdressed(self):
        self.engine.start()
        self.b.operator(0x1298, b'\x01')
        self.b.operator(0x6789, b'\x00\xaa\x10')
        wait_for(lambda: any(f['target'] == 0x6789 for f in self.a.received))
        for target, value in [(0x1298, '01'), (0x6789, '00 aa 10')]:
            out = next(f for f in self.a.received if f['target'] == target)
            self.assertEqual((out['op'], out['a'], out['b'], out['payload']),
                             (2, self.a.obj, self.a.subscriber, value))

    def test_initial_snapshots_stay_local_and_last_status_survives_disconnect(self):
        self.engine.start()
        self.a._broadcast(frame(3, self.a.subscriber, self.a.obj, 27, b'initial snapshot'))
        wait_for(lambda: self.engine.status()['counts'].get('service_blocked', 0) > 0)
        self.assertFalse(any(f['target'] == 27 for f in self.b.received))
        self.engine.disconnect()
        status = self.engine.status()
        self.assertFalse(status['sides'])
        self.assertEqual(status['last_sides']['A']['probe']['object_ids']['Input Mixer Module, Number 01'], self.a.obj)

    def test_udp_parameter_is_readdressed_and_counted(self):
        self.engine.start()
        a_bridge = self.engine.bridges['A']
        self.a.udp.sendto(frame(3, self.a.subscriber, self.a.obj, 0x1280, b'\x81\x00'),
                          ('127.0.0.2', a_bridge._udp_local_port))
        wait_for(lambda: any(d.startswith(b'\xf1') for d in self.b.datagrams))
        from legacy_lab import decode_raw
        f = decode_raw(next(d for d in self.b.datagrams if d.startswith(b'\xf1')))
        self.assertEqual((f['op'], f['a'], f['b']), (2, self.b.obj, self.b.subscriber))
        self.assertGreater(a_bridge.probe_status()['parameter_events'], 0)

    def test_rta_spectrum_stays_local_but_rta_controls_pass(self):
        for side, rack in [('A', self.a), ('B', self.b)]:
            captured = self.engine.bridges[side].captured
            with captured.lock:
                captured.by_id[rack.obj] = 'RTA Control Manager'
                captured.by_name['RTA Control Manager'] = rack.obj
        self.engine.start()
        self.a.operator(0x1009, b'\x20\x00' * 24)
        self.a.operator(0x100a, b'\x25\xb1' * 24)
        wait_for(lambda: self.engine.status()['counts'].get('telemetry_blocked', 0) >= 2)
        self.assertFalse(any(f['target'] in (0x1009, 0x100a) for f in self.b.received))
        self.a.operator(0x100c, b'\x01')
        wait_for(lambda: any(f['target'] == 0x100c for f in self.b.received))
        self.assertEqual(self.engine.bridges['A'].probe_status()['telemetry_events'], 2)

    def test_unrecorded_address_and_payload_are_forwarded_without_samples(self):
        self.engine.start()
        value = bytes.fromhex('de ad be ef 11 22 33 44')
        self.a.operator(0x55aa, value)
        wait_for(lambda: any(f['target'] == 0x55aa for f in self.b.received))
        out = next(f for f in self.b.received if f['target'] == 0x55aa)
        self.assertEqual(bytes.fromhex(out['payload']), value)
        status = self.engine.status()
        self.assertFalse(status['coverage']['unknown_parameter_allowlist'])
        self.assertTrue(any(x['target'] == '0x55aa' for x in status['coverage']['unrecorded_parameters']))

    def test_event_log_cannot_start_unknown_method_feedback_loop(self):
        event_objects = {}
        invalid_writes = []
        for side, rack in [('A', self.a), ('B', self.b)]:
            obj = rack.obj + 2; event_objects[side] = obj
            captured = self.engine.bridges[side].captured
            with captured.lock:
                captured.by_id[obj] = 'Event Log Server'
                captured.by_name['Event Log Server'] = obj
            # Reproduce hardware behavior: every invalid log write generates
            # a different error event, which defeats byte-based echo detection.
            original = rack._process
            def process(conn, f, rack=rack, obj=obj, original=original):
                if f['op'] == 2 and f['a'] == obj and f['target'] == 0x1000:
                    invalid_writes.append(f)
                    rack._send(conn, frame(3, rack.subscriber, obj, 0x1000,
                               ('ERROR: Unknown Method: 0x1000 #' + str(len(invalid_writes))).encode()))
                    return
                original(conn, f)
            rack._process = process
        self.engine.start()
        for side, rack, local in [('A', self.a, '127.0.0.2'), ('B', self.b, '127.0.0.3')]:
            event = frame(3, rack.subscriber, event_objects[side], 0x1000, b'CSP: 0-0-0\0')
            rack._broadcast(event)
            rack.udp.sendto(event, (local, self.engine.bridges[side]._udp_local_port))
        wait_for(lambda: self.engine.status()['counts'].get('service_blocked', 0) >= 4)
        # Legitimate messages after the events prove the worker is progressing.
        self.a.operator(0x1280, b'\x22\x98')
        self.b.operator(0x1298, b'\x01')
        wait_for(lambda: any(f['target'] == 0x1280 for f in self.b.received) and
                         any(f['target'] == 0x1298 for f in self.a.received))
        self.assertEqual(invalid_writes, [])
        self.assertEqual(self.engine.bridges['A'].probe_status()['service_events'], 2)
        self.assertEqual(self.engine.bridges['B'].probe_status()['service_events'], 2)
        # Do not silently exclude an unrecognized target on the same object.
        self.b._broadcast(frame(3, self.b.subscriber, event_objects['B'], 0x1001, b'\x77'))
        wait_for(lambda: any(f['a'] == event_objects['A'] and f['target'] == 0x1001
                             for f in self.a.received))

    def test_meter_incident_streams_block_both_directions_tcp_udp_controls_survive(self):
        # Sizes and targets from the physical 0.4.0 incident, independent of
        # frame fragmentation and transport. Keep Input Mixer mapped as well.
        meters = {}
        for side, rack in [('A', self.a), ('B', self.b)]:
            obj = rack.obj + 1; meters[side] = obj
            captured = self.engine.bridges[side].captured
            with captured.lock:
                captured.by_id[obj] = 'Metering Sources'
                captured.by_name['Metering Sources'] = obj
        self.engine.start()
        for side, rack, local in [('A', self.a, '127.0.0.2'), ('B', self.b, '127.0.0.3')]:
            for target, count in [(0x1000, 640), (0x1001, 512)]:
                raw = frame(3, rack.subscriber, meters[side], target, b'\xa0\x00' * count)
                rack._broadcast(raw)
                rack.udp.sendto(raw, (local, self.engine.bridges[side]._udp_local_port))
        wait_for(lambda: self.engine.status()['counts'].get('telemetry_blocked', 0) >= 8)
        self.assertFalse(any(f['target'] in (0x1000, 0x1001)
                             for rack in (self.a, self.b) for f in rack.received))
        self.assertFalse(any(d.startswith(b'\xf1')
                             for rack in (self.a, self.b) for d in rack.datagrams))
        # A real command in the same UDP datagram must still get through.
        meter = frame(3, self.a.subscriber, meters['A'], 0x1000, b'\xa0\x00' * 640)
        fader = frame(3, self.a.subscriber, self.a.obj, 0x1280, b'\x22\x98')
        self.a.udp.sendto(meter + fader,
                         ('127.0.0.2', self.engine.bridges['A']._udp_local_port))
        wait_for(lambda: any(d.startswith(b'\xf1') for d in self.b.datagrams))
        from legacy_lab import decode_raw
        forwarded = decode_raw(next(d for d in self.b.datagrams if d.startswith(b'\xf1')))
        self.assertEqual((forwarded['target'], forwarded['payload']), (0x1280, '22 98'))
        self.assertEqual((forwarded['op'], forwarded['a'], forwarded['b']),
                         (2, self.b.obj, self.b.subscriber))
        # Same target on another object and a new target on Metering Sources
        # remain unknown commands; do not expand this to a blanket ban.
        self.b.operator(0x1000, b'\x11')
        self.b._broadcast(frame(3, self.b.subscriber, meters['B'], 0x1009, b'\x01'))
        wait_for(lambda: any(f['target'] == 0x1000 for f in self.a.received) and
                         any(f['target'] == 0x1009 for f in self.a.received))
        self.assertEqual(self.engine.bridges['A'].probe_status()['telemetry_events'], 5)
        self.assertEqual(self.engine.bridges['B'].probe_status()['telemetry_events'], 4)


class MaximumCoverageTests(unittest.TestCase):
    def test_meter_discovery_kept_without_requesting_measurement_streams(self):
        from legacy_lab import decode_raw
        class FakeBridge:
            side = 'A'; _client_id = 0x7788; _generation = 1
            captured = None
            sent = None
            def log(self, *args, **kwargs):
                pass
            def send_f1(self, raw):
                f = decode_raw(raw); self.sent.append(f)
                if f['target'] == 4:
                    name = bytes.fromhex(f['payload']).rstrip(b'\0').decode()
                    obj = {'Metering Sources':0x3456, 'Global Metering Sources':0x4567}[name]
                    self.captured.observe(decode_raw(frame(3, 3, 0, 2, obj.to_bytes(4, 'big'))))
        specs = [s for s in DATA['objects'] if s['name'] in ('Metering Sources', 'Global Metering Sources')]
        bridge = FakeBridge(); bridge.sent = []
        captured = CapturedSubscriptions(bridge, 'capture_all', specs=specs)
        bridge.captured = captured; captured.start(); captured.thread.join(timeout=2)
        self.assertEqual(captured.status()['phase'], 'requests_sent')
        self.assertEqual(captured.status()['objects_done'], 2)
        self.assertEqual(captured.id_for('Metering Sources'), 0x3456)
        self.assertEqual(captured.status()['skipped_subscriptions'], ['Metering Sources'])
        self.assertFalse(any(f['a'] == 0x3456 for f in bridge.sent))
        self.assertEqual([f['target'] for f in bridge.sent if f['a'] == 0x4567], [23, 24])

    def test_recorded_incident_frames_classify_with_fresh_session_ids(self):
        import json
        from types import SimpleNamespace
        from legacy_lab import decode_raw
        from mirror_app import classify
        samples = json.loads((pathlib.Path(__file__).parent / 'incident_frames.json').read_text())
        class Names:
            def name_for(self, obj):
                return self.name if obj == 0x3456 else None
        names = Names()
        bridge = SimpleNamespace(captured=names, _client_id=0x7788,
                                 _registered_object=7, _server_object=7,
                                 _rx_mapper_object=None, _probe_objects=set())
        for sample in samples:
            with self.subTest(name=sample['name'], expected=sample['expected']):
                original = decode_raw(bytes.fromhex(sample['raw']))
                names.name = sample['name']
                fresh = frame(3, bridge._client_id, 0x3456, original['target'],
                              bytes.fromhex(original['payload']))
                self.assertEqual(classify(fresh, bridge)[0], sample['expected'])

    def test_known_meter_readback_is_distinct_from_controls(self):
        from captured_rx import is_telemetry
        self.assertTrue(is_telemetry('Metering Sources', 0x1002))
        self.assertTrue(is_telemetry('Metering Sources', 0x1008))
        self.assertTrue(is_telemetry('Metering Sources', 0x1000))
        self.assertTrue(is_telemetry('Metering Sources', 0x1001))
        self.assertFalse(is_telemetry('Metering Sources', 0x1009))
        self.assertFalse(is_telemetry('Channel Mapper', 0x1000))
        self.assertFalse(is_telemetry('RTA Control Manager', 0x100c))
        self.assertFalse(is_telemetry('Unrecorded Object', 0x1009))

    def test_full_profile_includes_unselected_objects_and_timeout_keeps_completed(self):
        from legacy_lab import decode_raw
        class FakeBridge:
            side = 'A'; _client_id = 0x7777; _generation = 1
            captured = None
            def log(self, *args, **kwargs):
                pass
            def send_f1(self, raw):
                f = decode_raw(raw)
                if f['target'] == 4 and bytes.fromhex(f['payload']) == b'Previously Unrecorded Object\0':
                    self.captured.observe(decode_raw(frame(3, 3, 0, 2, (0x987).to_bytes(4, 'big'))))
        request = {'target': 23, 'payload': '00 00 00 00 00 2e 01 00 00 12 80 00 00 12 80 00 00 00 30 00 ff'}
        specs = [{'name':'Previously Unrecorded Object', 'selected':False, 'requests':[request]},
                 {'name':'Missing Object', 'selected':False, 'requests':[request]}]
        bridge = FakeBridge()
        captured = CapturedSubscriptions(bridge, 'capture_all', specs=specs, lookup_timeout=.08)
        bridge.captured = captured; captured.start(); captured.thread.join(timeout=2)
        status = captured.status()
        self.assertEqual(status['objects_total'], 2)
        self.assertEqual(status['objects_done'], 1)
        self.assertEqual(status['phase'], 'partial_requests_sent')
        self.assertEqual(captured.id_for('Previously Unrecorded Object'), 0x987)
        # A late reply cannot claim the missing object's name after timeout.
        captured.observe(decode_raw(frame(3, 3, 0, 2, (0x654).to_bytes(4, 'big'))))
        self.assertIsNone(captured.id_for('Missing Object'))


if __name__ == '__main__':
    unittest.main()
