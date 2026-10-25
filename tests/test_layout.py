import json
import pathlib
import time
import unittest
from layout_map import ORDER, width, plan_changes
from surface_link import SurfaceMirror


def banks(widths,fill=b'\0\0'):
    return {n:fill*(w*6) for n,w in zip(ORDER,widths)}


def set_slot(raw,w,layer,slot,value):
    out=bytearray(raw);i=2*(layer*w+slot);out[i:i+2]=value;return bytes(out)


class LayoutMappingTests(unittest.TestCase):
    def test_physical_capture_changes_only_fifth_slot_without_copying_other_channels(self):
        fixture=json.loads((pathlib.Path(__file__).parent/'layout_incident.json').read_text())
        src={};dst={};before=None;after=None
        for e in fixture['properties']:
            if e['name'] not in ORDER or e['target']!='0x1001':continue
            raw=bytes.fromhex(e['payload'])
            if e['side']=='A':
                if e['name'] in src:before=src[e['name']];after=raw
                else:src[e['name']]=raw
            else:dst[e['name']]=raw
        self.assertEqual([width(src[n]) for n in ORDER],[12,8,8])
        self.assertEqual([width(dst[n]) for n in ORDER],[12,0,12])
        result,missing,changed=plan_changes(ORDER[0],before,after,src,dst)
        expected=set_slot(dst[ORDER[0]],12,0,4,bytes.fromhex('01 04'))
        self.assertEqual(result,{ORDER[0]:expected});self.assertEqual(missing,[])
        self.assertEqual(changed,[dict(layer=1,strip=5,bank=ORDER[0],slot=5)])
        self.assertEqual(result[ORDER[0]][2:6],b'\0'*4)

    def test_middle_bank_maps_to_destination_right_on_every_layer(self):
        src=banks((12,8,8));dst=banks((12,0,12),b'\x0d\x01');after=src[ORDER[1]]
        for layer in range(6):after=set_slot(after,8,layer,7,bytes([1,layer]))
        result,missing,changed=plan_changes(ORDER[1],src[ORDER[1]],after,src,dst)
        expected=dst[ORDER[2]]
        for layer in range(6):expected=set_slot(expected,12,layer,7,bytes([1,layer]))
        self.assertEqual(result,{ORDER[2]:expected});self.assertFalse(missing)
        self.assertEqual(len(changed),6)

    def test_right_bank_delta_splits_between_two_destination_banks_in_reverse(self):
        src=banks((12,0,12));dst=banks((12,8,8),b'\x0d\x02');after=src[ORDER[2]]
        after=set_slot(after,12,3,7,b'\x01\x07');after=set_slot(after,12,3,8,b'\x01\x08')
        result,missing,_=plan_changes(ORDER[2],src[ORDER[2]],after,src,dst)
        self.assertEqual(result[ORDER[1]],set_slot(dst[ORDER[1]],8,3,7,b'\x01\x07'))
        self.assertEqual(result[ORDER[2]],set_slot(dst[ORDER[2]],8,3,0,b'\x01\x08'))
        self.assertFalse(missing)

    def test_missing_physical_slot_reported_and_malformed_layout_rejected(self):
        src=banks((12,8,8));dst=banks((12,0,12));after=set_slot(src[ORDER[2]],8,5,7,b'\x01\x08')
        result,missing,_=plan_changes(ORDER[2],src[ORDER[2]],after,src,dst)
        self.assertEqual(result,{});self.assertEqual(missing,[(6,28)])
        with self.assertRaises(ValueError):width(b'\x01')
        with self.assertRaises(ValueError):plan_changes(ORDER[0],b'',b'\0'*12,src,dst)
        with self.assertRaises(ValueError):plan_changes(ORDER[0],src[ORDER[0]],src[ORDER[0]],src,{})


class FakeSurface:
    def __init__(self):self.generation=1;self.writes=[];self.connected=True;self.client=44;self.by_name={n:i+10 for i,n in enumerate(ORDER)}
    def write_bank(self,name,target,payload,generation):self.writes.append((name,target,payload))
    def request_bank_readback(self,name,generation):pass
    def status(self):return dict(connected=self.connected,phase='ready')
    def disconnect(self):self.connected=False


