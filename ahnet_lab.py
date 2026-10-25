#!/usr/bin/env python3
"""dLive AH-Net Command Lab 0.5.7 — independent direct MixRack test client.

Standalone direct client; Director is not required:
  python3 ahnet_lab.py --mode direct --dm64 192.168.1.70
The network identity handshake is based on capture 001; registration of per-channel AH-Net objects is not yet validated.
Direct transport was confirmed on-site; individual new parameter writes still require on-site readback.
"""
import argparse, datetime, ipaddress, json, pathlib, secrets, socket, socketserver, threading, time, traceback, urllib.parse, webbrowser, math, collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE=pathlib.Path(__file__).resolve().parent
CATALOG=json.loads((BASE/'catalog.json').read_text(encoding='utf-8'))
ITEMS={x['id']:x for x in CATALOG['items']}
LOG=BASE/'logs'
LOG.mkdir(exist_ok=True)
TOKEN=secrets.token_urlsafe(20)


def frame(op,oa,ob,target,payload):
    if len(payload)>8*1024*1024:raise ValueError('message too large')
    for x in (op,oa,ob,target):
        if x<0 or x>=2**32:raise ValueError('header field out of range')
    return (b'\xf1'+int(op).to_bytes(2,'big')+int(oa).to_bytes(4,'big')+int(ob).to_bytes(4,'big')+int(target).to_bytes(4,'big')+len(payload).to_bytes(4,'big')+payload+b'\xf8')

def make_direct_hello(udp_listen_port):
    """Construct E0 hello with the ACTUAL UDP socket port (not a random number).

    Capture #001 and Session Probe v1: DM64 sends E0 keepalives to the port in
    the E0 00 04 01 03 XX XX E7 announcement. A wrong port triggers ICMP 3/3.
    """
    p=int(udp_listen_port)
    if not 1<=p<=65535:
        raise ValueError('Invalid UDP announce port')
    return (b'\xe0\x00\x04\x01\x03'+p.to_bytes(2,'big')+b'\xe7'
            b'\xe0\x00\x02\x04\x03\xe7')

class FrameParser:
    def __init__(self): self.buf=bytearray()
    def feed(self,data):
        self.buf.extend(data)
        msgs=[]
        while self.buf:
            p=self.buf.find(b'\xf1')
            if p<0:self.buf.clear();break
            if p:self.buf=self.buf[p:]
            if len(self.buf)<20:break
            length=int.from_bytes(self.buf[15:19],'big')
            if length>8*1024*1024:self.buf=self.buf[1:];continue
            size=20+length
            if len(self.buf)<size:break
            if self.buf[size-1]!=0xf8:self.buf=self.buf[1:];continue
            raw=bytes(self.buf[:size]);del self.buf[:size]
            msgs.append({'op':int.from_bytes(raw[1:3],'big'),'a':int.from_bytes(raw[3:7],'big'),
              'b':int.from_bytes(raw[7:11],'big'),'target':int.from_bytes(raw[11:15],'big'),
              'payload':raw[19:-1].hex(' '),'raw':raw.hex(' ')})
        if len(self.buf)>8*1024*1024+20: self.buf.clear()
        return msgs

class UDPRelay:
    """Forward UDP/51327 unmodified in both directions, preserving each client's ephemeral port."""
    def __init__(self, target, listen_port=51327, device_port=51327, emit=None):
        self.target, self.listen_port, self.device_port = target, listen_port, device_port
        self.emit = emit or (lambda *a, **k: None)
        self.listener = None
        self.lock = threading.RLock()
        self.peers = {}
        self.counters = {'to_dm64': 0, 'from_dm64': 0}
        self.last_to = None
        self.last_from = None
        self.first_to = None
        self.first_from = None
        self.sample_requests = []
        self.sample_responses = []
        self.running = False
        self.error = None

    def serve(self):
        try:
            s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1',self.listen_port))
            s.settimeout(0.5)
            self.listener=s
            self.listen_port=s.getsockname()[1]
            self.running=True
            threading.Thread(target=self._from_local,daemon=True).start()
            self.emit('udp_relay_started', listen=f'127.0.0.1:{self.listen_port}',destination=f'{self.target}:{self.device_port}')
        except OSError as ex:
            self.running=False;self.error=str(ex)
            self.emit('udp_relay_failed',error=self.error)
            if self.listener:self.listener.close()

    def _from_local(self):
        while self.running:
            try:data,addr=self.listener.recvfrom(65535)
            except socket.timeout:continue
            except OSError:
                if self.running:self.emit('udp_local_error')
                break
            with self.lock:
                peer=self.peers.get(addr)
                if peer is None:
                    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
                    sock.settimeout(0.5)
                    try:sock.connect((self.target,self.device_port))
                    except OSError as ex:
                        self.emit('udp_upstream_failed',error=str(ex));sock.close();continue
                    peer={'sock':sock,'last_seen':time.monotonic(),'to_dm64':0,'from_dm64':0}
                    self.peers[addr]=peer
                    threading.Thread(target=self._from_remote,args=(addr,peer),daemon=True).start()
                    self.emit('udp_peer_open',peer=f'{addr[0]}:{addr[1]}')
                peer['last_seen']=time.monotonic()
            try:
                peer['sock'].send(data)
                with self.lock:
                    peer['to_dm64']+=1;self.counters['to_dm64']+=1
                    self.last_to=time.time()
                    if self.first_to is None:self.first_to=self.last_to
                    if len(self.sample_requests)<5:self.sample_requests.append({'from':f'{addr[0]}:{addr[1]}','size':len(data),'hex':data[:24].hex(' ')})
            except OSError as ex:
                self.emit('udp_send_failed',error=str(ex))

    def _from_remote(self,client,peer):
        while self.running:
            try:data=peer['sock'].recv(65535)
            except socket.timeout:
                if time.monotonic()-peer['last_seen']>120:break
                continue
            except OSError:break
            try:self.listener.sendto(data,client)
            except OSError:break
            with self.lock:
                peer['from_dm64']+=1;self.counters['from_dm64']+=1
                self.last_from=time.time()
                if self.first_from is None:self.first_from=self.last_from
                if len(self.sample_responses)<5:self.sample_responses.append({'to':f'{client[0]}:{client[1]}','size':len(data),'hex':data[:24].hex(' ')})
        with self.lock:
            if self.peers.get(client) is peer:self.peers.pop(client,None)
        peer['sock'].close()
        self.emit('udp_peer_close',peer=f'{client[0]}:{client[1]}')

    def status(self):
        with self.lock:
            return {'running':self.running,'port':self.listen_port,'error':self.error,
                    'peers':len(self.peers),'client_addresses':[f'{a[0]}:{a[1]}' for a in self.peers],
                    'first_to':self.first_to,'first_from':self.first_from,'last_to':self.last_to,'last_from':self.last_from,
                    'since_to_sec':round(time.time()-self.last_to,2) if self.last_to else None,
                    'since_from_sec':round(time.time()-self.last_from,2) if self.last_from else None,
                    'sample_requests':list(self.sample_requests),'sample_responses':list(self.sample_responses),**self.counters}

    def close(self):
        self.running=False
        if self.listener:self.listener.close()
        with self.lock:
            for peer in self.peers.values():peer['sock'].close()
            self.peers.clear()

