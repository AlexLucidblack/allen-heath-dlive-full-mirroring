#!/usr/bin/env python3
"""dLive AH-Net Full Mirror 0.2.1: one REAL MixRack + one VIRTUAL rack.

No streaming audio, no GigaACE, no OS packet interception. Receives ALL F1 frames
visible to independent AH-Net sessions, tracks UDP datagrams, and mirrors to a
software rack. Management/session framing remains local to each connection.
"""
import argparse
import collections
import datetime as dt
import hmac
import json
import pathlib
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from ahnet_lab import DirectBridge, FrameParser, frame, ITEMS

ROOT = pathlib.Path(__file__).resolve().parent
LOGDIR = ROOT / 'logs'
LOGDIR.mkdir(exist_ok=True)


def canonical(f):
    return (min(f['a'],f['b']), max(f['a'],f['b']),f['target'],f['payload'])


def decode_raw(raw):
    p=FrameParser().feed(raw)
    if len(p)!=1 or bytes.fromhex(p[0]['raw'])!=raw:
        raise ValueError('Expected exactly one valid F1 frame')
    return p[0]


class VirtualMixRack:
    """Protocol mock for AH-Net transport and F1 state only; NOT DSP emulation."""
    def __init__(self, port=52321, udp_port=52327, host='127.0.0.1', on_frame=None, on_udp=None):
        self.host=host; self.port=port; self.udp_port=udp_port
        self.on_frame=on_frame or (lambda *_: None)
        self.on_udp=on_udp or (lambda *_: None)
        self.tcp=None;self.udp=None;self.running=threading.Event()
        self.lock=threading.RLock();self.sessions={};self.state={}
        self.stats=collections.Counter();self.errors=[]
    def start(self):
        tcp=socket.socket();tcp.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        tcp.bind((self.host,self.port));tcp.listen(12);tcp.settimeout(.4)
        self.port=tcp.getsockname()[1]
        udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);udp.bind((self.host,self.udp_port));udp.settimeout(.4)
        self.udp_port=udp.getsockname()[1]
        self.tcp=tcp;self.udp=udp;self.running.set()
        threading.Thread(target=self._accept,daemon=True,name='virtual-tcp-accept').start()
        threading.Thread(target=self._udp_loop,daemon=True,name='virtual-udp').start()
    def _accept(self):
        while self.running.is_set():
            try:conn,peer=self.tcp.accept()
            except socket.timeout:continue
            except OSError:break
            conn.settimeout(.4)
            with self.lock:
                self.sessions[conn]={'addr':peer,'client_id':None,'udp_port':None,'writer':threading.Lock(),'connected':time.time()}
                self.stats['connections']+=1
            threading.Thread(target=self._handle,args=(conn,),daemon=True,name='virtual-tcp-client').start()
    def _send(self,c,raw):
        with self.lock:s=self.sessions.get(c)
        if s is None:return False
        try:
            with s['writer']:c.sendall(raw)
            with self.lock:self.stats['tcp_tx']+=1
            return True
        except OSError:return False
    def _broadcast(self,raw):
        with self.lock:peers=list(self.sessions)
        for p in peers:self._send(p,raw)
    def _handle(self,c):
        parser=FrameParser();hello=bytearray();handshake_complete=False
        try:
            while self.running.is_set():
                try:d=c.recv(65535)
                except socket.timeout:continue
                if not d:break
                if not handshake_complete:
                    hello.extend(d)
                    anchor=hello.find(b'DR Box Identification\x00\xf7')
                    if anchor<0:
                        if len(hello)>16384:break
                        continue
                    end=anchor+len(b'DR Box Identification\x00\xf7')
                    start=hello.rfind(b'\xf0\x00\x02\x00\x00',0,anchor)
                    if start<0 or start+7>len(hello):break
                    client_id=int.from_bytes(hello[start+5:start+7],'big')
                    udp_port=None
                    e0=hello.find(b'\xe0\x00\x04\x01\x03')
                    if e0>=0 and e0+8<=len(hello):udp_port=int.from_bytes(hello[e0+5:e0+7],'big')
                    with self.lock:
                        self.sessions[c]['client_id']=client_id
                        self.sessions[c]['udp_port']=udp_port
                        self.stats['handshakes']+=1
                    handshake_complete=True
                    self._send(c,frame(3,client_id,0,2,(7).to_bytes(4,'big')))
                    d=bytes(hello[end:]);hello.clear()
                for f in parser.feed(d):self._process(c,f)
        except Exception as ex:
            with self.lock:self.errors.append(str(ex))
        finally:
            with self.lock:self.sessions.pop(c,None)
            try:c.close()
            except OSError:pass
    def _process(self,c,f):
        raw=bytes.fromhex(f['payload'])
        with self.lock:self.stats['tcp_rx']+=1
        # Mocked responses based on PCAP #001 and Command Lab's working handshake.
        if f['op']==2 and f['a']==0 and f['b']==3 and f['target']==4:
            names={b'DR Box Identification\x00':7,b'Channel Selector Manager\x00':0x1e12,
                   b'Channel Mapper\x00':0x55,b'Audio Clock Source\x00':0x1e16}
            assigned=names.get(raw, 7)
            self._send(c,frame(3,3,0,2,assigned.to_bytes(4,'big')))
        elif f['op']==2 and f['a']==7 and f['target']==0x100:
            self._send(c,frame(3,f['b'],7,2,b'TLDDM64Stagebox V2.12 - Rev. 94091\x00'))
        elif f['op']==2 and f['b']==3 and f['target']==6 and f['a'] in (7,0x1e12,0x55):
            if len(raw)!=16:
                with self.lock:self.stats['invalid_registrations']+=1
                return
            with self.lock:self.stats['registrations']+=1
            self._send(c,frame(3,int.from_bytes(raw[4:6],'big') if len(raw)>=6 else 1,f['a'],8,b''))
        elif f['target']>=0x1000:
            with self.lock:
                self.state[(min(f['a'],f['b']),max(f['a'],f['b']),f['target'])]=f['payload']
                self.stats['state_updates']+=1
            # A virtual echo is intentionally broadcast: mirror must suppress it.
            answer=(frame(3,f['b'],f['a'],f['target'],raw) if f['op']==2 else bytes.fromhex(f['raw']))
            self._broadcast(answer)
        self.on_frame(f)
    def mutate(self,f):
        """Simulate operator changing state on the virtual MixRack."""
        if f['target']<0x1000:raise ValueError('Virtual write must target parameter area >= 0x1000')
        raw=bytes.fromhex(f['payload'])
        with self.lock:
            self.state[(min(f['a'],f['b']),max(f['a'],f['b']),f['target'])]=f['payload']
            self.stats['virtual_operator_actions']+=1
        # MixRack's canonical notification echoes a changed state with op=3.
        update=frame(3,f['b'],f['a'],f['target'],raw)
        self._broadcast(update)
        return update
    def _udp_loop(self):
        while self.running.is_set():
            try:d,source=self.udp.recvfrom(65535)
            except socket.timeout:continue
            except OSError:break
            with self.lock:self.stats['udp_rx']+=1
            self.on_udp(d,source)
            if d==b'\xe0\x00\x01\x03\xe7':
                # 5-byte heartbeat request gets 18-byte heartbeat response.
                try:
                    self.udp.sendto(d+b'\x00'*13,source)
                    with self.lock:self.stats['udp_tx']+=1
                except OSError:pass
            elif d.startswith(b'\xf1'):
                # Preserve UDP transport for genuine parameter datagrams.
                for f in FrameParser().feed(d):
                    self.on_frame(f)
                    if f['target']<0x1000:continue
                    with self.lock:
                        key=(min(f['a'],f['b']),max(f['a'],f['b']),f['target'])
                        self.state[key]=f['payload'];self.stats['state_updates']+=1
                        self.stats['udp_f1_rx']+=1
                    raw=bytes.fromhex(f['raw'])
                    echo=(frame(3,f['b'],f['a'],f['target'],bytes.fromhex(f['payload'])) if f['op']==2 else raw)
                    try:
                        self.udp.sendto(echo,source)
                        with self.lock:self.stats['udp_tx']+=1
                    except OSError:pass
    def status(self):
        with self.lock:
            return {'listening':self.running.is_set(),'tcp_port':self.port,'udp_port':self.udp_port,
                    'clients':len(self.sessions),'state_keys':len(self.state),
                    'counters':dict(self.stats),'errors':list(self.errors[-5:]),
                    'sample_state':[{'a':hex(k[0]),'b':hex(k[1]),'target':hex(k[2]),'payload':v} for k,v in list(self.state.items())[-8:]]}
    def stop(self):
        self.running.clear()
        for s in (self.tcp,self.udp):
            if s:
                try:s.close()
                except OSError:pass
        with self.lock:all_s=list(self.sessions)
        for c in all_s:
            try:c.close()
            except OSError:pass