class LayoutReadbackTests(unittest.TestCase):
    def setUp(self):
        self.logs=[];self.m=SurfaceMirror(lambda *a,**kw:self.logs.append((a,kw)))
        self.m.cfg=dict(direction='both',echo_seconds=5)
        self.m.sessions={s:FakeSurface() for s in 'AB'}
        self.m.layouts={'A':banks((12,8,8)),'B':banks((12,0,12))}
        self.m.start()

    def change(self,side,name,value):
        self.m.property(side,name,dict(target=0x1001,payload=value.hex(' ')),1)

    def test_rapid_changes_keep_pending_destination_and_old_echo_cannot_rewind(self):
        a=self.m.layouts['A'][ORDER[0]]
        one=set_slot(a,12,0,0,b'\x01\x01');self.change('A',ORDER[0],one)
        two=set_slot(one,12,0,1,b'\x01\x02');self.change('A',ORDER[0],two)
        self.change('B',ORDER[0],one) # delayed echo of first write
        self.assertEqual(self.m.layouts['B'][ORDER[0]],two)
        three=set_slot(two,12,0,2,b'\x01\x03');self.change('A',ORDER[0],three)
        self.assertEqual(self.m.sessions['B'].writes[-1][2],three)
        self.change('B',ORDER[0],three)
        self.assertFalse(self.m.pending_layouts)
        self.assertEqual(self.m.status()['counts']['layout_readback_matched'],1)
        self.assertEqual(self.m.sessions['A'].writes,[])

    def test_timeout_warns_without_stopping_or_blind_retry_and_late_reply_clears(self):
        value=set_slot(self.m.layouts['A'][ORDER[0]],12,0,0,b'\x01\x01')
        self.change('A',ORDER[0],value)
        self.m.pending_layouts[('B',ORDER[0])]['at']=time.monotonic()-4
        status=self.m.status();self.assertTrue(status['running']);self.assertIsNone(status['error'])
        self.assertIsNotNone(status['warning']);self.assertEqual(len(status['layout_unconfirmed']),1)
        self.assertEqual(len(self.m.sessions['B'].writes),1)
        self.change('B',ORDER[0],value)
        self.assertFalse(self.m.status()['layout_unconfirmed'])
        self.assertIsNone(self.m.status()['warning'])

    def test_conflicting_readback_stops_layout_and_retains_error(self):
        before=self.m.layouts['A'][ORDER[0]]
        value=set_slot(before,12,0,0,b'\x01\x01');self.change('A',ORDER[0],value)
        self.change('B',ORDER[0],before)
        status=self.m.status();self.assertFalse(status['running'])
        self.assertEqual(status['last_error']['type'],'surface_layout_readback_mismatch')
        self.assertEqual(self.m.sessions['A'].writes,[])