class Bridge:
    def __init__(self,target,port):
        self.target=target;self.port=port;self.active=None;self.lock=threading.RLock()
        self.links={};self.loglines=[];self.pending=[];self.last=[];self.idseq=0;self.server=None;self.udp=None;self.unlocked=False;self.hardware_unlocked=False
        self.latest_by_item={}
        self.rx_observed=0;self.rx_matched=0;self.rx_last=None;self.rx_unknown=[];self.rx_channel_updates=0
        self.start_time=time.time()
        self.last_snapshot=None
        self._last_write={}
        self.item_match=collections.defaultdict(list)
        for x in ITEMS.values():
            a=int(x['obj_a'],16);b=int(x['obj_b'],16);t=int(x['target'],16);n=x['n']
            self.item_match[(a,b,t,n)].append(x['id'])
            self.item_match[(b,a,t,n)].append(x['id'])
        self.journal=LOG/('session_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S')+'.jsonl')
    def emit(self,kind,**fields):
        r={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds'),'event':kind,**fields}
        with self.lock:
            self.loglines.append(r)
            if len(self.loglines)>350:self.loglines=self.loglines[-350:]
        with self.lock:
            if getattr(self,'disk_log_enabled',lambda:True)():
                try:
                    with self.journal.open('a',encoding='utf-8') as h:h.write(json.dumps(r,ensure_ascii=False)+'\n')
                except OSError:pass
    def status(self):
        with self.lock:
            info=self.links.get(self.active) if self.active else None
            return {'target':self.target,'proxy_port':self.port,'connected':bool(info and info.get('alive')),
                    'selected_link':self.active,'note':'UDP counter measures only packets forwarded via local relay, not UDP sent directly to DM64','links':[{k:v for k,v in l.items() if k not in ('u_sock','d_sock','tx_lock','rx_parser','identity_bytes')} for l in self.links.values()],
                    'last_logs':list(self.loglines[-65:]),'pending':list(self.pending[-30:]),'journal':str(self.journal),'uptime_sec':round(time.time()-self.start_time,2),'unlocked':self.unlocked,'hardware_unlocked':self.hardware_unlocked,
                    'udp':self.udp.status() if self.udp else {'running':False,'error':'not initialized'},
                    'live_values':dict(self.latest_by_item),
                    'live_receive':{'total':self.rx_observed,'mapped':self.rx_matched,'last':self.rx_last,'input1_updates':self.rx_channel_updates,'unknown_tail':list(self.rx_unknown[-12:]),'subscription_verified':False}}
    def serve(self):
        bridge=self
        class TCPHandler(socketserver.BaseRequestHandler):
            def handle(self):bridge.handle_client(self.request,self.client_address)
        class TCPServer(socketserver.ThreadingTCPServer):
            allow_reuse_address=True
            daemon_threads=True
        self.server=TCPServer(('127.0.0.1',self.port),TCPHandler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.emit('proxy_started',listen='127.0.0.1:'+str(self.port),destination=self.target+':51321')
        self.udp=UDPRelay(self.target,emit=self.emit)
        self.udp.serve()
    def handle_client(self,client,addr):
        try:up=socket.create_connection((self.target,51321),timeout=4)
        except OSError as ex:
            self.emit('upstream_failed',error=str(ex));client.close();return
        client.settimeout(0.5);up.settimeout(0.5)
        for s in (client,up):
            try:s.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
            except OSError:pass
        with self.lock:
            self.idseq+=1;id_=str(self.idseq)
            lk={'id':id_,'peer':addr[0],'started':time.time(),'alive':True,'a2b':0,'b2a':0,
                 'server_identified':False,'client_identified':False,'identity_bytes':b'',
                 'u_sock':up,'d_sock':client,'tx_lock':threading.Lock(),
                 'last_from_director':None,'last_from_dm64':None,'first_eof':None,
                 'closure_reason':None,'closure_direction':None,'closed_at':None,
                 'frames_from_director':0,'frames_from_dm64':0}
            self.links[id_]=lk
        with self.lock:
            ustart=self.udp.status() if self.udp else {}
            lk['udp_start_to']=ustart.get('to_dm64',0)
            lk['udp_start_from']=ustart.get('from_dm64',0)
        self.emit('connection_open',link=id_,udp_at_start=ustart)
        def transfer(src,dst,direction):
            parser=FrameParser()
            stop_reason='unknown'
            try:
                while lk['alive']:
                    try:data=src.recv(32768)
                    except socket.timeout:continue
                    if not data:
                        stop_reason='peer_eof'
                        self.emit('tcp_peer_eof',link=id_,direction=direction,elapsed_sec=round(time.time()-lk['started'],2),
                                  udp_at_eof=self.udp.status() if self.udp else None,
                                  last_from_director=lk.get('last_from_director'),last_from_dm64=lk.get('last_from_dm64'))
                        break
                    with self.lock:lk['last_'+direction]=time.time()
                    if direction=='from_director':
                        if b'DR Box Identification' in data or b'TLDDM64Stagebox' in data:
                            with self.lock:lk['client_identified']=True
                        with lk['tx_lock']:dst.sendall(data)
                        with self.lock:lk['a2b']+=len(data)
                    else:
                        dst.sendall(data)
                        with self.lock:
                            lk['b2a']+=len(data)
                            lk['identity_bytes']=(lk['identity_bytes']+data)[-2048:]
                            if b'Stentor DM64' in lk['identity_bytes'] or b'TLDDM64Stagebox' in lk['identity_bytes']:
                                if not lk['server_identified']:self.emit('mixrack_identified',link=id_)
                                lk['server_identified']=True
                                self.active=id_
                    for f in parser.feed(data):
                        with self.lock:lk['frames_'+direction]+=1
                        self.observe_event(f,direction)
                        if direction=='from_dm64':self.match_event(f)
            except OSError as ex:
                stop_reason='socket_error'
                if lk['alive']:
                    self.emit('connection_lost',link=id_,direction=direction,error=str(ex))
            finally:
                with self.lock:
                    first=lk['alive']
                    lk['alive']=False
                    if self.active==id_:self.active=None
                if first:
                    with self.lock:
                        lk['closure_reason']=stop_reason;lk['closure_direction']=direction;lk['closed_at']=time.time()
                    self.emit('connection_shutdown',link=id_,direction=direction,reason=stop_reason,
                              elapsed_sec=round(time.time()-lk['started'],2),udp=self.udp.status() if self.udp else None,
                              a2b_bytes=lk['a2b'],b2a_bytes=lk['b2a'],
                              udp_delta_to=(self.udp.status()['to_dm64']-lk['udp_start_to']) if self.udp else 0,
                              udp_delta_from=(self.udp.status()['from_dm64']-lk['udp_start_from']) if self.udp else 0)
                    for so in (up,client):
                        try:so.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                        try:so.close()
                        except OSError:pass
                self.emit('connection_thread_exit',link=id_,direction=direction)
        a=threading.Thread(target=transfer,args=(client,up,'from_director'),daemon=True)
        b=threading.Thread(target=transfer,args=(up,client,'from_dm64'),daemon=True)
        a.start();b.start();a.join();b.join()
    def observe_event(self,f,direction):
        raw=bytes.fromhex(f['payload'])
        key=(f['a'],f['b'],f['target'],len(raw))
        ids=self.item_match.get(key,())
        with self.lock:
            if direction=='from_dm64':
                self.rx_observed+=1
                self.rx_last=time.time()
                if ids:
                    self.rx_matched+=1
                    self.rx_channel_updates+=1
                else:
                    self.rx_unknown.append({'op':f['op'],'a':f['a'],'b':f['b'],'target':f['target'],'len':len(raw),'hex':f['payload'][:90]})
                    if len(self.rx_unknown)>50:self.rx_unknown=self.rx_unknown[-50:]
        if not ids:return
        now=time.time()
        with self.lock:
            for item_id in ids:
                previous=self.latest_by_item.get(item_id)
                # Avoid an out-of-order older local Director frame overriding the observed MixRack echo.
                if previous and direction=='from_director' and ((previous['direction']=='from_dm64' and now-previous['time']<0.2) or (previous['direction']=='local_test' and now-previous['time']<1.2)):continue
                self.latest_by_item[item_id]={'hex':f['payload'],'direction':direction,'time':now,'opcode':f['op']}

    def adjust_composite(self,item,part,value):
        if item['id'] not in ('c011','c012','c013','c041','c042','c043','c055','c056','c057','c058','c188','c189') and not (item['group'] in ('Compressor','Gate / Expander') and item['n']==2 and any(w in item['name'] for w in ('Attack','Hold','Release'))):
            raise ValueError('Нет проверенной схемы редактирования этого составного блока')
        with self.lock:
            state=self.latest_by_item.get(item['id'])
        # Captured sample is an explicitly marked fallback; never pretend it is the live state.
        is_live=bool(state and time.time()-state['time']<120)
        baseline=bytes.fromhex(state['hex'] if is_live else item['samples'][0])
        data=bytearray(baseline)
        if item['id']=='c041':
            # Aggregate: header(2) + BPF center(2) + LOW(9) + HIGH(9).
            # Merge current band blocks before toggling aggregate flags to prevent
            # overwriting frequency and shapes with stale captured values.
            with self.lock:
                bands=[self.latest_by_item.get('c042'),self.latest_by_item.get('c043')]
            for off,band in zip((4,13),bands):
                if band and band.get('hex'):
                    payload=bytes.fromhex(band['hex'])
                    if len(payload)==9:data[off:off+9]=payload
        if part=='bpf_frequency_hz' and item['id']=='c041':
            hz=float(value)
            if not 50<=hz<=12000:raise ValueError('BPF centre must be 50..12000 Hz')
            u=round(4608*math.log2(hz/4))
            if not 0<=u<=65535:raise ValueError('BPF frequency encoding overflow')
            data[2:4]=u.to_bytes(2,'big')
        elif part=='abcd_source' and item['id']=='c189':
            n=int(value)
            if n not in (0,1,2,3,4):raise ValueError('ABCD: allowed 0=Off, 1=A, 2=B, 3=C, 4=D')
            data[0]=n
        elif part in ('sidechain_enabled','sidechain_mode') and item['id']=='c041':
            on=int(value)
            if on not in (0,1):raise ValueError('Sidechain flag/mode must be 0 or 1')
            data[0 if part=='sidechain_enabled' else 1]=(0 if on else 1) if part=='sidechain_enabled' else on
        elif part=='sidechain_shape' and item['id'] in ('c042','c043'):
            n=int(value)
            if n not in ((4,6) if item['id']=='c042' else (3,7)):
                raise ValueError('Filter shape not found in PCAP 010/011')
            data[6]=n
        elif part=='frequency_hz':
            hz=float(value)
            lo,hi=(20.,2000.) if item['id']=='c011' else (20.,5000.) if item['id']=='c042' else (120.,20000.) if item['id']=='c043' else (20.,20000.)
            if not lo<=hz<=hi:raise ValueError(f'Частота должна быть {lo:g}…{hi:g} Hz')
            u=round(4608*math.log2(hz/4))
            offset=0 if item['id']=='c011' else 2
            data[offset:offset+2]=u.to_bytes(2,'big')
        elif part=='filter_enabled' and item['id']=='c012':
            on=int(value)
            if on not in (0,1):raise ValueError('Filter enabled must be 0/1')
            data[0]=0 if on else 1
        elif part=='time_ms' and item['group'] in ('Compressor','Gate / Expander') and item['n']==2:
            ms=float(value)
            allowed=(0.05,300.0) if item['id'] in ('c020','c026','c033') else (10.0,5000.0) if item['id'] in ('c021','c027') else (10.0,1000.0) if item['id'] in ('c022','c028','c034') else (0.05,2000.0)
            if not allowed[0]<=ms<=allowed[1]:raise ValueError('Time out of range '+str(allowed))
            u=round(1793.067*math.log2(ms/0.000997235))
            if not 0<=u<=65535:raise ValueError('time overflow')
            data[0:2]=u.to_bytes(2,'big')
        elif part=='gain_db' and item['id'] in ('c055','c056','c057','c058'):
            db=float(value)
            if not -15<=db<=15:raise ValueError('PEQ Gain: -15…+15 dB')
            data[0:2]=round(32768+256*db).to_bytes(2,'big')
        elif part=='peq_enabled' and item['id']=='c188':
            n=int(value)
            if n not in (0,1):raise ValueError('PEQ in/out must be 0/1')
            # v2.12 #014: 0=in, 1=out; 36 remaining bytes unchanged
            data[0]=n
        elif part=='peq_type' and item['id'] in ('c055','c056','c057','c058'):
            n=int(value)
            if n not in ((0,1,4) if item['id']=='c055' else (0,2,3) if item['id']=='c058' else (0,)):
                raise ValueError('PEQ type code incompatible with band')
            data[6]=n
        elif part=='q_octaves' and item['id'] in ('c055','c056','c057','c058'):
            q=float(value)
            allowed=(1/9,.13,1/6,.2,.25,.3,1/3,.4,.45,.5,.55,.6,2/3,.7,.75,.8,.85,.9,.95,1.,1.1,1.2,1.3,1.4,1.5)
            if not any(abs(q-a)<0.00001 for a in allowed):raise ValueError('PEQ Q is discrete, 25 values only')
            # The previous linear interpolation produced WRONG Q values on real S5000.
            # Refuse falsely precise commands until Q/raw lookup is calibrated.
            raise ValueError('Q: шкала AH-Net НЕ подтверждена; прежняя линейная формула отключена. Используй RAW / ALL c055–c058 или отдельную калибровку.')
        elif part=='hpf_type' and item['id']=='c011':
            n=int(value)
            if n not in (0,1,2,3,5):raise ValueError('HPF type: 0,1,2,3,5')
            data[2]=n
        else:raise ValueError('Этот параметр пока не имеет проверенной позиции в блоке')
        return bytes(data),('last observed %s' % state['direction'] if is_live else 'CAPTURED BASELINE, may overwrite other block fields')

    def match_event(self,f):
        with self.lock:
            self.last.append({'t':time.time(),**{k:f[k] for k in ('op','a','b','target','payload')}})
            if len(self.last)>200:self.last=self.last[-200:]
            for p in self.pending:
                if p['status']!='sent_tcp' or time.time()-p['timestamp']>8:continue
                if f['op']==3 and f['a']==p['b'] and f['b']==p['a'] and f['target']==p['target']:
                    p['status']='seen_matching_update' if f['payload']==p['payload'] else 'seen_different_update'
                    p['observed_payload']=f['payload']
                    self.emit('inbound_candidate',id=p['id'],status=p['status'],payload=f['payload'])
    def inject(self,item,payload,reason):
        with self.lock:
            lk=self.links.get(self.active) if self.active else None
            if not self.unlocked:raise PermissionError('Сначала разреши тестирование в интерфейсе')
            if item['risk'] in ('hardware','experimental') or item['n']>=19:
                if not self.hardware_unlocked:raise PermissionError('Разреши аппаратные/экспериментальные команды один раз в интерфейсе')
            if not lk or not lk['alive'] or not lk['server_identified']:
                raise RuntimeError('Нет подтверждённого TCP-подключения Director → proxy → DM64')
            raw=frame(2,int(item['obj_a'],16),int(item['obj_b'],16),int(item['target'],16),payload)
            entry={'id':secrets.token_hex(4),'item_id':item['id'],'name':item['name'],'timestamp':time.time(),
                   'status':'sent_tcp','a':int(item['obj_a'],16),'b':int(item['obj_b'],16),
                   'target':int(item['target'],16),'payload':payload.hex(' ')}
            self.pending.append(entry)
            if len(self.pending)>140:self.pending=self.pending[-140:]
        try:
            with lk['tx_lock']:
                if not lk['alive']:raise RuntimeError('Соединение закрыто')
                lk['u_sock'].sendall(raw)
        except OSError as ex:
            entry['status']='send_error'
            self.emit('test_command_error',id=entry['id'],error=str(ex))
            raise
        with self.lock:
            self.latest_by_item[item['id']]={'hex':payload.hex(' '),'direction':'local_test','time':time.time(),'opcode':2}
        self.emit('test_command_sent',id=entry['id'],item_id=item['id'],name=item['name'],
                  frame_hex=raw.hex(' '),reason=reason,status='sent_tcp_not_equipment_verified')
        return entry


class DirectBridge(Bridge):
    """Independent experimental AH-Net client: no Director, no TCP proxy.

    Only the initial E0/F0 identification sequence is based on capture #001.
    This is not a full implementation of Director's object registration.
    """
    def __init__(self, target, port=51321, udp_port=51327):
        super().__init__(target,port)
        self.udp_port=udp_port
        self._udp_stats={'running':False,'to_dm64':0,'from_dm64':0,'last_to':None,'last_from':None,'error':None}
        self._tcp_thread=None
        self._udp_thread=None
        self._udp_sock=None
        self._udp_local_port=None
        self._udp_announced_port=None
        self._closing=threading.Event()
        self._identified=threading.Event()
        self._endpoint_state='disconnected'
        self._generation=0
        self._client_id=None
        self._server_object=None
        self._identity_query_sent=False
        self._object_register_sent=False
        self._registered_reply=False
        self._rx_watch_state='idle'
        self._rx_mapper_object=None
        self._registered_object=None
        self._registration_at=None
        self._tx_sock=None
        self._tx_lock=threading.Lock()
        # Optional per-session source address for two MacBook network interfaces.
        self.bind_ip=''

    def serve(self):
        self.emit('direct_mode_ready',target=f'{self.target}:{self.port}',detail='No Director or local TCP proxy')

    def status(self):
        s=super().status()
        with self.lock:
            s['mode']='direct'
            s['rx_watch']={'state':self._rx_watch_state,'mapper_object':self._rx_mapper_object,'verified':False,'note':'Experimental Channel Mapper read-only subscription probe; does not prove Input 1 notifications'}
            s['registration']={'request_sent':self._object_register_sent,'object_assignment':self._registered_object,
                                'ack_received':self._registered_reply,
                                'at':self._registration_at,
                                'meaning':'Observed DR Box object announcement + subscriber registration from PCAP #001. ACK not sufficient to prove full session.'}
            s['endpoint_state']=self._endpoint_state
            s['connected']=bool(self._identified.is_set() and self._tx_sock is not None and
                                self.active is not None and self.links.get(self.active,{}).get('alive'))
            s['selected_link']=self.active
            s['proxy_port']=None
            s['udp']={**self._udp_stats,'since_to_sec':round(time.time()-self._udp_stats['last_to'],1) if self._udp_stats['last_to'] else None,
                      'since_from_sec':round(time.time()-self._udp_stats['last_from'],1) if self._udp_stats['last_from'] else None,
                      'peers':1 if self._udp_sock else 0, 'local_port':self._udp_local_port,
                      'announced_port':self._udp_announced_port,
                      'port_match':bool(self._udp_local_port and self._udp_local_port==self._udp_announced_port)}
            s['note']='TCP identity alone is NOT a full session. Monitor registration ACK, UDP replies and >60s lifetime. Validate commands on S5000.'
            return s

    def connect(self):
        self.disconnect()
        with self.lock:
            self._endpoint_state='connecting'
            self._generation+=1
            gen=self._generation
        try:
            s=socket.create_connection((self.target,self.port),timeout=4,
                                       source_address=(self.bind_ip,0) if self.bind_ip else None)
            s.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
            s.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            s.settimeout(0.5)
        except OSError as ex:
            with self.lock:self._endpoint_state='tcp_failed'
            self.emit('direct_connect_failed',error=str(ex),destination=f'{self.target}:{self.port}')
            raise RuntimeError('Не удалось открыть TCP-соединение DM64: '+str(ex))
        # Bind UDP BEFORE advertising our listening port to the MixRack.
        # macOS may assign any available source port; the E0 hello must announce
        # precisely that port so that the rack's replies reach our socket.
        try:
            udp_sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            udp_sock.bind((self.bind_ip or '0.0.0.0',0))
            udp_port=udp_sock.getsockname()[1]
            udp_sock.settimeout(0.5)
            udp_sock.connect((self.target,self.udp_port))
        except OSError as ex:
            try:udp_sock.close()
            except (OSError,UnboundLocalError):pass
            s.close()
            with self.lock:self._endpoint_state='udp_bind_failed'
            self.emit('direct_udp_bind_failed',error=str(ex))
            raise RuntimeError('Не удалось открыть UDP-сокет: '+str(ex))
        with self.lock:
            self._closing.clear();self._identified.clear()
            self._udp_sock=udp_sock
            self._udp_local_port=udp_port
            self._udp_announced_port=udp_port
            self._udp_stats.update({'running':True,'to_dm64':0,'from_dm64':0,
                                    'last_to':None,'last_from':None,'error':None})
            self._tx_sock=s
            linkid='direct-'+str(gen)
            self.active=linkid
            self.links[linkid]={'id':linkid,'started':time.time(),'alive':True,'server_identified':False,
                                'peer':self.target,'a2b':0,'b2a':0,'frames_from_director':0,'frames_from_dm64':0,
                                'identity_bytes':b'','last_from_director':None,'last_from_dm64':None,
                                'tx_lock':self._tx_lock,'u_sock':s,'d_sock':None}
            self._endpoint_state='tcp_open'
        self.emit('direct_tcp_open',link=linkid,destination=f'{self.target}:{self.port}')
        # DR Box identification from real #001 init, with fresh client ID per session.
        client_id=secrets.randbelow(65535-100)+100
        self._client_id=client_id
        self._server_object=None
        self._identity_query_sent=False
        self._object_register_sent=False
        self._registered_reply=False
        self._rx_watch_state='idle'
        self._rx_mapper_object=None
        self._registered_object=None
        self._registration_at=None
        self._rx_watch_state='idle';self._rx_mapper_object=None
        self.emit('direct_udp_bound',link=linkid,local_port=udp_port,
                  advertised_port=udp_port,destination_port=self.udp_port,
                  port_match=True)
        self._tcp_thread=threading.Thread(target=self._read_tcp,args=(s,linkid,gen),daemon=True)
        self._tcp_thread.start()
        hello=make_direct_hello(udp_port)
        tag=b'DR Box Identification\x00'
        identify=b'\xf0\x00\x02\x00\x00'+client_id.to_bytes(2,'big')+b'\x00\x04'+len(tag).to_bytes(2,'big')+tag+b'\xf7'
        try:
            with self._tx_lock:
                self._send_tcp(s,hello)
                self._send_tcp(s,identify)
            self.emit('direct_identity_sent',link=linkid,client_id=hex(client_id),
                      bytes=len(hello)+len(identify),source='observed in init PCAP #001')
            # PCAP #001: Director sends an additional F1 discovery request for the DR Box object.
            # Firmware assigns object 7 (do not assume constant), followed by target 0x0006 16B registration.
            with self._tx_lock:
                self._send_tcp(s,frame(2,0,3,4,tag))
            self._object_register_sent=True
            self.emit('direct_object_discovery_sent',link=linkid,name='DR Box Identification',
                      target='0x0004',source='PCAP #001')
        except OSError as ex:
            self.disconnect()
            raise RuntimeError('Ошибка при представлении AH-Net клиента: '+str(ex))
        # Start UDP heartbeat once the TCP E0 and object discovery frames have
        # been sent; the same pre-bound socket is used to receive rack replies.
        self._udp_thread=threading.Thread(target=self._run_udp,args=(gen,udp_sock),daemon=True)
        self._udp_thread.start()
        self._identified.wait(4.0)
        # Allow the asynchronous observed object-registration exchange to complete
        # before reporting the initial session to the UI. Identity is still not
        # proof of full subscription/long-term session availability.
        if self._identified.is_set():
            until=time.monotonic()+0.8
            while time.monotonic()<until and not self._registered_reply and not self._closing.is_set():
                time.sleep(0.01)
        if not self._identified.is_set():
            self.emit('direct_identity_pending',link=linkid,detail='TCP open, DM64 identity not yet seen; controls remain disabled')
        return {'state':self.status()['endpoint_state'],'identity_verified':self._identified.is_set(),
                'message':'DM64 identity received' if self._identified.is_set() else 'TCP open; no validated identity yet'}

    def begin_rx_watch(self):
        """Optional experimental READ/SUBSCRIBE discovery; no parameter writes.

        Capture #001: Director announced Channel Mapper and registered ranges at
        target 6. We deliberately start with a *small* range and do not claim
        that it exposes Input 1. Results are recorded for further decoding.
        """
        with self.lock:
            sock=self._tx_sock
            if not (sock and self._identified.is_set() and self._client_id):
                raise RuntimeError('Connect to DM64 before starting RX watch')
            if self._rx_watch_state not in ('idle','error'):
                return {'state':self._rx_watch_state,'notice':'RX watch already requested'}
            self._rx_watch_state='discovery_sent'
        discovery=frame(2,0,3,4,b'Channel Mapper\x00')
        try:
            with self._tx_lock:self._send_tcp(sock,discovery)
            self.emit('rx_watch_channel_mapper_discovery',hex=discovery.hex(' '))
        except OSError as ex:
            with self.lock:self._rx_watch_state='error'
            self.emit('rx_watch_error',error=str(ex))
            raise
        return {'state':'discovery_sent','notice':'Awaiting Channel Mapper assignment (experimental)'}

    def _send_tcp(self,sock,data):
        sock.sendall(data)
        self.observe_raw_tcp(data,'outgoing')

    def observe_raw_tcp(self,data,direction='incoming'):
        pass

    def _read_tcp(self,s,linkid,gen):
        parser=FrameParser()
        trailer=bytearray()
        try:
            while not self._closing.is_set() and gen==self._generation:
                try: data=s.recv(65535)
                except socket.timeout:continue
                if not data:
                    self.emit('direct_peer_eof',link=linkid,elapsed_sec=round(time.time()-self.links[linkid]['started'],2))
                    break
                self.observe_raw_tcp(data)
                trailer.extend(data)
                if len(trailer)>5000:del trailer[:-5000]
                with self.lock:
                    lk=self.links.get(linkid)
                    if lk:
                        lk['b2a']+=len(data)
                        lk['last_from_dm64']=time.time()
                if b'TLDDM64Stagebox' in trailer or b'Stentor DM64' in trailer:
                    with self.lock:
                        if gen==self._generation and not self._identified.is_set():
                            self._identified.set();self._endpoint_state='identified'
                            lk=self.links.get(linkid)
                            if lk:lk['server_identified']=True
                            self.emit('direct_identity_verified',link=linkid,device='DM64',source='TCP response')
                for f in parser.feed(data):
                    with self.lock:
                        lk=self.links.get(linkid)
                        if lk:lk['frames_from_dm64']+=1
                    # In PCAP #001 the rack first replies to DR Box Identification with
                    # F1(op3, client_id, 0, target=2, payload=server_object_id).
                    # The real Director then queries target 0x100 of that assigned object.
                    if (f['op']==3 and f['a']==self._client_id and f['b']==0 and
                        f['target']==2 and len(bytes.fromhex(f['payload']))==4 and
                        not self._identity_query_sent):
                        server_obj=int.from_bytes(bytes.fromhex(f['payload']),'big')
                        if 0<server_obj<65536:
                            self._server_object=server_obj
                            self._identity_query_sent=True
                            try:
                                query=frame(2,server_obj,self._client_id,0x100,b'')
                                with self._tx_lock:self._send_tcp(s,query)
                                self.emit('direct_identity_query_sent',link=linkid,assigned_object=server_obj,client_id=self._client_id)
                            except OSError as ex:
                                self.emit('direct_identity_query_error',error=str(ex))
                    # PCAP #001: answer to F1 0/3/4 discovery is F1 3/0/2 with a 4-byte object ID.
                    if (f['op']==3 and f['a']==3 and f['b']==0 and f['target']==2 and
                        len(bytes.fromhex(f['payload']))==4 and not self._registration_at):
                        assigned=int.from_bytes(bytes.fromhex(f['payload']),'big')
                        if 0<assigned<65536 and self._client_id is not None:
                            self._registered_object=assigned
                            # Exact observed fields from PCAP #001 with fresh client-id inserted.
                            # Range descriptors 0x1002..0x1004 are experimental, NOT confirmed authorization.
                            registration=(b'\x00\x00\x00\x00'+self._client_id.to_bytes(2,'big')+
                                          b'\x00\x00\x10\x02\x00\x00\x10\x04\x00\xff')
                            try:
                                with self._tx_lock:self._send_tcp(s,frame(2,assigned,3,6,registration))
                                self._registration_at=time.time()
                                with self.lock:
                                    lk=self.links.get(linkid)
                                    if lk:lk['a2b']+=len(registration)+20;lk['frames_from_director']+=1;lk['last_from_director']=time.time()
                                self.emit('direct_object_registration_sent',link=linkid,assigned_object=assigned,
                                          payload=registration.hex(' '),source='PCAP #001')
                            except OSError as ex:
                                self.emit('direct_object_registration_error',error=str(ex))
                    if (f['op']==3 and f['a']==self._client_id and
                        self._registered_object is not None and f['b']==self._registered_object and f['target']==8):
                        if not self._registered_reply:
                            self._registered_reply=True
                            self.emit('direct_object_registration_ack',link=linkid,
                                      note='Session lifetime still unverified')
                    # Experimental manager discovery. Registration here is a
                    # READ/SUBSCRIBE request for two Channel Mapper properties;
                    # it does not modify gain, patch, phantom or audio routing.
                    if (f['op']==3 and f['a']==3 and f['b']==0 and f['target']==2 and
                        len(bytes.fromhex(f['payload']))==4 and self._rx_watch_state=='discovery_sent'):
                        mapper=int.from_bytes(bytes.fromhex(f['payload']),'big')
                        if mapper>0:
                            self._rx_mapper_object=mapper
                            self._rx_watch_state='registering'
                            self.emit('rx_watch_mapper_identified',object_id=mapper)
                            register=(b'\x00'*4+self._client_id.to_bytes(2,'big')+
                                      b'\x00\x00'+(0x1003).to_bytes(4,'big')+
                                      (0x1004).to_bytes(4,'big')+b'\x00\xff')
                            try:
                                with self._tx_lock:self._send_tcp(s,frame(2,mapper,3,6,register))
                                self._rx_watch_state='requested'
                                self.emit('rx_watch_registration_requested',object_id=mapper,
                                          target_range='0x1003..0x1004',source='PCAP #001 partial')
                            except OSError as ex:
                                self._rx_watch_state='error';self.emit('rx_watch_error',error=str(ex))
                    if (f['op']==3 and self._rx_mapper_object is not None and
                        f['a']==self._client_id and f['b']==self._rx_mapper_object and
                        f['target']==8 and self._rx_watch_state=='requested'):
                        self._rx_watch_state='ack_received'
                        self.emit('rx_watch_ack_received',note='Subscription acceptance not equal to Input 1 update delivery')
                    self.observe_event(f,'from_dm64');self.match_event(f)
        except OSError as ex:
            if not self._closing.is_set():self.emit('direct_receive_error',link=linkid,error=str(ex))
        finally:
            udp_to_close=None
            with self.lock:
                if gen==self._generation:
                    lk=self.links.get(linkid)
                    if lk:lk['alive']=False
                    self.active=None;self._tx_sock=None;self._identified.clear();self._endpoint_state='disconnected'
                    self._closing.set()  # stop heartbeat when DM64 closes TCP
                    udp_to_close=self._udp_sock
                    self._udp_sock=None
                    self._udp_stats['running']=False
                    self.emit('direct_disconnected',link=linkid)
            if udp_to_close:
                try:udp_to_close.close()
                except OSError:pass
            try:s.close()
            except OSError:pass

    def _run_udp(self,gen,s):
        marker=b'\xe0\x00\x01\x03\xe7'  # 1 Hz in PCAP #001
        try:
            with self.lock:
                if gen!=self._generation or self._closing.is_set():return
                listen_port=s.getsockname()[1]
                if listen_port!=self._udp_announced_port:
                    self.emit('direct_udp_port_mismatch',local_port=listen_port,
                              advertised_port=self._udp_announced_port)
                    return
                self._udp_stats['running']=True;self._udp_stats['error']=None
            self.emit('direct_udp_open',destination=f'{self.target}:{self.udp_port}',
                      local_port=listen_port,advertised_port=self._udp_announced_port)
            parser=FrameParser()
            next_send=0
            while not self._closing.is_set() and gen==self._generation:
                if time.monotonic()>=next_send:
                    try:
                        s.send(marker)
                        with self.lock:
                            self._udp_stats['to_dm64']+=1;self._udp_stats['last_to']=time.time()
                    except OSError as ex:
                        with self.lock:self._udp_stats['error']=str(ex)
                        self.emit('direct_udp_send_error',error=str(ex))
                    next_send=time.monotonic()+1
                try:d=s.recv(65535)
                except socket.timeout:continue
                except OSError as ex:
                    if not self._closing.is_set():self.emit('direct_udp_receive_error',error=str(ex))
                    break
                with self.lock:
                    self._udp_stats['from_dm64']+=1
                    rx_count=self._udp_stats['from_dm64']
                    self._udp_stats['last_from']=time.time()
                if rx_count<=3:
                    self.emit('direct_udp_reply_received',bytes=len(d),hex=d[:32].hex(' '),
                              local_port=self._udp_local_port)
                # UDP datagrams can contain multiple F1 frames.
                for f in parser.feed(d):
                    self.observe_event(f,'from_dm64')
                    self.match_event(f)
        finally:
            with self.lock:
                if self._udp_sock is s:
                    self._udp_sock=None
                    self._udp_stats['running']=False
            s.close()

    def disconnect(self):
        self._closing.set()
        with self.lock:
            self._generation+=1
            s=self._tx_sock;self._tx_sock=None
            u=self._udp_sock;self._udp_sock=None
            linkid=self.active;self.active=None
            self._identified.clear();self._endpoint_state='disconnected';self._client_id=None;self._server_object=None
            if linkid and linkid in self.links:self.links[linkid]['alive']=False
            # Re-enable only by explicit user action after reconnect.
            self.unlocked=False;self.hardware_unlocked=False
        for sock in (s,u):
            if sock:
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                try:sock.close()
                except OSError:pass
        if linkid:self.emit('direct_user_disconnect',link=linkid)

    def inject(self,item,payload,reason):
        with self.lock:
            if not self.unlocked:raise PermissionError('Включи тестовый режим для склада')
            if (item['risk'] in ('hardware','experimental') or item['n']>=19) and not self.hardware_unlocked:
                raise PermissionError('Разреши тесты аппаратных и составных параметров')
            if not self._identified.is_set() or self._tx_sock is None or not self.active:
                raise RuntimeError('Нет подтверждённой AH-Net identity-сессии с DM64')
            sock=self._tx_sock
            linkid=self.active
            raw=frame(2,int(item['obj_a'],16),int(item['obj_b'],16),int(item['target'],16),payload)
            entry={'id':secrets.token_hex(4),'item_id':item['id'],'name':item['name'],'timestamp':time.time(),
                   'status':'sent_tcp','a':int(item['obj_a'],16),'b':int(item['obj_b'],16),
                   'target':int(item['target'],16),'payload':payload.hex(' ')}
            self.pending.append(entry)
            if len(self.pending)>140:self.pending=self.pending[-140:]
        try:
            with self._tx_lock:
                if not self._identified.is_set() or sock is not self._tx_sock:
                    raise RuntimeError('Прямое соединение было закрыто')
                self._send_tcp(sock,raw)
            with self.lock:
                lk=self.links.get(linkid)
                if lk:
                    lk['a2b']+=len(raw);lk['frames_from_director']+=1;lk['last_from_director']=time.time()
                self.latest_by_item[item['id']]={'hex':payload.hex(' '),'direction':'local_test','time':time.time(),'opcode':2}
            self.emit('direct_command_sent',id=entry['id'],item_id=item['id'],name=item['name'],
                      frame_hex=raw.hex(' '),status='sent_tcp_not_equipment_verified',reason=reason)
        except (OSError,RuntimeError) as ex:
            entry['status']='send_error'
            self.emit('direct_command_error',id=entry['id'],error=str(ex))
            raise
        return entry

# Instantiate after validating IP
BRIDGE=None

def command_payload(item,value):
    n=item['n'];fmt=item['fmt']
    if fmt=='db256':
        db=float(value)
        if item['id']=='c001' and not (-128<=db<=10):raise ValueError('Fader: -Infinity (−128 sentinel) … +10 dB')
        if item['id']=='c003':
            if not 5<=db<=60:raise ValueError('Preamp Gain hardware must be 5..60 dB')
        elif item['id'] in ('c019','c025','c032','c038'):
            if not 0<=db<=60:raise ValueError('Depth must be 0..60 dB')
        elif item['id'] in ('c053','c054') and not (-128<=db<=0):raise ValueError('Parallel Dry/Wet: -Infinity..0 dB')
        elif item['id'] in ('c001','c053','c054') and db==-128.0:
            # Experimental raw 0x0000; firmware interpretation as -Infinity still needs DM64 readback.
            pass
        elif not -120<=db<=24:raise ValueError('dB outside permitted test range -120…+24')
        u=round(32768+256*db)
        if not 0<=u<=65535:raise ValueError('dB cannot be encoded')
        b=u.to_bytes(2,'big')
    elif fmt=='delay96':
        ms=float(value)
        if not 0<=ms<=341.32:raise ValueError('Input Delay outside 0…341.32 ms (console limit)')
        b=round(ms*96).to_bytes(2,'big')
    elif fmt=='name9':
        b=value.encode('ascii','strict')
        if len(b)>8:raise ValueError('Channel Name must contain <=8 ASCII characters')
        b=b.ljust(9,b'\x00')
    elif fmt in ('flag','colour','enum'):
        v=int(str(value),0) if isinstance(value,str) and value.startswith('0x') else int(value)
        if fmt=='flag' and v not in (0,1):raise ValueError('flag must be 0/1')
        if fmt=='colour' and not 0<=v<=7:raise ValueError('color 0..7')
        if fmt=='enum' and not 0<=v<=2:raise ValueError('enum 0..2')
        b=bytes([v])
    else:
        b=bytes.fromhex(value.replace('0x',''))
    if len(b)!=n:raise ValueError(f'expected {n} bytes, got {len(b)}')
    return b

class AppHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def _send(self,code,obj):
        d=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(d)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(d)
    def do_GET(self):
        path=urllib.parse.urlsplit(self.path).path
        if path=='/api/catalog':return self._send(200,CATALOG)
        if path=='/api/status':return self._send(200,BRIDGE.status())
        if path=='/api/diagnostics':
            status=BRIDGE.status()
            # Exclude application secret and raw command bytes; preserve protocol/debug metadata.
            diagnostic={'version':'0.5.6','status':status,'recent_events':list(BRIDGE.loglines),'explanation':'Direct mode: isolated AH-Net identity+TCP command client. Real S5000 is authoritative for change confirmation. All timestamps UTC.'}
            data=json.dumps(diagnostic,ensure_ascii=False,indent=2).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Disposition','attachment; filename=AHNET_Lab_0_5_6_Diagnostics.json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data);return
        if path in ('/','/index.html'):
            data=(BASE/'index.html').read_text(encoding='utf-8').replace('%%LAB_TOKEN%%',TOKEN).encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Security-Policy',"default-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data);return
        self._send(404,{'error':'not found'})
    def do_POST(self):
        if self.path not in ('/api/send','/api/adjust','/api/unlock','/api/lock','/api/connect','/api/disconnect','/api/rx-watch'):
            return self._send(404,{'error':'unknown path'})
        if self.headers.get('X-Lab-Token')!=TOKEN:return self._send(403,{'error':'bad local client token'})
        if not self.headers.get('Content-Type','').startswith('application/json'):
            return self._send(415,{'error':'application/json required'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<=n<=30000:return self._send(413,{'error':'request too large'})
            q=json.loads(self.rfile.read(n))
            if self.path=='/api/connect':
                if not isinstance(BRIDGE,DirectBridge):raise RuntimeError('Direct mode not enabled')
                return self._send(200,{'ok':True,**BRIDGE.connect()})
            if self.path=='/api/rx-watch':
                if not isinstance(BRIDGE,DirectBridge):raise RuntimeError('Requires direct mode')
                return self._send(200,{'ok':True,**BRIDGE.begin_rx_watch()})
            if self.path=='/api/disconnect':
                if not isinstance(BRIDGE,DirectBridge):raise RuntimeError('Direct mode not enabled')
                BRIDGE.disconnect()
                return self._send(200,{'ok':True})
            if self.path=='/api/lock':
                with BRIDGE.lock:
                    BRIDGE.unlocked=False;BRIDGE.hardware_unlocked=False
                BRIDGE.emit('lab_locked')
                return self._send(200,{'ok':True})
            if self.path=='/api/unlock':
                if q.get('bench_ack') is not True:
                    raise ValueError('Подтверди, что оборудование находится на безопасном тестовом стенде')
                hardware=q.get('hardware_ack') is True
                with BRIDGE.lock:
                    BRIDGE.unlocked=True;BRIDGE.hardware_unlocked=hardware
                BRIDGE.emit('lab_unlocked',hardware=hardware)
                return self._send(200,{'ok':True,'hardware':hardware})
            x=ITEMS[q['id']]
            if self.path=='/api/adjust':
                with BRIDGE.lock:
                    b,origin=BRIDGE.adjust_composite(x,q['part'],q['value'])
                    result=BRIDGE.inject(x,b,str(q.get('reason','mixer control'))[:120]+' | '+origin)
                return self._send(200,{'ok':True,'message':'TCP sent; device application not confirmed','entry':result,'baseline':origin})
            b=command_payload(x,str(q['value']))
            if x['fmt']=='hex' and b.hex(' ') not in [bytes.fromhex(z).hex(' ') for z in x['samples']]:
                if q.get('expert') is not True:raise ValueError('Для нового HEX требуется явное экспертное разрешение')
            result=BRIDGE.inject(x,b,str(q.get('reason','bench test'))[:120])
            return self._send(200,{'ok':True,'message':'TCP sent, application/console confirmation pending','entry':result})
        except (ValueError,KeyError,UnicodeError,RuntimeError,OSError,PermissionError) as ex:
            self._send(400,{'ok':False,'error':str(ex)})


def main():
    global BRIDGE
    p=argparse.ArgumentParser(description='AH-Net v2.12 controlled live command lab, Python 3.10+')
    p.add_argument('--dm64',default='192.168.1.70',help='Your real MixRack IP')
    p.add_argument('--proxy-port',type=int,default=51321)
    p.add_argument('--mode',choices=['direct','proxy'],default='direct',help='direct: standalone MixRack; proxy: Director bridge')
    p.add_argument('--ui-port',type=int,default=8768)
    p.add_argument('--no-browser',action='store_true')
    a=p.parse_args()
    if not ipaddress.ip_address(a.dm64).is_private: p.error('Only private/LAN MixRack IPs are permitted')
    if a.dm64 in ('127.0.0.1','0.0.0.0'):p.error('Set real DM64 LAN address, not local loopback')
    if not 1024<=a.proxy_port<=65535 or not 1024<=a.ui_port<=65535 or a.proxy_port==a.ui_port:p.error('Bad ports')
    BRIDGE=DirectBridge(a.dm64,a.proxy_port) if a.mode=='direct' else Bridge(a.dm64,a.proxy_port)
    BRIDGE.serve()
    http=ThreadingHTTPServer(('127.0.0.1',a.ui_port),AppHandler)
    url=f'http://127.0.0.1:{a.ui_port}/'
    print('dLive Command Lab 0.5.7 | Mode:',a.mode)
    print('GUI:',url,'| MixRack:',a.dm64+':51321','| Direct Mode:',a.mode=='direct')
    print('Direct mode requires NO Director. Its identification and command application require onsite testing.')
    print('Journal:',BRIDGE.journal)
    if not a.no_browser:threading.Timer(1.0,lambda:webbrowser.open(url)).start()
    try:http.serve_forever()
    except KeyboardInterrupt:print('\nShutting down')
    finally:
        http.server_close()
        if isinstance(BRIDGE,DirectBridge):BRIDGE.disconnect()
        else:BRIDGE.server.shutdown();BRIDGE.server.server_close();BRIDGE.udp.close()

if __name__=='__main__':main()
