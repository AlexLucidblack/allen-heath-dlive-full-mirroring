import copy
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from ahnet_lab import frame
from legacy_lab import decode_raw
from mirror_app import WireSplitter, DEFAULT_CONFIG, DirectMirror, validate_config
from surface_link import SurfaceMirror, DATA, EXTRA
from layout_map import ORDER
from test_direct import wait_for, CapturingRack


class SurfaceMock:
    def __init__(self,base,widths=(2,0,2)):
        self.tcp=socket.socket();self.tcp.bind(('127.0.0.1',0));self.tcp.listen();self.tcp.settimeout(.2)
        self.port=self.tcp.getsockname()[1]
        self.udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.udp.bind(('127.0.0.1',0));self.udp.settimeout(.2)
        self.udp_port=self.udp.getsockname()[1];self.stop_event=threading.Event();self.conn=None
        self.client=None;self.requests=[];self.writes=[];self.error=None
        self.objects={o['name']:base+i for i,o in enumerate(DATA['objects'])}
        self.layouts={n:b'\0'*(w*12) for n,w in zip(ORDER,widths)}
        self.echo_layout_writes=True;self.accept_layout_writes=True;self.answer_layout_reads=True
        self.peer=None;self.udp_peer=None
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
        self.ut=threading.Thread(target=self.run_udp,daemon=True);self.ut.start()

    def send(self,data):
        if self.conn:self.conn.sendall(data)

    def emit(self,name,target,payload):
        if name in ORDER and target==0x1001:self.layouts[name]=payload
        self.send(frame(3,self.client,self.objects[name],target,payload))

    def run_udp(self):
        while not self.stop_event.is_set():
            try:
                data,peer=self.udp.recvfrom(65535);self.udp_peer=peer
                self.udp.sendto(data,peer)
            except socket.timeout:continue
            except OSError:return

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:self.conn,self.peer=self.tcp.accept();break
                except socket.timeout:continue
            if not self.conn:return
            self.conn.settimeout(.2);split=WireSplitter()
            while not self.stop_event.is_set():
                try:data=self.conn.recv(65535)
                except socket.timeout:continue
                if not data:return
                for kind,raw in split.feed(data):
                    if kind=='service' and raw.startswith(b'\xf0'):
                        assert raw[1:3]==b'\x00\x01'
                        self.client=int.from_bytes(raw[5:7],'big')
                        self.send(frame(3,self.client,0,2,(5).to_bytes(4,'big')))
                    if kind!='f1':continue
                    f=decode_raw(raw);self.requests.append(f);assert f['op']==1,f
                    if f['a']==5 and f['target']==0x100:
                        self.send(frame(3,self.client,5,2,b'TLD168Surface V2.12 - Rev. 94091\0'))
                    elif f['a']==0 and f['target']==4:
                        name=bytes.fromhex(f['payload']).rstrip(b'\0').decode()
                        self.send(frame(3,3,0,2,self.objects.get(name,0).to_bytes(4,'big')))
                    elif f['target'] in (6,23):
                        assert int.from_bytes(bytes.fromhex(f['payload'])[4:6],'big')==self.client
                    elif f['target'] in (8,24):
                        self.send(frame(3,self.client,f['a'],f['target'],bytes.fromhex(f['payload'])))
                        for name in ORDER:
                            if f['a']==self.objects[name] and f['target']==8 and self.answer_layout_reads:
                                self.emit(name,0x1001,self.layouts[name])
                    elif f['target']>=0x1000:
                        self.writes.append(f)
                        name=next((n for n in ORDER if self.objects[n]==f['a']),None)
                        if name and f['target']==0x1001:
                            if self.accept_layout_writes:
                                self.layouts[name]=bytes.fromhex(f['payload'])
                                if self.echo_layout_writes:self.emit(name,0x1001,self.layouts[name])
                        else:self.send(frame(3,self.client,f['a'],f['target'],bytes.fromhex(f['payload'])))
        except (OSError,AssertionError) as ex:
            if not self.stop_event.is_set():self.error=str(ex)

    def close(self):
        self.stop_event.set()
        for s in (self.conn,self.tcp,self.udp):
            if s:
                try:s.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                s.close()
        self.thread.join(timeout=1);self.ut.join(timeout=1)


