import copy,json,pathlib,socket,tempfile,threading,types,unittest
from ahnet_lab import frame,FrameParser
from legacy_lab import decode_raw
from mirror_app import WireSplitter,DEFAULT_CONFIG,validate_config,DirectMirror
from scene_sync import Scene,SceneCollector,SceneRPC,SceneSync,adapt_surface,records
from layout_map import ORDER
from test_direct import wait_for
FIX=json.loads((pathlib.Path(__file__).parent/'saved_scene_fixture.json').read_text())
def sample(kind):
 f=FIX[kind];return Scene(kind,tuple(bytes.fromhex(v) for v in f['headers']),bytes.fromhex(f['body']))
class SceneFormatTests(unittest.TestCase):
 def test_capture_collector_complete_and_malformed(self):
  for k in ('rack','surface'):
   s=sample(k);c=SceneCollector(k)
   for p in s.chunks():c.feed(p)
   self.assertEqual(c.value,s);self.assertEqual(s.name,'Scene 17')
  with self.assertRaises(ValueError):SceneCollector('surface').feed(b'bad')
  c=SceneCollector('surface');s=sample('surface')
  for p in s.headers:c.feed(p)
  with self.assertRaises(ValueError):c.feed(s.body+b'extra')
 def test_saved_banks_map_all_layers_and_preserve_other_records(self):
  s=sample('surface');out,dropped=adapt_surface(s,dict(zip(ORDER,(12,0,12))))
  old=dict(records(s.body));new=dict(records(out.body))
  self.assertEqual(old['LED Brightness'],new['LED Brightness'])
  self.assertEqual([len(new[n]) for n in ORDER],[146,2,146])
  for layer in range(6):
   a=b''.join(old[n][2+layer*w*2:2+(layer+1)*w*2] for n,w in zip(ORDER,(12,8,8)))
   b=b''.join(new[n][2+layer*w*2:2+(layer+1)*w*2] for n,w in zip(ORDER,(12,0,12)))
   self.assertEqual(b,a[:48])
  self.assertEqual(dropped,24);self.assertEqual(int.from_bytes(out.headers[-1],'big'),len(out.body))
  with self.assertRaises(ValueError):records(out.body[:-1])
 def test_large_real_blocks_and_fragmented_frame(self):
  body=bytes.fromhex(FIX['large_block']);self.assertEqual(len(body),35000);raw=frame(3,44,55,2,body)
  for parser in (FrameParser(),WireSplitter()):
   found=[]
   for n in range(0,len(raw),777):found.extend(parser.feed(raw[n:n+777]))
   self.assertEqual(len(found),1)
   f=found[0] if isinstance(parser,FrameParser) else decode_raw(found[0][1])
   self.assertEqual(bytes.fromhex(f['payload']),body)
  payload=b'\xe0\xf1\xf8\xf0'*20000
  self.assertEqual(bytes.fromhex(decode_raw(frame(3,44,55,2,payload))['payload']),payload)
 def test_validation(self):
  c=copy.deepcopy(DEFAULT_CONFIG);c['scene_sync']=True
  self.assertFalse(validate_config(c)['scene_sync'])
  c.update(surface_observe=True,probe='capture_input1')
  self.assertFalse(validate_config(c)['scene_sync'])
  c['probe']='capture_all';self.assertFalse(validate_config(c)['scene_sync'])
