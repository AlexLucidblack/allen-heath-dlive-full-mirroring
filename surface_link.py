"""Independent Surface op=1 sessions, using observed 2.12 initialization.

Default: bank writes only. Opt-in experimental mode relays all received mapped
properties. Property relay does not establish Scene/Show Store semantics.
"""
import collections
import errno
import json
import pathlib
import secrets
import socket
import threading
import time
from ahnet_lab import frame, make_direct_hello
from legacy_lab import decode_raw
from layout_map import ORDER, width, plan_changes

DATA = json.loads((pathlib.Path(__file__).parent / 'surface_subscriptions.json').read_text())
EXTRA = json.loads((pathlib.Path(__file__).parent / 'surface_control_subscriptions.json').read_text())
OBSERVE_ONLY = {spec['name'] for spec in EXTRA['objects']}
LOCAL_STORAGE = {'Surface Scene Manager','Scene Manager Intermediate','Library File Manager'}
BANKS = {'Bank Switcher Manager', 'Channel Left Bank Switcher',
         'Channel Middle Bank Switcher', 'Channel Right Bank Switcher'}


class SurfaceSession:
    def __init__(self, side, cfg, callback, log):
        self.side = side; self.cfg = cfg; self.callback = callback; self.log = log
        self.specs=DATA['objects']+(EXTRA['objects'] if cfg.get('surface_controls_observe') else [])
        self.lock = threading.RLock(); self.tx_lock = threading.Lock()
        self.cancel = threading.Event(); self.reply = threading.Event()
        self.sock = None; self.udp = None; self.thread = None
        self.by_name = {}; self.by_id = {}; self.pending = None; self.assigned = None
        self.client = secrets.randbelow(65000) + 100
        self.identity = None; self.generation = 0;self.last_receive=time.monotonic()
        self.state = dict(connected=False, phase='disconnected', identity=None,
                          identity_registration_ack=False,
                          objects_done=0, objects_total=len(self.specs),
                          properties=0, bank_events=0, operation_events=0,
                          errors=[], missing=[], registration_acks=0)
        self.state.update(udp_transient_errors=0, udp_last_error=None)

    def status(self):
        with self.lock:
            return {**self.state, 'silence_seconds':round(time.monotonic()-self.last_receive,3), 'objects':dict(self.by_name), 'generation':self.generation,
                    'errors':list(self.state['errors']), 'missing':list(self.state['missing']),
                    'ip':self.cfg['surface_ip'], 'local_ip':self.cfg.get('local_ip','')}

    def connect(self):
        self.disconnect()
        with self.lock:
            self.last_receive=time.monotonic()
            self.generation += 1; self.cancel = threading.Event(); self.reply = threading.Event()
            self.by_id.clear(); self.by_name.clear(); self.pending = None
            self.identity = None; self.client = secrets.randbelow(65000) + 100
            self.state.update(phase='connecting', connected=False, errors=[], missing=[],
                              objects_done=0, properties=0, bank_events=0, operation_events=0,
                              registration_acks=0, identity=None)
            self.state.update(udp_transient_errors=0, udp_last_error=None)
            self.state['identity_registration_ack']=False
        local = self.cfg.get('local_ip','')
        try:
            self.sock = socket.create_connection((self.cfg['surface_ip'],self.cfg.get('surface_tcp',51321)),
                         timeout=4, source_address=(local,0) if local else None)
            self.sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            self.sock.settimeout(.5)
            self.udp = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            self.udp.bind((local or '0.0.0.0',0)); self.udp.settimeout(.2)
            self.udp.connect((self.cfg['surface_ip'],self.cfg.get('surface_udp',51327)))
            gen=self.generation
            self.thread=threading.Thread(target=self._run,args=(gen,self.sock,self.udp,self.cancel),daemon=True)
            self.thread.start()
        except OSError:
            self.disconnect(); raise
        return self.status()

    def _send(self, raw):
        with self.tx_lock:
            if self.cancel.is_set() or not self.sock:
                raise RuntimeError('Surface disconnected')
            self.sock.sendall(raw)
        self.log('surface_tcp_outgoing',self.side,hex=raw.hex(' '),n=len(raw))

    def _f1(self, obj, recipient, target, payload):
        self._send(frame(1,obj,recipient,target,payload))

    def _lookup(self, name):
        with self.lock:
            self.pending = name; self.assigned = None; self.reply.clear()
        self._f1(0,3,4,(name+'\0').encode('ascii'))
        deadline=time.monotonic()+8
        while not self.reply.wait(.05):
            if self.cancel.is_set():raise RuntimeError('Surface disconnected')
            if time.monotonic()>deadline:
                # Assignment replies carry no name. Never continue after timeout.
                with self.lock:self.pending=None
                raise RuntimeError('Surface lookup timeout: '+name)
        if self.cancel.is_set() or self.assigned is None:
            raise RuntimeError('Surface disconnected during lookup')
        return self.assigned

    def _observe(self, f):
        hook=getattr(self,'on_frame',None)
        if hook and hook(self.side,f,self):return
        value=bytes.fromhex(f['payload'])
        with self.lock:
            if f['op']==3 and f['a']==self.client and f['b']==0 and f['target']==2 and len(value)==4:
                self.identity=int.from_bytes(value,'big')
                return
            if f['op']==3 and f['a']==3 and f['b']==0 and f['target']==2 and len(value)==4 and self.pending:
                self.assigned=int.from_bytes(value,'big')
                if 0<self.assigned<65536:
                    self.by_name[self.pending]=self.assigned;self.by_id[self.assigned]=self.pending
                self.pending=None;self.reply.set();return
            if f['op']!=3 or f['a']!=self.client:return
            if self.identity and f['b']==self.identity and f['target']==8:
                self.state['identity_registration_ack']=True
                return
            if self.identity and f['b']==self.identity and f['target']==2 and b'Surface' in value:
                self.state.update(connected=True,identity=value.rstrip(b'\0').decode('ascii',errors='replace'))
                return
            name=self.by_id.get(f['b'])
            if not name:return
            if f['target'] in (8,24):self.state['registration_acks']+=1
            if f['target']>=0x1000:
                self.state['properties']+=1
                self.state['bank_events' if name in BANKS else 'operation_events']+=1
                gen=self.generation
            else:return
        self.log('surface_property',self.side,name=name,target=hex(f['target']),
                 payload=f['payload'],category='bank' if name in BANKS else ('surface_control_observation' if name in OBSERVE_ONLY else 'scene_show_observation'))
        f = {**f, '_received_monotonic':time.monotonic()}
        self.callback(self.side,name,f,gen,source=self)

    def _run(self, gen, sock, udp, cancel):
        # Import lazily to avoid a module cycle with the engine.
        from allen_heath_dlive_full_mirroring import WireSplitter
        splitter=WireSplitter()
        def receive():
            try:data=sock.recv(65535)
            except socket.timeout:return
            if not data:raise RuntimeError('Surface closed TCP')
            self.last_receive=time.monotonic()
            self.log('surface_tcp_incoming',self.side,hex=data.hex(' '),n=len(data))
            for kind,raw in splitter.feed(data):
                if kind=='f1':self._observe(decode_raw(raw))
        def heartbeat():
            next_send=0
            try:
                while not cancel.is_set() and gen==self.generation:
                    try:
                        if time.monotonic()>=next_send:
                            next_send=time.monotonic()+1
                            udp.send(b'\xe0\x00\x01\x03\xe7')
                        data=udp.recv(65535)
                    except socket.timeout:continue
                    except OSError as ex:
                        # Connected UDP reports delayed ICMP refusal while the
                        # rebooted Surface has already opened its TCP service.
                        # Keep TCP discovery alive and retry UDP independently.
                        if ex.errno not in (errno.ECONNREFUSED, errno.ECONNRESET):
                            raise
                        if cancel.is_set() or gen!=self.generation:return
                        with self.lock:
                            self.state['udp_transient_errors']+=1
                            self.state['udp_last_error']=str(ex)
                        self.log('surface_udp_retry',self.side,error=str(ex))
                        cancel.wait(.2)
                        continue
                    self.last_receive=time.monotonic()
                    with self.lock:self.state['udp_last_error']=None
                    self.log('surface_udp_incoming',self.side,hex=data.hex(' '),n=len(data))
                    for kind,raw in WireSplitter().feed(data,datagram=True):
                        if kind=='f1':self._observe(decode_raw(raw))
            except OSError as ex:
                if not cancel.is_set():self._fail(gen,ex)
        heart=None
        try:
            self._send(make_direct_hello(udp.getsockname()[1]))
            tag=b'Surface Identification\0'
            self._send(b'\xf0\x00\x01\x00\x00'+self.client.to_bytes(2,'big')+b'\x00\x04'+len(tag).to_bytes(2,'big')+tag+b'\xf7')
            heart=threading.Thread(target=heartbeat,daemon=True);heart.start()
            deadline=time.monotonic()+8
            while not self.identity:
                if cancel.is_set():return
                if time.monotonic()>deadline:raise RuntimeError('Surface identity assignment timeout')
                receive()
            self._f1(self.identity,self.client,0x100,b'')
            while not self.status()['connected']:
                if cancel.is_set():return
                if time.monotonic()>deadline:raise RuntimeError('Surface identity response timeout')
                receive()
            # Observed identity subscription and synchronization request.
            registration=b'\x00'*4+self.client.to_bytes(2,'big')+bytes.fromhex('00 00 10 03 00 00 10 03 00 ff')
            self._f1(self.identity,3,6,registration)
            self._f1(self.identity,self.client,8,b'')
            # A separate reader allows serialized root discovery to wait safely.
            def reader():
                try:
                    while not cancel.is_set() and gen==self.generation:receive()
                except (OSError,RuntimeError,ValueError) as ex:
                    if not cancel.is_set():self._fail(gen,ex)
            self.reader=threading.Thread(target=reader,daemon=True);self.reader.start()
            with self.lock:self.state['phase']='subscribing'
            for spec in self.specs:
                if cancel.is_set():return
                obj=self._lookup(spec['name'])
                if not 0<obj<65536:
                    with self.lock:self.state['missing'].append(spec['name'])
                else:
                    for req in spec['requests']:
                        target=req['target'];payload=bytes.fromhex(req['payload'])
                        if target in (6,23):payload=payload[:4]+self.client.to_bytes(2,'big')+payload[6:]
                        recipient=0xffffffff if target==23 else (3 if target==6 else self.client)
                        self._f1(obj,recipient,target,payload)
                with self.lock:self.state['objects_done']+=1
            if not self.status()['identity_registration_ack']:
                raise RuntimeError('Surface identity registration ACK missing')
            with self.lock:self.state['phase']='ready'
            while not cancel.wait(.2):
                if gen!=self.generation:return
        except (OSError,RuntimeError,ValueError) as ex:
            if not cancel.is_set():self._fail(gen,ex)
        finally:
            cancel.set()
            for s in (sock,udp):
                try:s.close()
                except OSError:pass
            if heart:heart.join(timeout=1)
            with self.lock:
                if gen==self.generation:self.state['connected']=False

    def _fail(self,gen,ex):
        with self.lock:
            if gen!=self.generation:return
            self.state.update(connected=False,phase='error');self.state['errors'].append(str(ex))
        self.log('surface_error',self.side,error=str(ex));self.cancel.set();self.reply.set()

    def write_bank(self,name,target,payload,generation):
        # No generic RPC replay: Scene/Show status is never a Store operation.
        with self.lock:
            if generation!=self.generation or not self.state['connected'] or self.state['phase']!='ready':
                raise RuntimeError('Surface session not ready or replaced')
            obj=self.by_name.get(name)
            if name not in BANKS or not obj or target<0x1000:
                raise RuntimeError('No bank object mapping on destination')
            self._f1(obj,self.client,target,payload)

    def write_property(self,name,target,payload,generation):
        # Experimental: no method allowlist, but require a destination object
        # discovered by name. Never replay connection/registration envelopes.
        with self.lock:
            if generation!=self.generation or not self.state['connected'] or self.state['phase']!='ready':
                raise RuntimeError('Surface session not ready or replaced')
            obj=self.by_name.get(name)
            if not obj or target<0x1000:
                raise RuntimeError('No destination property mapping')
            self._f1(obj,self.client,target,payload)

    def request_bank_readback(self,name,generation):
        # Observed 0x08 synchronization request after the 0x06 subscription.
        # A fresh explicit request does not depend on write-origin echo behavior.
        with self.lock:
            if generation!=self.generation or not self.state['connected'] or self.state['phase']!='ready':
                raise RuntimeError('Surface session not ready or replaced')
            obj=self.by_name.get(name)
            if name not in ORDER or not obj:raise RuntimeError('No layout mapping')
            self._f1(obj,self.client,8,b'')

    def disconnect(self):
        self.cancel.set();self.reply.set()
        for s in (self.sock,self.udp):
            if s:
                try:s.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                try:s.close()
                except OSError:pass
        if self.thread and self.thread is not threading.current_thread():self.thread.join(timeout=1)
        with self.lock:
            self.sock=None;self.udp=None;self.pending=None
            self.state.update(connected=False,phase='disconnected')