class ObservingBridge(DirectBridge):
    def __init__(self,side,target,port,udp_port,on_frame,on_datagram):
        super().__init__(target,port,udp_port)
        self.side=side;self._mirror_frame_callback=on_frame;self._mirror_udp_callback=on_datagram
        self._mirror_local=threading.local()
        self.init_rx_probe()
    def connect(self):
        self.init_rx_probe()
        return super().connect()
    def init_rx_probe(self):
        self.probe_lock=threading.RLock()
        self.probe={'phase':'idle','name':None,'assigned_object':None,'ack_count':0,
                    'registrations_sent':0,'expected_acks':0,'errors':[],
                    'source':'Original Director capture #001; links are address pairs, NOT ranges',
                    'evidence':{'Channel Selector Manager':['0x1043','0x1000'],
                                'Channel Mapper':['0x1003','0x10f7']}}
        self._probe_pending_name=None
        self._probe_objects=set()
    def probe_request(self,mode='selector'):
        if mode not in ('selector','mapper8','mapper32'):
            raise ValueError('Unsupported RX probe profile')
        if not self.status()['connected']:raise RuntimeError('First connect AH-Net side')
        if not hasattr(self,'probe_lock'):self.init_rx_probe()
        with self.probe_lock:
            if self.probe['phase'] not in ('idle','complete','error'):
                raise RuntimeError('RX probe already running')
            name='Channel Selector Manager' if mode=='selector' else 'Channel Mapper'
            self.probe={'phase':'awaiting_assignment','name':name,'profile':mode,
                        'assigned_object':None,'ack_count':0,'registrations_sent':0,
                        'expected_acks':1 if mode=='selector' else (8 if mode=='mapper8' else 32),
                        'errors':[],'requested_at':time.time(),'first_nonservice':None,
                        'deadline_monotonic':time.monotonic()+10,'payload_bytes':16}
            self._probe_pending_name=name
        raw=frame(2,0,3,4,(name+'\x00').encode('ascii'))
        self.send_f1(raw)
        return dict(self.probe)
    def probe_status(self):
        if not hasattr(self,'probe_lock'):self.init_rx_probe()
        with self.probe_lock:
            if (self.probe['phase'] in ('awaiting_assignment','awaiting_ack') and
                time.monotonic()>self.probe.get('deadline_monotonic',float('inf'))):
                self.probe['phase']='error'
                self.probe['errors'].append('Registration timed out after 10 seconds; ACK is not DSP feedback')
            return dict(self.probe)
    def _handle_probe_frame(self,f):
        if not hasattr(self,'probe_lock'):self.init_rx_probe()
        with self.probe_lock:
            state=self.probe
            if (state['phase']=='awaiting_assignment' and f['op']==3 and f['a']==3 and
                f['b']==0 and f['target']==2 and len(bytes.fromhex(f['payload']))==4):
                assigned=int.from_bytes(bytes.fromhex(f['payload']),'big')
                if not 0<assigned<65536:return
                state['assigned_object']=assigned;self._probe_objects.add(assigned)
                self._probe_pending_name=None
                if state['profile']=='selector':pairs=[(0x1043,0x1000)]
                else:pairs=[(0x1003+i,0x10f7+i) for i in range(state['expected_acks'])]
                # Addresses are recorded *links* from original Director capture #001;
                # not contiguous DSP parameters. Register a bounded number of pairs.
                registration_frames=[]
                for left,right in pairs:
                    payload=(b'\x00'*4+self._client_id.to_bytes(2,'big')+
                             left.to_bytes(4,'big')+right.to_bytes(4,'big')+b'\x00\xff')
                    registration_frames.append(frame(2,assigned,3,6,payload))
                state['phase']='awaiting_ack'
                state['deadline_monotonic']=time.monotonic()+10
                state['registrations_sent']=len(registration_frames)
            elif (state['phase']=='awaiting_ack' and f['op']==3 and f['a']==self._client_id
                  and f['b']==state['assigned_object'] and f['target']==8):
                state['ack_count']+=1
                if state['ack_count']>=state['expected_acks']:
                    state['phase']='complete';state['ack_at']=time.time()
                return
            else:
                if (state.get('assigned_object') is not None and f['target']>=0x1000
                    and state['phase'] in ('awaiting_ack','complete') and
                    f['a']==self._client_id and f['b']==state['assigned_object']):
                    state['first_nonservice']=state.get('first_nonservice') or {'target':hex(f['target']),'payload':f['payload'][:80]}
                return
        # Send outside of probe_lock to avoid blocking status calls.
        for raw in registration_frames:
            try:self.send_f1(raw)
            except Exception as ex:
                with self.probe_lock:
                    state['phase']='error';state['errors'].append(str(ex))
                break
    def observe_raw_tcp(self,data,direction='incoming'):
        self._mirror_udp_callback(self.side,'tcp_'+direction,data)
    def _read_tcp(self,*args):
        self._mirror_local.transport='tcp'
        return super()._read_tcp(*args)
    def observe_event(self,f,direction):
        # Base TCP thread calls this after handling identity and registration.
        # Our discovery/manager registration is independent of Base's optional RX watch.
        if direction=='from_dm64':self._handle_probe_frame(f)
        super().observe_event(f,direction)
        if direction=='from_dm64':
            self._mirror_frame_callback(self.side,f,getattr(self._mirror_local,'transport','tcp'))
    def _run_udp(self,gen,s):
        marker=b'\xe0\x00\x01\x03\xe7'
        try:
            with self.lock:
                if gen!=self._generation or self._closing.is_set():return
                self._udp_stats['running']=True;self._udp_stats['error']=None
            next_send=0.0;
            while not self._closing.is_set() and gen==self._generation:
                if time.monotonic()>=next_send:
                    try:
                        s.send(marker)
                        with self.lock:self._udp_stats['to_dm64']+=1;self._udp_stats['last_to']=time.time()
                        self._mirror_udp_callback(self.side,'outgoing',marker)
                    except OSError as ex:
                        with self.lock:self._udp_stats['error']=str(ex)
                    next_send=time.monotonic()+1
                try:d=s.recv(65535)
                except socket.timeout:continue
                except OSError as ex:
                    if not self._closing.is_set():self.emit('direct_udp_receive_error',error=str(ex))
                    break
                with self.lock:
                    self._udp_stats['from_dm64']+=1;self._udp_stats['last_from']=time.time()
                self._mirror_udp_callback(self.side,'incoming',d)
                self._mirror_local.transport='udp'
                for f in FrameParser().feed(d):self.observe_event(f,'from_dm64')
        finally:
            with self.lock:
                if self._udp_sock is s:self._udp_sock=None;self._udp_stats['running']=False
            try:s.close()
            except OSError:pass
    def send_f1(self,raw):
        decode_raw(raw)
        with self.lock:s=self._tx_sock;alive=self._identified.is_set() and bool(self.active)
        if not alive or s is None:raise RuntimeError('AH-Net side '+self.side+' is not connected')
        with self._tx_lock:self._send_tcp(s,raw)


