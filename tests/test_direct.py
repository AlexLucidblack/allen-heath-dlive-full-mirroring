import copy
import pathlib
import tempfile
import threading
import time
import unittest

from ahnet_lab import frame
from legacy_lab import VirtualMixRack, decode_raw
from mirror_app import DirectMirror, WireSplitter, DEFAULT_CONFIG, validate_config


def wait_for(predicate, seconds=3):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('condition not met')


class CapturingRack(VirtualMixRack):
    def __init__(self, model):
        self.received = []; self.datagrams = []; self.model = model
        super().__init__(port=0, udp_port=0, on_frame=self.received.append,
                         on_udp=lambda d, _: self.datagrams.append(d))

    def _send(self, conn, raw):
        if self.model == 'CDM32':
            raw = raw.replace(b'TLDDM64Stagebox', b'TLDCDM32Stagebox')
            # Replacement changes F1 length; re-encode rather than corrupt header.
            if b'TLDCDM32Stagebox' in raw:
                old = raw[19:-1]
                raw = frame(int.from_bytes(raw[1:3], 'big'), int.from_bytes(raw[3:7], 'big'),
                            int.from_bytes(raw[7:11], 'big'), int.from_bytes(raw[11:15], 'big'), old)
        return super()._send(conn, raw)


class SplitterTests(unittest.TestCase):
    def test_split_frames_and_interleaved_service_preserve_order(self):
        raw = frame(2, 800, 900, 99, b'unknown')
        split = WireSplitter()
        self.assertEqual(split.feed(raw[:16]), [])
        self.assertEqual(split.feed(raw[16:] + b'\xe0\x00\x01\x03\xe7' + raw),
                         [('f1', raw), ('service', b'\xe0\x00\x01\x03\xe7'), ('f1', raw)])

    def test_opaque_udp_and_truncated_f1_are_preserved(self):
        for data in (b'\x77\x00\x80\x01', b'\xf1\x00\x00'):
            self.assertEqual(WireSplitter().feed(data, datagram=True), [('opaque', data)])

    def test_heartbeat_response_trailer_does_not_leak(self):
        data = b'\xe0\x00\x01\x03\xe7' + bytes(range(13))
        self.assertEqual(WireSplitter().feed(data, datagram=True), [('service', data)])

    def test_six_addresses_and_ports_validate(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg['A'].update(local_ip='192.168.1.201')
        cfg['B'].update(ip='192.168.2.70', surface_ip='192.168.2.71', local_ip='192.168.2.201')
        valid = validate_config(cfg)
        self.assertEqual(valid['B']['local_ip'], '192.168.2.201')
        cfg['B']['local_ip'] = cfg['B']['ip']
        with self.assertRaises(ValueError):
            validate_config(cfg)


class DirectNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = CapturingRack('DM64'); self.b = CapturingRack('CDM32')
        self.a.start(); self.b.start()
        self.engine = DirectMirror(pathlib.Path(self.tmp.name) / 'connection.json', log_dir=self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)
        for side, rack, local in [('A', self.a, '127.0.0.2'), ('B', self.b, '127.0.0.3')]:
            self.cfg[side].update(ip='127.0.0.1', tcp=rack.port, udp=rack.udp_port,
                                  local_ip=local, surface_ip='', model=rack.model)
        self.cfg.update(direction='both', probe='mapper8')
        self.engine.connect(self.cfg)
        self.assertTrue(all(x['connected'] for x in self.engine.status()['sides'].values()))
        wait_for(lambda: all(b.probe_status()['phase'] == 'complete' for b in self.engine.bridges.values()))
        self.engine.start()
        self.a.received.clear(); self.b.received.clear()

    def tearDown(self):
        self.engine.close(); self.a.stop(); self.b.stop(); self.tmp.cleanup()

    def count(self, key):
        return self.engine.status()['counts'].get(key, 0)

    def test_both_source_ips_are_used_by_tcp_and_udp(self):
        for side, expected in [('A', '127.0.0.2'), ('B', '127.0.0.3')]:
            bridge = self.engine.bridges[side]
            self.assertEqual(bridge._tx_sock.getsockname()[0], expected)
            self.assertEqual(bridge._udp_sock.getsockname()[0], expected)
        self.assertEqual(self.engine.status()['config']['A']['surface_ip'], '')

    def test_unknown_f1_and_unknown_low_target_pass_unchanged(self):
        for op, target in [(2, 0x3456), (3, 0x9876), (2, 99), (9, 0x7788)]:
            raw = frame(op, 800, 900, target, b'\xaa\xbb')
            self.a._broadcast(raw)
            wait_for(lambda: any(bytes.fromhex(f['raw']) == raw for f in self.b.received))
        self.assertGreaterEqual(self.count('forward_unknown'), 4)
        self.assertEqual(self.count('relay_error_stopped'), 0)

    def test_unknown_own_client_event_not_treated_as_service(self):
        raw = frame(3, self.engine.bridges['A']._client_id, 777, 0x8765, b'\x11')
        self.a._broadcast(raw)
        wait_for(lambda: any(bytes.fromhex(f['raw']) == raw for f in self.b.received))

    def test_known_fader_notification_becomes_write_and_echo_stops(self):
        self.a._broadcast(frame(3, 0x2e, 0x30, 0x1280, b'\x81\x00'))
        expected = frame(2, 0x30, 0x2e, 0x1280, b'\x81\x00')
        wait_for(lambda: any(bytes.fromhex(f['raw']) == expected for f in self.b.received))
        wait_for(lambda: self.count('echo_suppressed') >= 1)
        time.sleep(.1)
        self.assertFalse(any(f['target'] == 0x1280 for f in self.a.received))

    def test_reverse_direction_transfers(self):
        raw = frame(2, 800, 900, 0x4321, b'\x44')
        self.b._broadcast(raw)
        wait_for(lambda: any(bytes.fromhex(f['raw']) == raw for f in self.a.received))

    def test_conversion_can_be_disabled(self):
        self.engine.stop(); self.engine.cfg['convert_known_notifications'] = False; self.engine.start()
        raw = frame(3, 0x2e, 0x30, 0x1280, b'\x81\x00')
        self.a._broadcast(raw)
        wait_for(lambda: any(bytes.fromhex(f['raw']) == raw for f in self.b.received))

    def test_send_failure_stops_relay(self):
        def fail(*_):
            raise OSError('simulated disconnect during TX')
        self.engine.bridges['B'].send_raw = fail
        self.a._broadcast(frame(2, 800, 900, 0x4567, b'\x12'))
        wait_for(lambda: self.count('relay_error_stopped') > 0)
        self.assertFalse(self.engine.status()['running'])

    def test_known_service_families_and_session_rpc_not_forwarded(self):
        before = self.count('service_blocked')
        service = [b'\xe0\x00\x01\x03\xe7', b'\xf0\x00\x02\xf7',
                   frame(3, 3, 0, 2, b'\x00\x00\x00\x07')]
        for raw in service:
            self.a._broadcast(raw)
        wait_for(lambda: self.count('service_blocked') >= before + 3)
        self.assertFalse(self.b.received)

    def test_udp_mixed_service_and_unknown_keeps_datagram(self):
        bridge = self.engine.bridges['A']
        unknown = frame(2, 800, 900, 0x7654, b'\x12\x34')
        self.a.udp.sendto(b'\xe0\x00\x01\x03\xe7' + unknown,
                          ('127.0.0.2', bridge._udp_local_port))
        wait_for(lambda: unknown in self.b.datagrams)
        self.assertNotIn(b'\xe0\x00\x01\x03\xe7' + unknown, self.b.datagrams)

    def test_opaque_udp_passes_unchanged(self):
        bridge = self.engine.bridges['A']; data = b'\x77\x00\x88\x99'
        self.a.udp.sendto(data, ('127.0.0.2', bridge._udp_local_port))
        wait_for(lambda: data in self.b.datagrams)

    def test_stop_drops_old_jobs_and_restart_does_not_replay(self):
        raw = frame(2, 800, 900, 0x5432, b'\x55')
        old = (self.engine.epoch, tuple(self.engine.bridges[s]._generation for s in ('A', 'B')),
               'A', 'tcp', [('f1', raw)])
        self.engine.stop(); self.engine.start(); self.engine._route(old)
        self.assertFalse(any(f['target'] == 0x5432 for f in self.b.received))

    def test_single_direction_blocks_reverse(self):
        self.engine.stop(); self.engine.cfg['direction'] = 'A_to_B'; self.engine.start()
        before = self.count('received')
        self.b._broadcast(frame(2, 800, 900, 0x5432, b'\x55'))
        wait_for(lambda: self.count('received') > before)
        self.engine.jobs.join()
        self.assertFalse(any(f['target'] == 0x5432 for f in self.a.received))

    def test_fast_value_change_then_return_is_not_dropped(self):
        raw = b''.join(frame(3, 0x2e, 0x30, 0x1280, value) for value in (b'\x80\x00', b'\x81\x00', b'\x80\x00'))
        self.a._broadcast(raw)
        wait_for(lambda: len([f for f in self.b.received if f['target'] == 0x1280]) >= 3)
        values = [f['payload'] for f in self.b.received if f['target'] == 0x1280]
        self.assertEqual(values[:3], ['80 00', '81 00', '80 00'])

    def test_disconnect_stops_and_config_persists(self):
        self.b.stop()
        wait_for(lambda: not self.engine.status()['running'])
        self.assertEqual(self.engine.cfg['B']['local_ip'], '127.0.0.3')
        self.assertTrue(self.engine.config_path.exists())

    def test_phantom_unmapped_candidate_never_crosses_tcp_udp_either_direction(self):
        start=self.count('phantom_blocked')
        for side,rack in [('A',self.a),('B',self.b)]:
            for value in (b'\x00',b'\x01'):
                raw=frame(2,800,900,0x1003,value)
                rack._broadcast(raw)
                bridge=self.engine.bridges[side]
                rack.udp.sendto(raw,(self.engine.cfg[side]['local_ip'],bridge._udp_local_port))
        wait_for(lambda:self.count('phantom_blocked')>=start+8)
        self.assertFalse(any(f['target']==0x1003 for r in (self.a,self.b) for f in r.received))
        self.assertFalse(any(decode_raw(d)['target']==0x1003 for r in (self.a,self.b) for d in r.datagrams if d.startswith(b'\xf1')))


if __name__ == '__main__':
    unittest.main()
