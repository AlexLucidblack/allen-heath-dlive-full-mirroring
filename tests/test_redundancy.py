import copy,json,pathlib,tempfile,time,types,unittest
from unittest.mock import patch
from ahnet_lab import frame
from legacy_lab import decode_raw
from mirror_app import DirectMirror,DEFAULT_CONFIG,validate_config,WireSplitter
from redundancy import Journal
from layout_map import ORDER
from safety_filter import phantom,preserve_scene_preamps
from scene_sync import Scene,records
from test_direct import wait_for

NAMES=('Input Mixer Module, Number 01','StageBox Analogue Input, Number 01','FX Manager 04','StageBox Scene Manager')
class FakeRack:
 serial=0
 def __init__(self,side,cfg,callback,log):
  FakeRack.serial+=1;self.side=side;self.target=cfg['ip'];self.port=cfg['tcp'];self.udp_port=cfg['udp'];self.callback=callback
  self._generation=FakeRack.serial;self._client_id=2000+self._generation;self._registered_object=None;self._server_object=None
  self.ids={n:10000+self._generation*10+i for i,n in enumerate(NAMES)};self.captured=types.SimpleNamespace(name_for=lambda obj:next((n for n,i in self.ids.items() if i==obj),None),id_for=self.ids.get)
  self.connected=False;self.sent=[];self.udp_live=True
 def serve(self):pass
 def connect(self):self.connected=True;return self.status()
 def disconnect(self):self.connected=False
 def probe_request(self,p):pass
 def probe_status(self):return dict(phase='requests_sent')
 def status(self):return dict(connected=self.connected,udp=dict(running=self.udp_live,since_from_sec=0))
 def send_raw(self,raw,transport):
  if not self.connected:raise OSError('rack down')
  self.sent.append((raw,transport))
 def operator(self,target,payload,name=NAMES[0],transport='tcp'):
  raw=frame(3,self._client_id,self.ids[name],target,payload)
  self.callback(self.side,transport,[('f1',raw)],source=self)
class FakeSurface:
 serial=0
 def __init__(self,side,cfg,callback,log):
  FakeSurface.serial+=1;self.generation=FakeSurface.serial;self.client=3000+self.generation;self.side=side;self.callback=callback
  self.by_name={n:4000+self.generation*10+i for i,n in enumerate((*ORDER,'Surface Scene Manager','Bank Switcher Manager'))}
  self.connected=False;self.silence=0;self.sent=[];self.cfg=cfg;self.on_frame=None
 def status(self):return dict(connected=self.connected,phase='ready' if self.connected else 'disconnected',silence_seconds=self.silence)
 def connect(self):
  self.connected=True
  for n,w in zip(ORDER,(12,8,8) if self.side=='A' else (12,0,12)):self.operator(n,0x1001,b'\xff\xff'*(6*w))
  return self.status()
 def disconnect(self):self.connected=False
 def operator(self,name,target,payload):
  f=decode_raw(frame(3,self.client,self.by_name[name],target,payload));self.callback(self.side,name,f,self.generation,source=self)
 def write_property(self,name,target,payload,generation):
  if not self.connected or generation!=self.generation:raise OSError('surface down')
  self.sent.append((name,target,payload))
 def write_bank(self,*args):self.write_property(*args)

def scene(kind,data):
 body=b''.join((len(n.encode())+1+len(v)).to_bytes(2,'big')+n.encode()+b'\0'+v for n,v in data)
 return Scene(kind,(b'header',len(body).to_bytes(4,'big')),body)