class MirrorEngine:
    def __init__(self,dm_ip='192.168.1.70',virtual_tcp_port=52321,virtual_udp_port=52327,real_port=51321,real_udp_port=51327,mock_a=False,surface_ip='192.168.1.71'):
        self.surface_ip=surface_ip
        self.lock=threading.RLock();self.counter=collections.Counter();self.events=collections.deque(maxlen=250)
        self.expected={};self._last_expire=0;self.started=time.time();self.log_path=LOGDIR/('mirror_'+dt.datetime.now().strftime('%Y%m%d_%H%M%S')+'.jsonl')
        self.log_error=None;self.arm_until=0;self._last_real_tx=0.0;self.mode='shadow'
        self.mock_a=bool(mock_a);self.mock_source=None
        self.rx_window=None;self.rx_samples=collections.deque(maxlen=150);self.recent_a_observed=collections.deque(maxlen=120)
        self.virtual=VirtualMixRack(port=virtual_tcp_port,udp_port=virtual_udp_port,on_frame=self._virtual_ingress,on_udp=self._virtual_udp)
        self.virtual.start()
        if self.mock_a:
            self.mock_source=VirtualMixRack(port=0,udp_port=0,on_frame=lambda *_:None)
            self.mock_source.start()
            dm_ip='127.0.0.1';real_port=self.mock_source.port;real_udp_port=self.mock_source.udp_port
        self.A=ObservingBridge('A',dm_ip,real_port,real_udp_port,self._on_frame,self._on_udp)
        self.B=ObservingBridge('B','127.0.0.1',self.virtual.port,self.virtual.udp_port,self._on_frame,self._on_udp)
        self.B.serve();self.A.serve()
        self.B.connect()
        if self.mock_a:self.connect_real()
    def _event(self,kind,side=None,**kwargs):
        record={'t':round(time.time(),3),'type':kind,'side':side,**kwargs}
        with self.lock:
            self.events.append(record);self.counter[kind]+=1
            try:
                with self.log_path.open('a',encoding='utf-8') as out:
                    out.write(json.dumps(record,ensure_ascii=False)+'\n')
            except OSError as ex:self.log_error=str(ex)
        return record
    def _expire(self):
        now=time.monotonic()
        if now-self._last_expire<0.5:return
        self._last_expire=now
        for k,v in list(self.expected.items()):
            if v<now:del self.expected[k]
    def _expect(self,side,signature):
        with self.lock:self._expire();self.expected[(side,signature)]=time.monotonic()+1.5
    def _consume_echo(self,side,signature):
        with self.lock:
            self._expire()
            # Suppress ALL duplicate notifications in the short echo window,
            # not only the first packet. An unchanged value is idempotent.
            expiry=self.expected.get((side,signature),0)
            return expiry>=time.monotonic()
    def _classify_frame(self,side,f):
        bridge=self.A if side=='A' else self.B
        a,b=f['a'],f['b']
        if f['target']<0x1000:return 'rpc'
        if bridge._client_id is not None and bridge._client_id in (a,b):
            if bridge._registered_object in (a,b):return 'own_handshake'
            if a in getattr(bridge,'_probe_objects',set()) or b in getattr(bridge,'_probe_objects',set()):
                return 'manager_notification'
        return 'parameter_candidate'
    def _is_local_service(self,side,f):
        return self._classify_frame(side,f)!='parameter_candidate'
    def _on_frame(self,side,f,transport):
        sig=canonical(f)
        is_echo=self._consume_echo(side,sig)
        # Manager notifications may contain useful control state; retain them in
        # the RX observation window, but never blindly replay them to another rack.
        a,b=f['a'],f['b'];cl=self._classify_frame(side,f)
        self._event('received_f1',side,transport=transport,op=f['op'],a=hex(a),b=hex(b),
                    target=hex(f['target']),n=len(bytes.fromhex(f['payload'])),hex=f['raw'],frame_class=cl,echo=is_echo)
        if is_echo:
            self._event('echo_suppressed',side,transport=transport,target=hex(f['target']))
            return
        if cl in ('rpc','own_handshake'):
            self._event('session_local',side,target=hex(f['target']),reason=cl)
            return
        if side=='A':
            with self.lock:
                sample={'t':round(time.time(),3),'target':hex(f['target']),
                        'a':hex(a),'b':hex(b),'op':f['op'],'transport':transport,
                        'payload':f['payload'][:192],'mock':self.mock_a,'class':cl}
                self.recent_a_observed.append(sample)
                if self.rx_window is not None and time.time()<self.rx_window['ends']:
                    self.rx_samples.append(sample)
                elif self.rx_window is not None and self.rx_window['active']:
                    self.rx_window['active']=False
            if cl=='manager_notification':
                self._event('a_manager_notification','A',target=hex(f['target']),mock=self.mock_a)
                return
            self._event('a_parameter_candidate','A',target=hex(f['target']),mock=self.mock_a)
        elif cl=='manager_notification':
            self._event('manager_notification','B',target=hex(f['target']))
            return
        if side=='A':
            # UDP was already mirrored as the complete original datagram by _on_udp.
            if transport=='udp':return
            # Full byte preservation for observed TCP F1 parameter frames.
            try:
                self._expect('B',sig)
                self.B.send_f1(bytes.fromhex(f['raw']))
                self._event('mirrored_to_virtual','A',target=hex(f['target']),n=len(bytes.fromhex(f['raw'])))
            except Exception as ex:self._event('mirror_error','A',error=str(ex))
        else:
            self._handle_virtual_to_real(f)
    def _handle_virtual_to_real(self,f):
        # Virtual rack sends op=3 notifications. Real rack accepts op=2 writes.
        raw=frame(2,f['b'],f['a'],f['target'],bytes.fromhex(f['payload']))
        if self.mode!='live' or time.monotonic()>self.arm_until:
            self._event('virtual_to_real_shadow','B',target=hex(f['target']),raw_write=raw.hex(' '))
            return
        # Bounded rate, explicit short arm; no automated attempt to transmit
        # arbitrary management F1 to live hardware.
        if time.monotonic()-self._last_real_tx<0.17:
            self._event('live_rate_limited','B',target=hex(f['target']))
            return
        try:
            self._expect('A',canonical(f))
            self.A.send_f1(raw)
            self._last_real_tx=time.monotonic()
            self._event('live_to_dm64','B',target=hex(f['target']),hex=raw.hex(' '))
        except Exception as ex:self._event('live_tx_error','B',error=str(ex))
    def _virtual_ingress(self,f):
        if f['target']>=0x1000:
            self._event('virtual_server_received','B',op=f['op'],target=hex(f['target']),n=len(bytes.fromhex(f['payload'])))
    def _virtual_udp(self,d,addr):
        self._event('virtual_udp_rx','B',n=len(d),hex=d[:32].hex(' '))
    def _on_udp(self,side,direction,d):
        if direction.startswith('tcp_'):
            self._event('tcp_chunk',side,direction=direction[4:],n=len(d),hex=d.hex(' '))
            return
        self._event('udp_datagram',side,direction=direction,n=len(d),hex=d.hex(' '))
        # E0 keepalives are local per socket; real F1 UDP datagrams travel by UDP.
        if side=='A' and direction=='incoming' and d.startswith(b'\xf1'):
            frames=FrameParser().feed(d)
            # Unlike protocol-local heartbeats, all original payload bytes remain intact.
            if (not frames or b''.join(bytes.fromhex(f['raw']) for f in frames)!=d or
                any(self._is_local_service(side,f) or self._consume_echo(side,canonical(f)) for f in frames)):
                return
            try:
                for f in frames:self._expect('B',canonical(f))
                with self.B.lock:b_udp=self.B._udp_sock
                if b_udp is None:raise RuntimeError('Virtual B UDP disconnected')
                b_udp.send(d)
                self._event('mirrored_udp_datagram','A',n=len(d),frames=len(frames),hex=d.hex(' '))
            except Exception as ex:self._event('mirror_error','A',transport='udp',error=str(ex))
    def connect_real(self):
        x=self.A.connect();self._event('real_connect','A',result=x)
        return x
    def disconnect_real(self):
        self.mode='shadow';self.log_error=None;self.arm_until=0;self.A.disconnect();self._event('real_disconnect','A')
    def mock_a_write(self,item_id,sample_index=0):
        if not self.mock_a or self.mock_source is None:
            raise RuntimeError('Mock A is not enabled. Start with --mock-a')
        if not self.A.status()['connected']:raise RuntimeError('Mock A client is disconnected')
        it=ITEMS.get(item_id)
        if not it:raise ValueError('Unknown catalog item')
        samples=it.get('samples') or []
        ix=int(sample_index)
        if not 0<=ix<len(samples):raise ValueError('Invalid captured value index')
        pay=bytes.fromhex(samples[ix]);obj_a=int(it['obj_a'],16);obj_b=int(it['obj_b'],16)
        self.mock_source.mutate({'a':obj_a,'b':obj_b,'target':int(it['target'],16),'payload':pay.hex(' ')})
        self._event('mock_a_operator','A',item_id=item_id,sample_index=ix)
        return {'mock_a_generated':True,'item':item_id,'sample':ix}
    def start_rx_window(self,seconds=20,label='Input 1 fader/mute'):
        seconds=max(5,min(60,int(seconds)))
        with self.lock:
            now=time.time();self.rx_samples.clear()
            self.rx_window={'started':now,'ends':now+seconds,'active':True,
                            'label':str(label)[:100], 'start_received_f1':self.counter.get('received_f1',0),
                            'start_candidates':self.counter.get('a_parameter_candidate',0)}
        self._event('rx_window_started','A',seconds=seconds,label=label)
        return {'seconds':seconds,'label':label}
    def probe_real(self,profile='selector'):
        result=self.A.probe_request(profile)
        self._event('rx_probe_started','A',profile=profile)
        return result
    def virtual_write(self,item_id, sample_index=0):
        it=ITEMS.get(item_id)
        if not it:raise ValueError('Unknown catalog item')
        samples=it.get('samples') or []
        i=int(sample_index)
        if not 0<=i<len(samples):raise ValueError('Bad sample index')
        blob=bytes.fromhex(samples[i])
        if len(blob)!=it['n']:raise ValueError('Catalog sample length mismatch')
        obj_a=int(it['obj_a'],16);obj_b=int(it['obj_b'],16);target=int(it['target'],16)
        return self.virtual.mutate({'a':obj_a,'b':obj_b,'target':target,'payload':blob.hex(' ')})
    def replay(self,count=50):
        n=max(1,min(int(count),500))
        valid=[x for x in ITEMS.values() if x.get('samples') and int(x['target'],16)>=0x1000]
        for it in valid[:n]:
            data=bytes.fromhex(it['samples'][0])
            if len(data)!=it['n']:continue
            f={'op':3,'a':int(it['obj_b'],16),'b':int(it['obj_a'],16),'target':int(it['target'],16),'payload':data.hex(' ')}
            f['raw']=frame(f['op'],f['a'],f['b'],f['target'],data).hex(' ')
            self._event('simulated_A_f1','A',target=hex(f['target']),hex=f['raw'])
            self._on_frame('A',f,'offline_capture')
        return {'requested':n,'virtual_state_keys':self.virtual.status()['state_keys']}
    def enable_live(self,phrase):
        if not hmac.compare_digest(str(phrase).encode('utf-8'),'РАЗРЕШАЮ ТЕСТ DM64'.encode('utf-8')):
            raise PermissionError('Incorrect confirmation phrase')
        if not self.A.status()['connected']:raise RuntimeError('Real DM64 not connected')
        self.mode='live';self.arm_until=time.monotonic()+60
        self._event('experimental_live_arm','A',seconds=60)
        return {'mode':self.mode,'seconds':60}
    def disable_live(self):
        self.mode='shadow';self.log_error=None;self.arm_until=0;self._event('experimental_live_off','A')
    def status(self):
        a=self.A.status();b=self.B.status()
        with self.lock:
            return {'version':'0.2.2','surface':{'model':'S5000','ip':self.surface_ip,'role':'reference_only'},'mock_a':self.mock_a,'A':{'connected':a['connected'],'identity':a['endpoint_state'],'udp':a['udp'],
                                       'registration':a['registration'],'rx':a.get('live_receive',{}),'ip':self.A.target,'tcp_port':self.A.port,'udp_port':self.A.udp_port,'rx_probe':self.A.probe_status()},
                    'B':{'ip':self.B.target,'tcp_port':self.B.port,'udp_port':self.B.udp_port,'connected':b['connected'],'identity':b['endpoint_state'],'udp':b['udp'],'registration':b['registration']},
                    'virtual':self.virtual.status(),'counts':dict(self.counter),'events':list(self.events)[-85:],
                    'rx_window':(dict(self.rx_window,active=self.rx_window['active'] and time.time()<self.rx_window['ends'],
                                     count=len(self.rx_samples), samples=list(self.rx_samples)[-45:]) if self.rx_window else None),
                    'a_candidates':list(self.recent_a_observed)[-25:],
                    'mode': 'live' if self.mode=='live' and time.monotonic()<=self.arm_until else 'shadow',
                    'live_sec_left':max(0,round(self.arm_until-time.monotonic(),1)) if self.mode=='live' else 0,
                    'log':str(self.log_path),'log_error':self.log_error,'uptime_sec':round(time.time()-self.started,1)}
    def close(self):
        self.disable_live();self.A.disconnect();self.B.disconnect();self.virtual.stop()
        if self.mock_source:self.mock_source.stop()