class SurfaceMirror:
    def __init__(self, log, session_factory=SurfaceSession):
        self.log=log;self.factory=session_factory;self.sessions={};self.lock=threading.RLock()
        self.cfg={};self.running=False;self.expected={};self.last={};self.counts=collections.Counter()
        self.last_sessions={};self.error=None;self.started_at=0
        self.layouts={'A':{},'B':{}};self.pending_layouts={}
        self.unconfirmed_layouts={};self.warning=None
        self.last_error=None;self.last_state={}

    def _fault(self,kind,message,**fields):
        self.running=False;self.expected.clear();self.error=message
        self.last_error=dict(t=time.time(),type=kind,error=message,**fields)
        self.log(kind,error=message,**fields)

    def _unconfirmed(self,key,value,reason):
        self.unconfirmed_layouts[key]=value
        self.warning='Раскладка отправлена, подтверждение чтением не получено'
        self.counts['layout_unconfirmed']+=1
        self.log('surface_layout_unconfirmed',key[0],name=key[1],reason=reason,verified=False)

    def connect(self,cfg):
        self.disconnect();self.cfg=cfg
        result={}
        for side in ('A','B'):
            s=self.factory(side,{**cfg[side],'surface_controls_observe':cfg.get('surface_controls_observe',False)},self.property,self.log);self.sessions[side]=s
            s.on_frame=getattr(self,'on_frame',None)
            try:result[side]=s.connect()
            except (OSError,RuntimeError) as ex:
                result[side]={'error':str(ex)};self._fault('surface_connect_error',str(ex),side=side)
        return result

    def start(self):
        with self.lock:
            if self.error:
                raise RuntimeError('Переподключи Surface после ошибки: '+self.error)
            if len(self.sessions)!=2 or any(not s.status()['connected'] or s.status()['phase']!='ready' for s in self.sessions.values()):
                raise RuntimeError('Дождись подключения и подписок обеих Surface')
            if any(n not in self.layouts[side] for side in ('A','B') for n in ORDER):
                raise RuntimeError('Дождись исходных раскладок трёх банков обеих Surface')
            self.expected.clear();self.last.clear();self.started_at=time.monotonic();self.running=True

    def stop(self):
        with self.lock:
            self.running=False;self.expected.clear()
            for key,value in self.pending_layouts.items():self._unconfirmed(key,value,'stopped before readback')
            self.pending_layouts.clear()

    def property(self,side,name,f,generation,source=None):
        with self.lock:
            from safety_filter import phantom
            if source is not None and source is not self.sessions.get(side):return
            if name in LOCAL_STORAGE:
                self.log('storage_event_local',side,name=name,target=hex(f['target']));return
            if phantom(name,f['target'],bytes.fromhex(f['payload'])):
                self.log('phantom_blocked',side,name=name);return
            handler=getattr(self,'recovery_handler',None)
            if handler and handler(side,name,f,source):return
            if (self.cfg.get('scene_sync') and name=='Surface Scene Manager' and f['target']==0x100c):
                if (side in self.sessions and (source is None or source is self.sessions[side]) and
                        generation==self.sessions[side].generation and len(bytes.fromhex(f['payload']))==2):
                    callback=getattr(self,'on_scene',None)
                    if callback:callback(side,int.from_bytes(bytes.fromhex(f['payload']),'big'))
                return
            if name in OBSERVE_ONLY:return # diagnostics only; write semantics not established
            experimental=self.cfg.get('surface_experimental',False)
            if (name not in BANKS and not experimental) or side not in self.sessions:return
            if f.get('op',3)!=3 or f.get('a',self.sessions[side].client)!=self.sessions[side].client or f['target']<0x1000:return
            if source is not None and self.sessions.get(side) is not source:return
            if self.sessions[side].generation!=generation:return
            dest='B' if side=='A' else 'A';now=time.monotonic()
            self.expected={k:v for k,v in self.expected.items() if v>now}
            sig=(name,f['target'],f['payload'])
            is_layout=name in ORDER and f['target']==0x1001
            pending=self.pending_layouts.get((side,name))
            unconfirmed=self.unconfirmed_layouts.get((side,name))
            confirmation=pending or unconfirmed
            if is_layout and confirmation and confirmation['payload']==f['payload']:
                self.pending_layouts.pop((side,name),None)
                self.unconfirmed_layouts.pop((side,name),None)
                if not self.unconfirmed_layouts:self.warning=None
                self.counts['layout_readback_matched']+=1
                self.log('surface_layout_readback_matched',side,name=name)
                return
            if self.expected.get((side,sig),0)>now:
                self.counts['echo_suppressed']+=1
                # An older echo must not rewind the latest pending layout.
                return
            if is_layout and pending:
                self._fault('surface_layout_readback_mismatch',
                            'Полученная раскладка Surface отличается от отправленной',
                            side=side,name=name,expected=pending['payload'],actual=f['payload'])
                self.pending_layouts.clear();self.unconfirmed_layouts.pop((side,name),None)
            before=None
            if is_layout:
                value=bytes.fromhex(f['payload'])
                try:width(value)
                except ValueError as ex:
                    self._fault('surface_layout_invalid',str(ex),side=side);return
                before=self.layouts[side].get(name)
                self.layouts[side][name]=value
            if not self.running:return
            if f.get('_received_monotonic',self.started_at)<self.started_at:return
            if self.cfg['direction'] in ({'A':'B_to_A','B':'A_to_B'}[side],):return
            target=self.sessions[dest]
            if name not in target.by_name:
                self.counts['unmapped_dropped']+=1
                self.log('surface_unmapped_dropped',side,name=name,destination=dest)
                return
            key=(side,name,f['target'])
            if self.last.get(key) and self.last[key][0]==f['payload'] and now-self.last[key][1]<.05:
                self.counts['duplicate_suppressed']+=1;return
            self.last[key]=(f['payload'],now)
            try:
                if is_layout:
                    if before is None:return
                    planned,missing,changed=plan_changes(name,before,value,
                                                        self.layouts[side],self.layouts[dest])
                    if missing:
                        self.counts['layout_unmapped_positions']+=len(missing)
                        self.log('surface_layout_unmapped',side,destination=dest,positions=missing,
                                 reason='destination has fewer physical strips')
                    for bank,payload in planned.items():
                        payload_hex=payload.hex(' ')
                        self.expected[(dest,(bank,0x1001,payload_hex))]=now+self.cfg.get('echo_seconds',5)
                        target.write_bank(bank,0x1001,payload,target.generation)
                        self.layouts[dest][bank]=payload
                        self.unconfirmed_layouts.pop((dest,bank),None)
                        if not self.unconfirmed_layouts:self.warning=None
                        self.pending_layouts[(dest,bank)]=dict(payload=payload_hex,at=now)
                        self.counts['sent_'+side+'_to_'+dest]+=1
                        self.log('surface_layout_sent',side,destination=dest,name=bank,
                                 payload=payload_hex,positions=changed,verified=False)
                        target.request_bank_readback(bank,target.generation)
                        self.counts['layout_readback_requested']+=1
                        self.log('surface_layout_readback_requested',dest,name=bank,target='0x8')
                    return
                self.expected[(dest,sig)]=now+self.cfg.get('echo_seconds',5)
                writer=target.write_property if experimental else target.write_bank
                writer(name,f['target'],bytes.fromhex(f['payload']),target.generation)
                self.counts['sent_'+side+'_to_'+dest]+=1
                if experimental:self.counts['experimental_sent']+=1
                self.log('surface_experimental_sent' if experimental else 'surface_bank_sent',side,destination=dest,name=name,target=hex(f['target']),
                         payload=f['payload'],verified=False)
            except (OSError,RuntimeError,ValueError) as ex:
                self._fault('surface_relay_stopped',str(ex),side=side)

    def status(self):
        with self.lock:
            sessions={side:s.status() for side,s in self.sessions.items()}
            if self.running and any(not s['connected'] or s['phase']!='ready' for s in sessions.values()):
                self._fault('surface_connection_lost','Surface connection lost')
            for key,value in list(self.pending_layouts.items()):
                if time.monotonic()-value['at']>3:
                    self.pending_layouts.pop(key)
                    self._unconfirmed(key,value,'readback timeout; no automatic write retry')
            return dict(running=self.running,sides=sessions,last_sides=self.last_sessions,
                        counts=dict(self.counts),error=self.error,warning=self.warning,
                        last_error=self.last_error,last_state=self.last_state,
                        readback_policy='request_current_layout; warn_without_reply; stop_layout_on_mismatch',
                        layout_geometry={side:{n:width(raw) for n,raw in banks.items()} for side,banks in self.layouts.items()},
                        layout_pending=[dict(side=k[0],bank=k[1],age_seconds=round(time.monotonic()-v['at'],3)) for k,v in self.pending_layouts.items()],
                        layout_unconfirmed=[dict(side=k[0],bank=k[1],age_seconds=round(time.monotonic()-v['at'],3)) for k,v in self.unconfirmed_layouts.items()],
                        experimental=self.cfg.get('surface_experimental',False),
                        relay_scope='received_subscribed_properties; base_eight_objects; optional_three_controls_observe_only',
                        capabilities=dict(bank_layout='position_delta_mapping_write_unverified',
                                          scene_store='disabled_not_implemented_discontinued',
                                          show_store='disabled_not_implemented_discontinued'),
                        hardware_verified=False)

    def disconnect(self):
        self.stop()
        if self.sessions:
            self.last_state=dict(error=self.error,warning=self.warning,last_error=self.last_error,
                                 layout_geometry={side:{n:width(raw) for n,raw in banks.items()} for side,banks in self.layouts.items()},
                                 layout_unconfirmed=[dict(side=k[0],bank=k[1]) for k in self.unconfirmed_layouts],
                                 disconnected_at=time.time())
        for side,s in list(self.sessions.items()):
            self.last_sessions[side]=s.status();s.disconnect()
        self.sessions.clear();self.error=None;self.last.clear()
        self.layouts={'A':{},'B':{}};self.pending_layouts.clear()
        self.unconfirmed_layouts.clear();self.warning=None
