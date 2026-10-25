import copy
import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch
from ahnet_lab import frame
from legacy_lab import decode_raw
from captured_rx import DATA,TEMPO,CapturedSubscriptions
from mirror_app import DirectMirror,DEFAULT_CONFIG,WireSplitter
from test_direct import CapturingRack,wait_for
import test_redundancy as fixtures

FIX=json.loads((pathlib.Path(__file__).parent/'global_tempo_fixture.json').read_text())


class TempoRack(CapturingRack):
    """Exact recorded 6/8 registration; intentionally fresh object IDs.

    The real capture sends no global notification back to its writing client.
    The mock follows that behavior unless echo is explicitly enabled.
    """
    def __init__(self,obj):
        self.obj=obj;self.subscriber=None;self.registrations=[];self.writes=[];self.period=221;self.echo=False
        super().__init__('DM64')
    def _process(self,conn,f):
        payload=bytes.fromhex(f['payload'])
        if (f['a'],f['b'],f['target'])==(0,3,4) and payload==TEMPO['name'].encode()+b'\0':
            self._send(conn,frame(3,3,0,2,self.obj.to_bytes(4,'big')));return
        if f['a']==self.obj and f['target']==6:
            assert f['b']==3 and len(payload)==16
            self.subscriber=int.from_bytes(payload[4:6],'big');self.registrations.append(f);return
        if f['a']==self.obj and f['target']==8:
            assert f['b']==self.subscriber and not payload
            self.registrations.append(f)
            self._send(conn,frame(3,self.subscriber,self.obj,8,b''))
            self._send(conn,frame(3,self.subscriber,self.obj,0x1000,self.period.to_bytes(2,'big')));return
        if f['a']==self.obj and f['target']==0x1000:
            assert f['op']==2 and f['b']==self.subscriber and len(payload)==2
            self.period=int.from_bytes(payload,'big');self.writes.append(f)
            if self.echo:self._send(conn,frame(3,self.subscriber,self.obj,0x1000,payload))
            return
        super()._process(conn,f)
    def operator(self,period,transport='tcp',bridge=None):
        self.period=period;raw=frame(3,self.subscriber,self.obj,0x1000,period.to_bytes(2,'big'))
        if transport=='tcp':self._broadcast(raw)
        else:self.udp.sendto(raw,(bridge.bind_ip,bridge._udp_local_port))


class TempoCaptureTests(unittest.TestCase):
    def test_recorded_name_template_method_and_units(self):
        self.assertEqual(FIX['name'],TEMPO['name']);self.assertEqual(FIX['root_lookups'],FIX['root_replies'])
        self.assertEqual(FIX['tcp_gaps'],0)
        registration=decode_raw(bytes.fromhex(FIX['subscription'][0]))
        request=decode_raw(bytes.fromhex(FIX['subscription'][1]))
        self.assertEqual((registration['op'],registration['target'],registration['b']),(2,6,3))
        self.assertEqual(registration['payload'],TEMPO['requests'][0]['payload'])
        self.assertEqual((request['target'],request['payload']),(8,''))
        for sample,bpm in zip(FIX['controlled_period_writes'],(150,120,100)):
            f=decode_raw(bytes.fromhex(sample['raw']));period=int.from_bytes(bytes.fromhex(f['payload']),'big')
            self.assertEqual((f['op'],f['target'],len(bytes.fromhex(f['payload']))),(2,0x1000,2))
            self.assertEqual(60000/period,bpm)
    def test_tap_values_match_seven_measured_intervals(self):
        for previous,current in zip(FIX['taps'],FIX['taps'][1:]):
            self.assertLess(abs((current['time']-previous['time'])*1000-current['period_ms']),1)
    def test_in_full_and_catalogue_profiles_but_not_input1_probe(self):
        class Bridge:pass
        for profile in ('capture_all','capture_catalog'):
            self.assertIn(TEMPO['name'],[s['name'] for s in CapturedSubscriptions(Bridge(),profile).specs])
        self.assertNotIn(TEMPO['name'],[s['name'] for s in CapturedSubscriptions(Bridge(),'capture_input1').specs])


class TempoNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.specs=patch.dict(DATA,objects=[TEMPO]);self.specs.start()
        self.ping=patch('redundancy.ping',lambda *args:dict(ok=True,fixture=True));self.ping.start()
        self.a=TempoRack(0x353);self.b=TempoRack(0x653);self.a.start();self.b.start()
        self.e=DirectMirror(pathlib.Path(self.tmp.name)/'connection.json',log_dir=self.tmp.name)
        cfg=copy.deepcopy(DEFAULT_CONFIG)
        for side,rack,local in [('A',self.a,'127.0.0.2'),('B',self.b,'127.0.0.3')]:
            cfg[side].update(ip='127.0.0.1',tcp=rack.port,udp=rack.udp_port,local_ip=local,surface_ip='')
        cfg.update(probe='capture_all',direction='both');self.e.connect(cfg)
        wait_for(lambda:all(b.probe_status()['phase']=='requests_sent' and b.probe_status()['global_tempo']['incoming_events'] for b in self.e.bridges.values()))
        self.e.start()
    def tearDown(self):
        self.e.close();self.a.stop();self.b.stop();self.ping.stop();self.specs.stop();self.tmp.cleanup()
    def test_fresh_6_8_subscription_exact_bytes_and_no_initial_copy(self):
        for side,rack in [('A',self.a),('B',self.b)]:
            self.assertEqual([f['target'] for f in rack.registrations[:2]],[6,8])
            self.assertTrue(all(f['target']==8 for f in rack.registrations[2:]))
            payload=bytes.fromhex(rack.registrations[0]['payload']);client=self.e.bridges[side]._client_id
            original=bytes.fromhex(TEMPO['requests'][0]['payload'])
            self.assertEqual(payload,original[:4]+client.to_bytes(2,'big')+original[6:])
            self.assertEqual(rack.registrations[1]['b'],client)
            self.assertEqual(self.e.bridges[side].probe_status()['expected_acks'],1)
            self.assertGreaterEqual(self.e.bridges[side].probe_status()['ack_count'],1)
        self.assertFalse(self.a.writes);self.assertFalse(self.b.writes)
    def test_direct_period_write_both_directions_without_waiting_for_echo(self):
        for n,value in enumerate((500,400,600),1):
            self.a.operator(value);wait_for(lambda:len(self.b.writes)==n)
        self.assertEqual([f['payload'] for f in self.b.writes],['01 f4','01 90','02 58'])
        for n,value in enumerate((580,660,620),1):
            self.b.operator(value);wait_for(lambda:len(self.a.writes)==n)
        for rack,side in [(self.a,'A'),(self.b,'B')]:
            for f in rack.writes:self.assertEqual((f['op'],f['a'],f['b'],f['target']),(2,rack.obj,self.e.bridges[side]._client_id,0x1000))
    def test_echo_stays_local_and_does_not_oscillate(self):
        self.b.echo=True;self.a.operator(580);wait_for(lambda:len(self.b.writes)==1)
        wait_for(lambda:self.e.status()['counts'].get('echo_suppressed',0)>0)
        self.assertFalse(self.a.writes)
    def test_operator_can_return_to_earlier_tempo_after_newer_write(self):
        self.a.operator(400);wait_for(lambda:len(self.b.writes)==1)
        self.a.operator(600);wait_for(lambda:len(self.b.writes)==2)
        self.b.operator(400);wait_for(lambda:len(self.a.writes)==1)
        self.assertEqual(self.a.period,400)
        self.b.operator(600);wait_for(lambda:len(self.a.writes)==2)
        self.assertEqual(self.a.period,600)
    def test_readback_poll_catches_silent_change_without_rewriting_steady_clock(self):
        self.a.period=600
        wait_for(lambda:len(self.b.writes)==1)
        self.assertEqual(self.b.period,600)
        time.sleep(.6)
        self.assertEqual(len(self.b.writes),1)
        self.assertFalse(self.a.writes)
    def test_udp_notification_maps_global_method_instead_of_meter_filter(self):
        self.a.operator(600,'udp',self.e.bridges['A'])
        wait_for(lambda:any(d.startswith(b'\xf1') for d in self.b.datagrams))
        f=decode_raw(next(d for d in self.b.datagrams if d.startswith(b'\xf1')))
        self.assertEqual((f['op'],f['a'],f['b'],f['target'],f['payload']),(2,self.b.obj,self.e.bridges['B']._client_id,0x1000,'02 58'))
    def test_auto_failover_replays_global_periods_in_both_directions(self):
        self.e.stop();self.e.cfg.update(auto_redundancy=True,surface_observe=True,direction='A_to_B')
        self.e.surfaces.factory=fixtures.FakeSurface;self.e.surfaces.connect(self.e.cfg)
        wait_for(lambda:all(self.e.redundancy._ready(s) for s in 'AB'))
        self.e.start();r=self.e.redundancy
        r.retry_at={s:time.monotonic()+100 for s in 'AB'}
        for failed,source,rack,values,destination in [('A','B',self.b,(580,660,620),self.a),('B','A',self.a,(519,560),self.b)]:
            healthy=self.e.bridges[failed];old=self.e.surfaces.sessions[failed];old.connected=False
            wait_for(lambda:r.master==source)
            for value in values:rack.operator(value)
            wait_for(lambda:r.journal.pending_count==len(values))
            r.retry_at[failed]=0
            wait_for(lambda:self.e.surfaces.sessions[failed] is not old and r._ready(failed))
            wait_for(lambda:len(destination.writes)==len(values))
            self.assertEqual([int.from_bytes(bytes.fromhex(f['payload']),'big') for f in destination.writes],list(values))
            self.assertIs(self.e.bridges[failed],healthy);self.assertEqual(r.master,source)
            r.retry_at[failed]=time.monotonic()+100