class SafetyTests(unittest.TestCase):
 def test_phantom_identified_exactly_and_gain_pad_not_blocked(self):
  for n in ('StageBox Analogue Input, Number 01','Surface Analogue Input, Number 01'):
   self.assertTrue(phantom(n,0x1003,b'\x01'));self.assertTrue(phantom(n,0x1003,b'\x00'))
   self.assertFalse(phantom(n,0x1002,b'\x01\x02'));self.assertFalse(phantom(n,0x1004,b'\x01'))
  self.assertFalse(phantom('FX Manager 04',0x1003,b'\x01'))
 def test_scene_keeps_destination_full_analogue_records_in_both_parts(self):
  for k in ('rack','surface'):
   n='StageBox Analogue Input, Number 01' if k=='rack' else 'Surface Analogue Input, Number 01'
   src=scene(k,[(n,b'\x01\x1b\x01\x01\x01'),('Fader',b'new')]);dst=scene(k,[(n,b'local'),('Fader',b'old')])
   out,nkept=preserve_scene_preamps(src,dst)
   self.assertEqual(nkept,1);self.assertEqual(dict(records(out.body)),{n:b'local','Fader':b'new'})
   self.assertEqual(int.from_bytes(out.headers[-1],'big'),len(out.body))
   with self.assertRaises(ValueError):preserve_scene_preamps(src,scene(k,[('Fader',b'old')]))
 def test_durable_order_and_uncertain_after_crash(self):
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t)/'journal.sqlite3';j=Journal(p);a=j.append('B',dict(kind='rack',payload='01'));b=j.append('B',dict(kind='rack',payload='02'))
   j.set(a,'sending');j.mark('connection_lost',side='A');j.close();j=Journal(p)
   self.assertEqual(j.first()[0],a);self.assertEqual(j.first()[3],'uncertain');j.set(a,'sent');self.assertEqual(j.first()[0],b);j.close()

class RedundancyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.ping=patch('redundancy.ping',lambda *a:dict(ok=False,error='ICMP filtered'));self.ping.start()
  self.e=DirectMirror(pathlib.Path(self.tmp.name)/'connection.json',bridge_factory=FakeRack,log_dir=self.tmp.name)
  self.e.surfaces.factory=FakeSurface
  c=copy.deepcopy(DEFAULT_CONFIG);c.update(direction='A_to_B',probe='capture_all',auto_redundancy=True,surface_observe=True,surface_banks=True,recovery_compact=False)
  self.e.connect(c);self.e.start();self.r=self.e.redundancy
  # Prevent the background reconnect while testing the outage journal, then release it explicitly.
  self.r.retry_at={s:time.monotonic()+100 for s in 'AB'}
 def tearDown(self):self.e.close();self.ping.stop();self.tmp.cleanup()
 def frames(self,s):return [decode_raw(frame) for raw,tr in self.e.bridges[s].sent for kind,frame in WireSplitter().feed(raw,datagram=True) if kind=='f1']
 def loss(self,side,kind):
  if kind in ('rack','both'):self.e.bridges[side].connected=False
  if kind in ('surface','both'):self.e.surfaces.sessions[side].connected=False
 def reconnect(self,side):
  old=self.e.bridges[side];surface=self.e.surfaces.sessions[side];self.r.retry_at[side]=0
  wait_for(lambda:(self.e.bridges[side] is not old or self.e.surfaces.sessions[side] is not surface) and self.r._ready(side),5)
 def test_ping_four_endpoints_and_icmp_filtered_does_not_fail_native(self):
  wait_for(lambda:len(self.r.pings)==4,3);self.assertEqual(self.r.master,'A');self.assertTrue(all(self.r.health.values()))
 def test_live_named_commands_and_48v_udp_tcp_both_roles(self):
  for side,dest in [('A','B'),('B','A')]:
   if side=='B':
    self.loss('A','surface');wait_for(lambda:self.r.master=='B')
    self.reconnect('A')
   b=self.e.bridges[side]
   for tr in ('udp','tcp'):
    for val in (b'\x00',b'\x01'):b.operator(0x1003,val,NAMES[1],tr)
   b.operator(0x1002,b'\x00\x20',NAMES[1]);b.operator(0x1004,b'\x01',NAMES[1]);b.operator(0x1280,b'\x55\x66')
   wait_for(lambda:len(self.frames(dest))>=3)
   self.assertFalse(any(f['target']==0x1003 for f in self.frames(dest)))
   self.assertEqual([f['target'] for f in self.frames(dest)][-3:],[0x1002,0x1004,0x1280])
   self.assertEqual(self.frames(dest)[-1]['a'],self.e.bridges[dest].ids[NAMES[0]])
 def test_failover_reconnect_order_and_reverse_second_loss(self):
  for failed,master,kind in [('A','B','surface'),('B','A','rack'),('A','B','both')]:
   old=self.e.bridges[failed];oldid=old.ids[NAMES[0]];old.sent.clear()
   self.loss(failed,kind);wait_for(lambda:self.r.master==master)
   self.assertEqual(self.e.cfg['direction'],master+'_to_'+failed)
   for v in range(12):self.e.bridges[master].operator(0x1280,v.to_bytes(2,'big'))
   self.assertFalse(self.frames(failed))
   self.reconnect(failed);wait_for(lambda:len(self.frames(failed))==12)
   fs=self.frames(failed);self.assertEqual([int.from_bytes(bytes.fromhex(f['payload']),'big') for f in fs],list(range(12)))
   self.assertTrue(all(f['a']==self.e.bridges[failed].ids[NAMES[0]] for f in fs))
   if kind=='surface':self.assertIs(self.e.bridges[failed],old)
   else:self.assertNotEqual(self.e.bridges[failed].ids[NAMES[0]],oldid)
   self.assertEqual(self.r.master,master);self.r.retry_at[failed]=time.monotonic()+100
 def test_unknown_bytes_keep_transport_live_and_block_after_loss(self):
  raw=b'opaque';self.e.packet('A','udp',[('opaque',raw)],source=self.e.bridges['A']);wait_for(lambda:self.e.bridges['B'].sent==[(raw,'udp')])
  self.e.bridges['B'].sent.clear()
  self.loss('B','surface');wait_for(lambda:not self.r.health['B'])
  self.e.packet('A','tcp',[('opaque',b'unknown_after_loss')],source=self.e.bridges['A']);self.reconnect('B')
  wait_for(lambda:bool(self.r.error));self.assertEqual(self.r.phase,'blocked')
  self.assertFalse(self.e.bridges['B'].sent)
 def test_unmapped_phantom_candidate_is_blocked_and_other_unknown_passes(self):
  self.e.packet('A','tcp',[('f1',frame(2,12345,54321,0x1003,b'\x01'))],source=self.e.bridges['A'])
  self.e.bridges['A'].operator(0x1280,b'\x01\x02');wait_for(lambda:len(self.frames('B'))==1)
  self.assertEqual(self.frames('B')[0]['target'],0x1280)
 def test_strip_all_layers_replayed_on_fresh_geometry(self):
  self.loss('A','surface');wait_for(lambda:self.r.master=='B')
  s=self.e.surfaces.sessions['B'];v=bytearray(self.r.layouts['B'][ORDER[2]])
  for layer in range(6):v[(layer*12+4)*2:(layer*12+4)*2+2]=bytes((layer,42))
  s.operator(ORDER[2],0x1001,bytes(v));self.reconnect('A')
  wait_for(lambda:bool(self.e.surfaces.sessions['A'].sent))
  n,t,p=self.e.surfaces.sessions['A'].sent[0];self.assertEqual(n,ORDER[1])
  for layer in range(6):self.assertEqual(p[(layer*8+4)*2:(layer*8+4)*2+2],bytes((layer,42)))
 def test_both_unavailable_requires_explicit_rearm_and_manual_stop_prevents_retry(self):
  self.loss('A','both');self.loss('B','both');wait_for(lambda:self.r.both_lost)
  self.reconnect('A');self.assertTrue(self.r.both_lost);self.assertEqual(self.r.phase,'both_unavailable')
  self.e.stop();a=self.e.bridges['A'];self.loss('A','both');self.r.retry_at['A']=0;time.sleep(.5)
  self.assertIs(self.e.bridges['A'],a);self.assertFalse(self.r.active)
 def test_stale_session_does_not_record_after_reconnect(self):
  old=self.e.bridges['A'];self.loss('A','both');wait_for(lambda:self.r.master=='B');self.reconnect('A')
  before=self.r.journal.stats();old.operator(0x1280,b'\x09\x09');self.assertEqual(self.r.journal.stats(),before)
 def test_udp_heartbeat_failure_switches_without_tcp_close(self):
  self.e.bridges['A'].udp_live=False;wait_for(lambda:self.r.master=='B');self.assertTrue(self.e.bridges['A'].connected)
 def test_validation_requires_capture_and_surfaces(self):
  c=copy.deepcopy(DEFAULT_CONFIG);c.update(auto_redundancy=True,direction='both')
  with self.assertRaises(ValueError):validate_config(c)

 def test_first_surviving_action_elects_master_before_monitor_tick(self):
  self.loss('A','surface');self.e.bridges['B'].operator(0x1280,b'\x22\x33')
  self.assertEqual(self.r.master,'B');self.assertEqual(self.r.journal.first()[1],'B')
 def test_parameter_partial_send_retries_state_on_new_connection(self):
  b=self.e.bridges['B'];native=b.send_raw
  def fail(raw,tr):
   native(raw,tr);b.connected=False;raise OSError('connection lost after write')
  b.send_raw=fail
  self.e.bridges['A'].operator(0x1280,b'\x11\x22')
  wait_for(lambda:self.r.journal.first() and not b.connected)
  self.assertIsNone(self.r.error);self.reconnect('B')
  wait_for(lambda:self.r.journal.stats().get('sent')==1)
  self.assertEqual(self.frames('B')[0]['payload'],'11 22')
 def test_late_standby_layout_echo_does_not_erase_newer_positions(self):
  self.loss('A','surface');wait_for(lambda:self.r.master=='B');self.reconnect('A')
  b=self.e.surfaces.sessions['B'];a=self.e.surfaces.sessions['A'];name=ORDER[2]
  v=bytearray(self.r.layouts['B'][name])
  for pos in (4,5):
   v[pos*2:pos*2+2]=bytes((pos,42));b.operator(name,0x1001,bytes(v))
  wait_for(lambda:len(a.sent)>=2)
  bank,target,old=a.sent[0];a.operator(bank,target,old)
  v[12:14]=bytes((6,42));b.operator(name,0x1001,bytes(v))
  wait_for(lambda:len(a.sent)>=3)
  final=a.sent[-1][2]
  for pos in (4,5,6):self.assertEqual(final[pos*2:pos*2+2],bytes((pos,42)))