class SurfaceTests(unittest.TestCase):
    def setUp(self):
        self.a=SurfaceMock(0x330);self.b=SurfaceMock(0x430);self.logs=[]
        self.m=SurfaceMirror(lambda *a,**kw:self.logs.append((a,kw)))
        self.cfg=copy.deepcopy(DEFAULT_CONFIG)
        for side,rack,local in [('A',self.a,'127.0.0.2'),('B',self.b,'127.0.0.3')]:
            self.cfg[side].update(surface_ip='127.0.0.1',local_ip=local,surface_tcp=rack.port,surface_udp=rack.udp_port)
        self.m.connect(self.cfg)
        wait_for(lambda: all(s.status()['phase']=='ready' for s in self.m.sessions.values()) and
                 all(len(b)==3 for b in self.m.layouts.values()))

    def tearDown(self):
        self.m.disconnect();self.a.close();self.b.close()
        self.assertIsNone(self.a.error);self.assertIsNone(self.b.error)

    def test_actual_surface_service_templates_and_both_source_addresses(self):
        wait_for(lambda:self.a.udp_peer and self.b.udp_peer)
        self.assertEqual((self.a.peer[0],self.a.udp_peer[0]),('127.0.0.2','127.0.0.2'))
        self.assertEqual((self.b.peer[0],self.b.udp_peer[0]),('127.0.0.3','127.0.0.3'))
        for rack in (self.a,self.b):
            obj=rack.objects['Bank Switcher Manager']
            rs=[f for f in rack.requests if f['a']==obj]
            self.assertEqual([f['target'] for f in rs],[6,8,23,24,24,24])
            payload=bytes.fromhex(rs[2]['payload'])
            self.assertEqual(payload[6:],bytes.fromhex('01 00 00 10 01 00 00 10 01 00 00 00 03 00 ff 00 00 10 0d 00 00 10 0d 00 00 00 07 00 ff 00 00 10 18 00 00 10 18 00 00 00 06 00 ff'))

    def test_bank_both_directions_dynamic_ids_and_echo(self):
        self.m.start();name='Bank Switcher Manager'
        self.a.emit(name,0x1018,b'\x01\x02\x03')
        wait_for(lambda:len(self.b.writes)==1)
        f=self.b.writes[0]
        self.assertEqual((f['op'],f['a'],f['b'],f['target'],f['payload']),
                         (1,self.b.objects[name],self.b.client,0x1018,'01 02 03'))
        wait_for(lambda:self.m.status()['counts'].get('echo_suppressed',0)>0)
        self.assertFalse(self.a.writes)
        self.b.emit('Channel Left Bank Switcher',0x1001,b'\x01\x02'+b'\0'*22)
        wait_for(lambda:len(self.a.writes)==1)

    def test_scene_show_are_observation_and_stop_drops_bank_changes(self):
        self.m.start()
        self.a.emit('Surface Scene Manager',0x1000,b'not a store command')
        self.a.emit('Library File Manager',0x1002,b'not a show file')
        wait_for(lambda:self.m.sessions['A'].status()['operation_events']>=2)
        self.assertFalse(self.b.writes)
        self.m.stop()
        prior=self.m.sessions['A'].status()['bank_events']
        self.a.emit('Bank Switcher Manager',0x1018,b'\x02')
        wait_for(lambda:self.m.sessions['A'].status()['bank_events']>prior)
        self.assertFalse(self.b.writes)
        self.m.start()
        self.a.emit('Bank Switcher Manager',0x1018,b'\x03')
        wait_for(lambda:len(self.b.writes)==1)
        self.assertEqual(self.b.writes[0]['payload'],'03')
        self.assertIn('not_implemented',self.m.status()['capabilities']['scene_store'])

    def test_experimental_all_mapped_properties_dynamic_ids_and_echo(self):
        self.cfg['surface_experimental']=True
        self.m.start()
        for name,target,payload in [('South Settings Config Manager',0x100c,b'\0\x19'),
                                    ('South Settings Config Manager',0x10ff,b'opaque unknown')]:
            prior=len(self.b.writes)
            self.a.emit(name,target,payload)
            wait_for(lambda:len(self.b.writes)>prior)
            got=self.b.writes[-1]
            self.assertEqual((got['op'],got['a'],got['b'],got['target'],got['payload']),
                             (1,self.b.objects[name],self.b.client,target,payload.hex(' ')))
        wait_for(lambda:self.m.status()['counts'].get('echo_suppressed',0)>=2)
        self.assertFalse(self.a.writes)
        self.b.emit('South Settings Config Manager',0x100c,b'\0\x20')
        wait_for(lambda:len(self.a.writes)==1)
        self.assertEqual(self.a.writes[0]['a'],self.a.objects['South Settings Config Manager'])
        self.assertEqual(self.a.writes[0]['b'],self.a.client)
        self.assertEqual(self.a.writes[0]['payload'],'00 20')
        self.assertEqual(self.m.status()['counts']['experimental_sent'],3)
        self.assertTrue(self.m.status()['experimental'])

    def test_experimental_direction_stop_and_service_responses(self):
        self.cfg.update(surface_experimental=True,direction='A_to_B')
        self.m.start();name='South Settings Config Manager'
        self.b.emit(name,0x100c,b'\0\x12')
        self.a.emit(name,24,b'ack')
        self.a.emit(name,0x105,b'query response')
        wait_for(lambda:self.m.sessions['B'].status()['operation_events']>=1)
        self.assertFalse(self.a.writes);self.assertFalse(self.b.writes)
        self.m.stop()
        self.a.emit(name,0x100c,b'\0\x13')
        wait_for(lambda:self.m.sessions['A'].status()['operation_events']>=1)
        self.assertFalse(self.b.writes)
        self.m.start();self.a.emit(name,0x100c,b'\0\x14')
        wait_for(lambda:len(self.b.writes)==1)
        self.assertEqual(self.b.writes[0]['payload'],'00 14')

    def test_extended_controls_are_subscribed_but_observed_only(self):
        self.m.disconnect()
        for rack in (self.a,self.b):
            # Fresh native mock connections are required after disconnect.
            rack.close()
        self.a=SurfaceMock(0x330);self.b=SurfaceMock(0x430)
        for rack,base in ((self.a,0x830),(self.b,0x930)):
            for i,spec in enumerate(EXTRA['objects']):rack.objects[spec['name']]=base+i
        self.cfg.update(surface_experimental=True,surface_controls_observe=True)
        for side,rack in (('A',self.a),('B',self.b)):
            self.cfg[side].update(surface_tcp=rack.port,surface_udp=rack.udp_port)
        self.m.connect(self.cfg)
        wait_for(lambda:all(s.status()['phase']=='ready' for s in self.m.sessions.values()))
        self.m.start()
        self.assertEqual(self.m.sessions['A'].status()['objects_total'],11)
        self.assertFalse(self.m.sessions['A'].status()['missing'])
        self.a.emit('Surface Soft Controls',0x1034,b'unknown control event')
        wait_for(lambda:self.m.sessions['A'].status()['operation_events']>=1)
        self.assertFalse(self.b.writes)

    def test_direction_replaced_session_and_connection_loss(self):
        self.cfg['direction']='A_to_B';self.m.start()
        self.b.emit('Bank Switcher Manager',0x1018,b'\x03')
        wait_for(lambda:self.m.sessions['B'].status()['bank_events']>=1)
        self.assertFalse(self.a.writes)
        old=self.m.sessions['A']
        self.m.property('A','Bank Switcher Manager',dict(target=0x1018,payload='03'),old.generation,source=object())
        self.assertFalse(self.b.writes)
        self.b.close()
        wait_for(lambda:not self.m.status()['running'])
        self.assertIsNotNone(self.m.status()['error'])