ENGINE=None
CSRF_TOKEN=secrets.token_urlsafe(22)
class Handler(BaseHTTPRequestHandler):
    server_version='AHNetFullMirror/0.2.2'
    def log_message(self,fmt,*args):pass
    def _send(self,status,obj,content_type='application/json; charset=utf-8'):
        if isinstance(obj,(dict,list)):blob=json.dumps(obj,ensure_ascii=False).encode()
        elif isinstance(obj,str):blob=obj.encode()
        else:blob=obj
        self.send_response(status);self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(blob)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers();self.wfile.write(blob)
    def do_GET(self):
        url=urllib.parse.urlsplit(self.path)
        if url.path=='/':
            html=(ROOT/'index.html').read_text(encoding='utf-8').replace('__TOKEN__',CSRF_TOKEN)
            return self._send(200,html,'text/html; charset=utf-8')
        if url.path=='/api/status':return self._send(200,ENGINE.status())
        if url.path=='/api/catalog':
            return self._send(200,[{'id':x['id'],'name':x['name'],'group':x['group'],'samples':len(x.get('samples',[]))} for x in ITEMS.values()])
        if url.path=='/api/traffic':
            with ENGINE.lock:
                blob=ENGINE.log_path.read_bytes() if ENGINE.log_path.exists() else b''
            return self._send(200,blob,'application/x-ndjson; charset=utf-8')
        if url.path=='/api/diagnostics':return self._send(200,ENGINE.status())
        return self._send(404,{'error':'Not found'})
    def do_POST(self):
        if self.headers.get('X-Mirror-Token')!=CSRF_TOKEN:
            return self._send(403,{'ok':False,'error':'Request token mismatch'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n>8192 or n<0:raise ValueError('Request too large')
            data=json.loads(self.rfile.read(n) or b'{}')
            path=urllib.parse.urlsplit(self.path).path
            if path=='/api/connect':out=ENGINE.connect_real()
            elif path=='/api/disconnect':ENGINE.disconnect_real();out={'disconnected':True}
            elif path=='/api/probe':out=ENGINE.probe_real(str(data.get('profile','selector')))
            elif path=='/api/rx_window':out=ENGINE.start_rx_window(data.get('seconds',20),data.get('label','Input 1 fader/mute'))
            elif path=='/api/mock_a_write':out=ENGINE.mock_a_write(str(data['id']),int(data.get('sample',0)))
            elif path=='/api/replay':out=ENGINE.replay(data.get('count',50))
            elif path=='/api/virtual_write':
                raw=ENGINE.virtual_write(str(data['id']),int(data.get('sample',0)))
                out={'sent_to_virtual_clients':True,'frame_hex':raw.hex(' ')}
            elif path=='/api/arm':out=ENGINE.enable_live(data.get('phrase',''))
            elif path=='/api/disarm':ENGINE.disable_live();out={'mode':'shadow'}
            else:return self._send(404,{'ok':False,'error':'Unknown action'})
            self._send(200,{'ok':True,'result':out})
        except (ValueError,KeyError,OSError,RuntimeError,PermissionError) as ex:
            self._send(400,{'ok':False,'error':str(ex)})


def main():
    global ENGINE
    p=argparse.ArgumentParser()
    p.add_argument('--dm64',default='192.168.1.70')
    p.add_argument('--surface',default='192.168.1.71',help='Surface IP for display and diagnostics; no connection is made to Surface')
    p.add_argument('--real-port',type=int,default=51321)
    p.add_argument('--real-udp-port',type=int,default=51327)
    p.add_argument('--ui-port',type=int,default=8780)
    p.add_argument('--virtual-port',type=int,default=52321)
    p.add_argument('--virtual-udp-port',type=int,default=52327)
    p.add_argument('--no-browser',action='store_true')
    p.add_argument('--mock-a',action='store_true',help='Use a second local protocol mock as side A; never contact physical DM64')
    cfg=p.parse_args()
    ENGINE=MirrorEngine(cfg.dm64,cfg.virtual_port,cfg.virtual_udp_port,cfg.real_port,cfg.real_udp_port,mock_a=cfg.mock_a,surface_ip=cfg.surface)
    url=f'http://127.0.0.1:{cfg.ui_port}/'
    http=ThreadingHTTPServer(('127.0.0.1',cfg.ui_port),Handler)
    print('dLive Full AH-Net Mirror 0.2.2 | REAL:',cfg.dm64,'| VIRTUAL:',ENGINE.virtual.port,'| UI:',url)
    print('LIVE WRITES: OFF | ALL FRAMES JOURNALED | Virtual does NOT emulate sound/DSP')
    if not cfg.no_browser:
        import webbrowser
        threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:http.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:http.server_close();ENGINE.close()

if __name__=='__main__':main()