class NetworkEndpoint:
 def __init__(self,e,side,kind,obj,client):
  self.e=e;self.side=side;self.kind=kind;self.obj=obj;self.client=client;self._client_id=client
  self.generation=1;self.by_name={'Surface Scene Manager':obj};self.captured=types.SimpleNamespace(id_for=lambda name:obj)
  self.scenes={16:sample(kind)};self.reads=0;self.writes=0;self.corrupt=False;self.connected=True;self.error=None;self.drop_ack=False;self.change_on_read=False
  self.selected=None;self.collect=None;self.stop=threading.Event()
  listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
  self.sock=socket.create_connection(listener.getsockname());self.server,_=listener.accept();listener.close()
  self.sock.settimeout(.1);self.server.settimeout(.1)
  self.st=threading.Thread(target=self.serve,daemon=True);self.rt=threading.Thread(target=self.receive,daemon=True)
  self.st.start();self.rt.start()
 def status(self):return dict(connected=self.connected,phase='ready')
 def send_raw(self,raw,transport):self.sock.sendall(raw)
 def _f1(self,obj,client,target,payload):self.sock.sendall(frame(1,obj,client,target,payload))
 def emit(self,payload):
  raw=frame(3,self.client,self.obj,2,payload)
  for i in range(0,len(raw),8123):self.server.sendall(raw[i:i+8123])
 def receive(self):
  p=FrameParser()
  while not self.stop.is_set():
   try:data=self.sock.recv(65535)
   except socket.timeout:continue
   except OSError:return
   if not data:return
   for f in p.feed(data):
    if self.kind=='rack':self.e.packet(self.side,'tcp',[('f1',bytes.fromhex(f['raw']))],source=self)
    elif not self.e.surfaces.on_frame(self.side,f,self):
     self.e.surfaces.property(self.side,'Surface Scene Manager',f,self.generation,source=self)
 def serve(self):
  p=FrameParser()
  try:
   while not self.stop.is_set():
    try:data=self.server.recv(65535)
    except socket.timeout:continue
    if not data:return
    for f in p.feed(data):
     assert (f['a'],f['b'])==(self.obj,self.client)
     payload=bytes.fromhex(f['payload'])
     if f['target']==0x105:
      self.reads+=1
      scene=self.scenes[int.from_bytes(payload,'big')]
      if self.change_on_read and self.reads==2:
       h=list(scene.headers);h[0]=b'CHANGED'+h[0][7:];scene=Scene(scene.kind,tuple(h),scene.body)
      for b in scene.chunks():self.emit(b)
     elif f['target']==0x107:self.selected=int.from_bytes(payload,'big');self.collect=SceneCollector(self.kind)
     elif f['target']==0x108:
      s=self.collect.feed(payload)
      if s is not None:
       self.writes+=1
       if self.corrupt:s=Scene(s.kind,s.headers,s.body[:-1]+bytes([s.body[-1]^1]))
       self.scenes[self.selected]=s
       if self.kind=='surface':self.server.sendall(frame(3,self.client,self.obj,0x100c,self.selected.to_bytes(2,'big')))
       if not self.drop_ack:self.emit(b'')
       self.collect=None
  except (OSError,AssertionError,ValueError) as ex:
   if not self.stop.is_set():self.error=str(ex)
 def close(self):
  self.stop.set();self.connected=False
  for s in (self.sock,self.server):
   try:s.shutdown(socket.SHUT_RDWR)
   except OSError:pass
   s.close()
  self.st.join(timeout=1);self.rt.join(timeout=1)
class SceneNetworkTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.logs=[]
  e=DirectMirror(pathlib.Path(self.tmp.name)/'connection.json',log_dir=self.tmp.name)
  e.cfg.update(scene_sync=True,direction='A_to_B');e.running=True;e.epoch=1;e.surfaces.cfg=e.cfg
  self.e=e;self.endpoints=[]
  for i,side in enumerate('AB'):
   for j,kind in enumerate(('rack','surface')):
    ep=NetworkEndpoint(e,side,kind,100+i*10+j,200+i*10+j);self.endpoints.append(ep)
    if kind=='rack':e.bridges[side]=ep
    else:e.surfaces.sessions[side]=ep
   e.surfaces.layouts[side]={n:b'\0'*(12*w) for n,w in zip(ORDER,(12,8,8) if side=='A' else (12,0,12))}
  for ep in self.endpoints[2:]:
   s=ep.scenes[16];h=list(s.headers);h[0]=b'OLD'+h[0][3:];ep.scenes[16]=Scene(ep.kind,tuple(h),s.body)
 def tearDown(self):
  self.e.running=False;self.e.epoch+=1;self.e.redundancy.close();self.e.scene_sync.close();self.e.closed.set();self.e.worker.join(timeout=1)
  for ep in self.endpoints:ep.close();self.assertIsNone(ep.error)
  self.tmp.cleanup()
 def test_complete_copy_both_parts_dynamic_ids_backup_echo_and_reverse(self):
  sync=self.e.scene_sync;sync.updated('A',16)
  wait_for(lambda:sync.status()['phase'] in ('ready','error','partial'),6)
  self.assertEqual(sync.status()['phase'],'ready',sync.status())
  self.assertEqual(self.e.bridges['B'].scenes[16],sample('rack'))
  out,_=adapt_surface(sample('surface'),dict(zip(ORDER,(12,0,12))))
  self.assertEqual(self.e.surfaces.sessions['B'].scenes[16],out)
  result=sync.status()['last_result'];self.assertTrue(result['readback_verified'])
  backup=json.loads(pathlib.Path(result['backup']).read_text())
  self.assertTrue(bytes.fromhex(backup['parts']['rack']['headers'][0]).startswith(b'OLD'))
  self.assertEqual(self.e.bridges['A'].reads,2);self.assertFalse(sync.pending)
  self.e.cfg['direction']='both'
  with sync.lock:sync.expected.clear()
  sync.updated('B',16)
  wait_for(lambda:sync.status()['counts'].get('verified',0)==2,6)
  self.assertEqual(self.e.bridges['A'].writes,1)
 def test_readback_mismatch_is_partial_and_mixrack_continues(self):
  self.e.bridges['B'].corrupt=True;s=self.e.scene_sync;s.updated('A',16)
  wait_for(lambda:s.status()['phase']=='partial',6)
  self.assertIn('отличается',s.status()['error']);self.assertTrue(self.e.running)
  s.updated('A',17);self.assertFalse(s.pending)
 def test_source_changes_during_read_prevent_all_destination_writes(self):
  self.e.bridges['A'].change_on_read=True;s=self.e.scene_sync;s.updated('A',16)
  wait_for(lambda:s.status()['phase']=='error',6)
  self.assertIn('изменилась',s.status()['error'])
  self.assertTrue(all(ep.writes==0 for ep in self.endpoints))
 def test_missing_write_ack_leaves_backup_and_marks_partial(self):
  s=self.e.scene_sync;dest=self.e.surfaces.sessions['B'];dest.drop_ack=True
  s._endpoint('B','surface',1).timeout=.15
  s.updated('A',16)
  wait_for(lambda:s.status()['phase']=='partial',6)
  self.assertTrue(pathlib.Path(s.status()['backup']).is_file())
  self.assertEqual(dest.writes,1);self.assertEqual(self.e.bridges['B'].writes,0)
  self.assertTrue(self.e.running)
  with self.assertRaises(RuntimeError):s.require_ready()
 def test_direction_stop_and_epoch_drop_jobs(self):
  s=self.e.scene_sync;s.updated('B',16);self.assertFalse(s.pending)
  s.updated('A',16);s.stop();self.e.running=False;self.e.epoch+=1
  self.assertFalse(s.pending);self.assertTrue(all(ep.writes==0 for ep in self.endpoints))
class SceneRPCTests(unittest.TestCase):
 def test_timeout_quarantines_late_answer(self):
  requests=[];r=SceneRPC('surface',44,55,lambda *a:requests.append(a),lambda:None,timeout=.01)
  with self.assertRaises(RuntimeError):r.read(16)
  self.assertTrue(r.poisoned);self.assertFalse(r.observe(dict(op=3,a=55,b=44,target=2,payload='00')))
  with self.assertRaises(RuntimeError):r.read(16)
  self.assertEqual(len(requests),1)