class LayoutNetworkTests(unittest.TestCase):
    def test_explicit_sync_confirms_without_spontaneous_echo_and_mismatch_is_retained(self):
        import copy
        from mirror_app import DEFAULT_CONFIG
        from test_surface import SurfaceMock
        from test_direct import wait_for
        a=SurfaceMock(0xb30,(12,8,8));b=SurfaceMock(0xc30,(12,0,12))
        b.echo_layout_writes=False
        m=SurfaceMirror(lambda *a,**kw:None)
        try:
            cfg=copy.deepcopy(DEFAULT_CONFIG)
            for side,rack,local in [('A',a,'127.0.0.2'),('B',b,'127.0.0.3')]:
                cfg[side].update(surface_ip='127.0.0.1',surface_tcp=rack.port,
                                 surface_udp=rack.udp_port,local_ip=local)
            m.connect(cfg)
            wait_for(lambda:all(s.status()['phase']=='ready' for s in m.sessions.values()) and
                     all(len(x)==3 for x in m.layouts.values()))
            m.start();baseline=m.layouts['A'][ORDER[0]];offset=len(b.requests)
            value=set_slot(baseline,12,0,4,b'\x01\x04')
            a.emit(ORDER[0],0x1001,value)
            wait_for(lambda:m.status()['counts'].get('layout_readback_matched',0)==1)
            self.assertTrue(m.status()['running'])
            self.assertEqual([f['target'] for f in b.requests[offset:] if f['a']==b.objects[ORDER[0]]],
                             [0x1001,8])
            # A rejected later write returns the old actual state on explicit
            # sync. It must never be mirrored back as an operator's action.
            b.accept_layout_writes=False
            b.layouts[ORDER[0]]=set_slot(baseline,12,0,7,b'\x01\x07')
            value=set_slot(value,12,0,5,b'\x01\x05')
            a.emit(ORDER[0],0x1001,value)
            wait_for(lambda:len(b.writes)==2)
            wait_for(lambda:not m.status()['running'])
            self.assertEqual(m.status()['last_error']['type'],'surface_layout_readback_mismatch')
            self.assertTrue(m.sessions['B'].status()['connected'])
            m.disconnect()
            self.assertEqual(m.status()['last_error']['type'],'surface_layout_readback_mismatch')
            self.assertEqual(m.status()['last_state']['layout_geometry']['B'][ORDER[0]],12)
        finally:m.disconnect();a.close();b.close()

    def test_different_bank_widths_over_real_tcp_with_destination_readback(self):
        import copy
        from mirror_app import DEFAULT_CONFIG
        from test_surface import SurfaceMock
        from test_direct import wait_for
        a=SurfaceMock(0x930,(12,8,8));b=SurfaceMock(0xa30,(12,0,12))
        m=SurfaceMirror(lambda *a,**kw:None)
        try:
            cfg=copy.deepcopy(DEFAULT_CONFIG)
            for side,rack,local in [('A',a,'127.0.0.2'),('B',b,'127.0.0.3')]:
                cfg[side].update(surface_ip='127.0.0.1',surface_tcp=rack.port,
                                 surface_udp=rack.udp_port,local_ip=local)
            m.connect(cfg)
            wait_for(lambda:all(s.status()['phase']=='ready' for s in m.sessions.values()) and
                     all(len(x)==3 for x in m.layouts.values()))
            m.start()
            value=set_slot(m.layouts['A'][ORDER[1]],8,2,0,b'\x01\x09')
            a.emit(ORDER[1],0x1001,value)
            wait_for(lambda:m.status()['counts'].get('layout_readback_matched',0)==1)
            self.assertEqual(b.writes[-1]['a'],b.objects[ORDER[2]])
            self.assertEqual(bytes.fromhex(b.writes[-1]['payload']),
                             set_slot(b'\0'*144,12,2,0,b'\x01\x09'))
            value=set_slot(m.layouts['B'][ORDER[2]],12,4,8,b'\x01\x0a')
            b.emit(ORDER[2],0x1001,value)
            wait_for(lambda:m.status()['counts'].get('layout_readback_matched',0)==2)
            self.assertEqual(a.writes[-1]['a'],a.objects[ORDER[2]])
            self.assertEqual(bytes.fromhex(a.writes[-1]['payload']),
                             set_slot(b'\0'*96,8,4,0,b'\x01\x0a'))
            self.assertFalse(m.status()['layout_pending'])
            self.assertTrue(m.status()['running'])
        finally:m.disconnect();a.close();b.close()

class EngineIsolationTests(unittest.TestCase):
    def test_layout_warning_and_failure_do_not_stop_live_mixrack_relay(self):
        from test_direct import DirectNetworkTests, wait_for
        from ahnet_lab import frame
        DirectNetworkTests.setUp(self)
        try:
            m=self.engine.surfaces
            m.sessions={s:FakeSurface() for s in 'AB'}
            m.cfg=dict(direction='both',echo_seconds=5)
            m.layouts={'A':banks((12,8,8)),'B':banks((12,0,12))}
            self.engine.cfg['surface_banks']=True;m.start()
            value=set_slot(m.layouts['A'][ORDER[0]],12,0,0,b'\x01\x01')
            m.property('A',ORDER[0],dict(target=0x1001,payload=value.hex(' ')),1)
            m.pending_layouts[('B',ORDER[0])]['at']=time.monotonic()-4
            self.assertTrue(self.engine.status()['running'])
            self.assertTrue(self.engine.status()['surfaces']['running'])
            self.assertTrue(self.engine.status()['surfaces']['warning'])
            # A genuine conflicting readback isolates only the Surface relay.
            value=set_slot(value,12,0,1,b'\x01\x02')
            m.property('A',ORDER[0],dict(target=0x1001,payload=value.hex(' ')),1)
            wrong=set_slot(m.layouts['B'][ORDER[0]],12,0,7,b'\x01\x07')
            m.property('B',ORDER[0],dict(target=0x1001,payload=wrong.hex(' ')),1)
            status=self.engine.status()
            self.assertTrue(status['running']);self.assertFalse(status['surfaces']['running'])
            self.a._broadcast(frame(3,0x2e,0x30,0x1280,b'\x81\x00'))
            wait_for(lambda:any(f['target']==0x1280 for f in self.b.received))
            self.assertEqual(self.engine.status()['counts'].get('surface_connection_lost_stopped',0),0)
            self.engine.stop();self.assertFalse(self.engine.status()['running'])
        finally:DirectNetworkTests.tearDown(self)