class FourEndpointTests(unittest.TestCase):
    def test_engine_observe_four_endpoints_and_stop_all(self):
        a=CapturingRack('DM64');b=CapturingRack('DM64');a.start();b.start()
        sa=SurfaceMock(0x530);sb=SurfaceMock(0x630)
        with tempfile.TemporaryDirectory() as tmp:
            e=DirectMirror(pathlib.Path(tmp)/'connection.json',log_dir=tmp)
            try:
                cfg=copy.deepcopy(DEFAULT_CONFIG);cfg.update(probe='none',surface_observe=True,surface_banks=True)
                # Distinct loopback aliases preserve real four-device validation.
                for side,rack,surf,ip,sip,local in [('A',a,sa,'127.0.0.1','127.0.0.2','127.0.0.5'),('B',b,sb,'127.0.0.3','127.0.0.4','127.0.0.6')]:
                    # These mock listeners bind 127.0.0.1, so override the factory
                    # target after validation through controlled test constructors.
                    cfg[side].update(ip=ip,surface_ip=sip,local_ip=local,tcp=rack.port,udp=rack.udp_port,surface_tcp=surf.port,surface_udp=surf.udp_port)
                original=e.factory
                e.factory=lambda side,c,*args:original(side,{**c,'ip':'127.0.0.1'},*args)
                sf=e.surfaces.factory
                e.surfaces.factory=lambda side,c,*args:sf(side,{**c,'surface_ip':'127.0.0.1'},*args)
                e.connect(cfg)
                wait_for(lambda:all(s.status()['phase']=='ready' for s in e.surfaces.sessions.values()) and
                         all(len(b)==3 for b in e.surfaces.layouts.values()))
                e.start();self.assertTrue(e.status()['surfaces']['running'])
                e.stop();self.assertFalse(e.status()['surfaces']['running'])
                e.disconnect();self.assertFalse(e.status()['surfaces']['sides'])
                self.assertEqual(len(e.status()['surfaces']['last_sides']),2)
            finally:e.close();a.stop();b.stop();sa.close();sb.close()

    def test_surface_options_validate(self):
        cfg=copy.deepcopy(DEFAULT_CONFIG);cfg['surface_banks']=True
        with self.assertRaises(ValueError):validate_config(cfg)
        cfg['surface_banks']=False;cfg['surface_experimental']=True
        with self.assertRaises(ValueError):validate_config(cfg)
        cfg['surface_observe']=True;cfg['B']['surface_ip']=cfg['A']['surface_ip']
        with self.assertRaises(ValueError):validate_config(cfg)