class RedundancySceneTCPTests(unittest.TestCase):
 def setUp(self):
  from test_scenes import SceneNetworkTests
  self.fixture=SceneNetworkTests();self.fixture.setUp();self.e=self.fixture.e;self.r=self.e.redundancy
  self.e.cfg.update(auto_redundancy=True,scene_sync=True,direction='A_to_B')
  for side in 'AB':
   for kind,endpoint in (('rack',self.e.bridges[side]),('surface',self.e.surfaces.sessions[side])):
    original=endpoint.scenes[16];name=('StageBox' if kind=='rack' else 'Surface')+' Analogue Input, Number 01'
    raw=name.encode()+b'\0'+(b'source48V' if side=='A' else b'LOCAL48V')
    body=original.body+len(raw).to_bytes(2,'big')+raw
    endpoint.scenes[16]=Scene(kind,(*original.headers[:-1],len(body).to_bytes(4,'big')),body)
  for b in self.e.bridges.values():b.probe_status=lambda:dict(phase='requests_sent');b.status=lambda b=b:dict(connected=b.connected,udp=dict(running=True,since_from_sec=0))
  self.r.retry_at={s:time.monotonic()+100 for s in 'AB'}
  self.r.start()
 def tearDown(self):self.fixture.tearDown()
 def test_discontinued_store_paths_do_not_read_write_or_queue(self):
  for side in 'AB':
   session=self.e.surfaces.sessions[side]
   for name in ('Surface Scene Manager','Scene Manager Intermediate','Library File Manager'):
    self.r.surface(side,name,decode_raw(frame(3,1,2,0x100c,(16).to_bytes(2,'big'))),session)
  self.assertIsNone(self.r.journal.first())
  self.assertFalse(any(ep.reads or ep.writes for ep in self.fixture.endpoints))
