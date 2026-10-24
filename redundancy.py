"""Opt-in active/standby controller, disk journal and per-side reconnects.

Order is observed arrival order. Socket acceptance is not hardware confirmation.
Unknown opaque messages are retained, but cannot be safely replayed after loss.
"""
import collections,json,pathlib,platform,sqlite3,subprocess,threading,time
from concurrent.futures import ThreadPoolExecutor
from ahnet_lab import frame
from legacy_lab import decode_raw
from layout_map import ORDER,width
from safety_filter import phantom,preserve_scene_preamps,object_name
from scene_sync import Scene,SceneRPC,adapt_surface
from recovery_queue import state_key

class Journal:
    def __init__(self,path):
        self.storage_path=str(path)
        self.lock=threading.RLock();self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS commands(seq INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, body TEXT, state TEXT DEFAULT "pending", error TEXT)')
        self.db.execute('CREATE INDEX IF NOT EXISTS unfinished ON commands(seq) WHERE state IN ("pending","sending","uncertain")')
        self.db.execute('CREATE TABLE IF NOT EXISTS marks(seq INTEGER PRIMARY KEY AUTOINCREMENT,t REAL,event TEXT,body TEXT)')
        # A prior process may have sent a command without committing its result.
        self.db.execute('UPDATE commands SET state="uncertain",error="process stopped during send" WHERE state="sending"');self.db.commit()
        self.pending_count,self.pending_bytes=self.db.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(body)),0) FROM commands WHERE state IN ("pending","sending","uncertain")').fetchone()
        self.compact_candidates={};self.compacted=0
    def mark(self,event,**fields):
        with self.lock:
            self.compact_candidates.clear()
            self.db.execute('INSERT INTO marks(t,event,body) VALUES(?,?,?)',(time.time(),event,json.dumps(fields)));self.db.commit()
    def append(self,source,body,compact=False):
        with self.lock:
            encoded=json.dumps(body);key=state_key(body) if compact else None
            # A barrier closes the whole segment, including other control keys.
            if key is None:self.compact_candidates.clear()
            previous=self.compact_candidates.get((source,key)) if key else None
            old=self.db.execute('SELECT state,LENGTH(body) FROM commands WHERE seq=?',(previous,)).fetchone() if previous else None
            replace=bool(old and old[0]=='pending')
            size=self.pending_bytes-(old[1] if replace else 0)+len(encoded)
            count=self.pending_count+(0 if replace else 1)
            if size>128*1024*1024:raise RuntimeError('Журнал достиг лимита 128 МиБ незавершённых данных')
            if count>250000:raise RuntimeError('Журнал достиг лимита 250000 незавершённых команд')
            with self.db:
                if replace:self.db.execute('UPDATE commands SET state="coalesced",error="replaced by later absolute offline value" WHERE seq=?',(previous,))
                cur=self.db.execute('INSERT INTO commands(source,body) VALUES(?,?)',(source,encoded))
            self.pending_count=count;self.pending_bytes=size
            if replace:self.compacted+=1
            if key:self.compact_candidates[(source,key)]=cur.lastrowid
            if cur.lastrowid%100==0:
                self.db.execute('DELETE FROM commands WHERE state IN ("sent","superseded","coalesced") AND seq<?',(cur.lastrowid-10000,))
                self.db.execute('DELETE FROM marks WHERE seq<(SELECT MAX(seq)-10000 FROM marks)');self.db.commit()
            return cur.lastrowid
    def first(self):
        rows=self.pending(1)
        return rows[0] if rows else None
    def pending(self,limit=32):
        with self.lock:
            rows=self.db.execute('SELECT seq,source,body,state FROM commands WHERE state IN ("pending","sending","uncertain") ORDER BY seq LIMIT ?',(limit,)).fetchall()
            return [(r[0],r[1],json.loads(r[2]),r[3]) for r in rows]
    def names(self):
        with self.lock:
            return list(dict.fromkeys(cmd['name'] for _,_,cmd,_ in self.pending(50000) if cmd.get('kind')=='rack'))
    def set_many(self,sequences,state,error=None):
        with self.lock,self.db:
            for seq in sequences:
                row=self.db.execute('SELECT state,LENGTH(body) FROM commands WHERE seq=?',(seq,)).fetchone()
                if not row:continue
                before=row[0] in ('pending','sending','uncertain');after=state in ('pending','sending','uncertain')
                self.db.execute('UPDATE commands SET state=?,error=? WHERE seq=?',(state,error,seq))
                self.pending_count+=int(after)-int(before);self.pending_bytes+=(int(after)-int(before))*row[1]
    def set(self,seq,state,error=None,body=None):
        with self.lock:
            if body is not None:
                encoded=json.dumps(body)
                size=self.db.execute('SELECT COALESCE(SUM(LENGTH(body)),0) FROM commands WHERE state IN ("pending","sending","uncertain") AND seq!=?',(seq,)).fetchone()[0]
                if size+len(encoded)>128*1024*1024:raise RuntimeError('Журнал достиг лимита 128 МиБ незавершённых данных')
                self.db.execute('UPDATE commands SET body=? WHERE seq=?',(encoded,seq))
                self.pending_bytes=self.db.execute('SELECT COALESCE(SUM(LENGTH(body)),0) FROM commands WHERE state IN ("pending","sending","uncertain")').fetchone()[0]
            self.set_many([seq],state,error)
    def stats(self):
        with self.lock:return dict(self.db.execute('SELECT state,COUNT(*) FROM commands GROUP BY state').fetchall())
    def close(self):
        with self.lock:self.db.close()
    def relocate(self,path):
        # Keep the Journal object stable for readers; swap its backing storage
        # only under its lock. The caller fences producers and queue workers.
        with self.lock:
            target=sqlite3.connect(str(path),check_same_thread=False)
            try:
                self.db.backup(target)
                target.execute('PRAGMA journal_mode=WAL')
                target.execute('PRAGMA synchronous=FULL')
            except Exception:
                target.close();raise
            self.db.close();self.db=target;self.storage_path=str(path)

def ping(ip,local=''):
    args=['ping','-n','1','-w','1000',ip] if platform.system()=='Windows' else ['ping','-c','1',ip]
    if local and platform.system()!='Windows':args[1:1]=['-S' if platform.system()=='Darwin' else '-I',local]
    t=time.monotonic()
    try:
        r=subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=1.4)
        return dict(ok=r.returncode==0,elapsed_ms=round((time.monotonic()-t)*1000),t=time.time())
    except (OSError,subprocess.TimeoutExpired) as ex:return dict(ok=False,error=str(ex),t=time.time())

class Redundancy:
    def __init__(self,engine):
        self.e=engine;self.lock=threading.RLock();self.io=threading.RLock();self.closed=threading.Event();self.wake=threading.Event()
        self.active=False;self.master='A';self.role=0;self.token=0;self.journal=None;self.path=None
        self.phase='disabled';self.error=None;self.last_error=None;self.health={};self.pings={};self.retries={};self.retry_at={}
        self.next_batch=0;self.replayed_commands=0;self.replay_batches=0;self.global_seen={}
        self.first_seen={};self.journals=[];self.scene_echo={};self.expected={};self.layouts={'A':{},'B':{}};self.rpc={};self.loss_seen=False;self.both_lost=False
        self.cleanup_due=False
        self.pool=ThreadPoolExecutor(max_workers=4);self.ping_jobs={};self.last_ping=0
        self.monitor=threading.Thread(target=self._monitor,daemon=True);self.worker=threading.Thread(target=self._work,daemon=True)
        self.monitor.start();self.worker.start()
    def enabled(self):return bool(self.e.cfg.get('auto_redundancy'))
    def _ready(self,side):
        e=self.e;b=e.bridges.get(side);s=e.surfaces.sessions.get(side)
        if not b or not s:return False
        bs=b.status();ss=s.status();udp=bs.get('udp',{})
        return bool(bs['connected'] and b.probe_status().get('phase')=='requests_sent' and ss['connected'] and
                    ss['phase']=='ready' and ss.get('silence_seconds',0)<4 and
                    udp.get('running',True) and udp.get('since_from_sec') is not None and udp['since_from_sec']<4 and
                    all(n in e.surfaces.layouts[side] for n in ORDER))
    def _destination_ready(self,side):
        """Receiving writes need not wait for the complete notification catalogue.

        Master eligibility still requires the full subscription in _ready().
        Individual writes additionally require a freshly resolved object ID.
        """
        b=self.e.bridges.get(side);s=self.e.surfaces.sessions.get(side)
        if not b or not s:return False
        bs=b.status();ss=s.status();u=bs.get('udp',{})
        return bool(bs['connected'] and b.probe_status().get('phase') in ('discovering','registering','requests_sent') and
                    u.get('running',True) and u.get('since_from_sec') is not None and u['since_from_sec']<4 and
                    ss['connected'] and ss['phase']=='ready' and ss.get('silence_seconds',0)<4 and
                    all(n in self.e.surfaces.layouts[side] for n in ORDER))
    def start(self):
        if self.e.cfg['direction']=='both':raise RuntimeError('Для резервирования выбери начального мастера 1 → 2 или 2 → 1')
        ready={s:self._ready(s) for s in 'AB'}
        if not any(ready.values()):raise RuntimeError('Дождись полной готовности хотя бы одной системы')
        self.stop()
        with self.lock:
            self.master='A' if self.e.cfg['direction']=='A_to_B' else 'B'
            if not ready[self.master]:self.master='B' if self.master=='A' else 'A'
            self.token+=1;self.role+=1;self.active=False;self.error=None;self.loss_seen=not all(ready.values());self.both_lost=False
            self.path=str(pathlib.Path(self.e.log_path).with_name('recovery_'+str(time.time_ns())+'.sqlite3'))
            self.journal=Journal(self.path);self.active=True;self.journals.append(self.journal);self.health=ready;self.rpc={};self.expected={};self.scene_echo={}
            self.next_batch=0;self.replayed_commands=0;self.replay_batches=0
            self.global_seen={s:b.probe_status().get('last_global_period_ms') for s,b in self.e.bridges.items() if b.probe_status().get('last_global_period_ms') is not None}
            self.layouts={s:dict(self.e.surfaces.layouts[s]) for s in 'AB'}
            self.phase='live' if all(ready.values()) else 'recording';self._direction()
            self.cleanup_due=True;self.e.disk_logging=True
            self.journal.mark('armed',master=self.master,health=ready)
        self.wake.set()
    def _direction(self):self.e.cfg['direction']='A_to_B' if self.master=='A' else 'B_to_A'
    def _persist(self):
        if not self.active:return
        self.cleanup_due=True;self.e.disk_logging=True
        if self.journal and self.journal.storage_path==':memory:':
            self.path=str(pathlib.Path(self.e.log_path).with_name('recovery_'+str(time.time_ns())+'.sqlite3'))
            self.journal.relocate(self.path)
    def purge_logs(self,manual=False):
        # Order matches delivery: io -> engine gate -> controller -> journal.
        with self.io,self.e.gate,self.lock:
            if not manual:
                if (not self.active or not self.cleanup_due or self.error or self.both_lost
                    or not all(self._ready(s) for s in 'AB')
                    or any(journal.first() for journal in self.journals)):
                    return False
            self.e.disk_logging=False
            for journal in self.journals:
                if journal.storage_path!=':memory:':journal.relocate(':memory:')
            if manual:
                # Disconnect is an explicit request to discard even unfinished
                # data. No automatic cleanup uses this branch.
                for journal in self.journals:
                    with journal.lock:
                        journal.db.execute('DELETE FROM commands')
                        journal.db.execute('DELETE FROM marks');journal.db.commit()
                        journal.pending_count=0;journal.pending_bytes=0
                        journal.compact_candidates.clear()
            self.path=None;self.cleanup_due=False
            # Session writers synchronize with their own transport lock.
            from contextlib import ExitStack
            with ExitStack() as stack:
                for bridge in self.e.bridges.values():
                    lock=getattr(bridge,'lock',None)
                    if lock:stack.enter_context(lock)
                success=self.e.clear_log_files('disconnect' if manual else 'recovery_complete')
            self.cleanup_due=not success
            return success
    def stop(self):
        with self.lock:
            self.active=False;self.token+=1
            for _,rpc in self.rpc.values():rpc.cancel()
            if self.journal:
                try:self.journal.mark('manual_stop',phase=self.phase)
                except Exception as ex:self.last_error='Не удалось записать остановку: '+str(ex)
            self.phase='stopped' if self.journal else 'disabled'
        self.wake.set()
    def status(self):
        with self.lock:
            return dict(enabled=self.enabled(),active=self.active,master=self.master,phase=self.phase,error=self.error,
                        last_error=self.last_error,health=dict(self.health),pings=dict(self.pings),journal=self.path,
                        counts=self.journal.stats() if self.journal else {},delivery='ordered_batched_send; not_exactly_once',
                        compact=self.e.cfg.get('recovery_compact',False),compacted=self.journal.compacted if self.journal else 0,
                        replayed_commands=self.replayed_commands,replay_batches=self.replay_batches,
                        destination_ready={s:self._destination_ready(s) for s in 'AB'},rate_limit=1000)
    def observe_reply(self,side,kind,source,f):
        with self.lock:pair=self.rpc.get((side,kind))
        return bool(pair and pair[0] is source and pair[1].observe(f))
    def _consider_master(self,side):
        # Catch the first surviving-console action even before the next monitor tick.
        if self.active and side!=self.master and not self.both_lost and not self._ready(self.master) and self._ready(side):
            with self.lock:
                if self.active and side!=self.master and not self._ready(self.master):
                    self.loss_seen=True;self.master=side;self.role+=1;self.expected.clear();self.scene_echo.clear();self.layouts[self.master]=dict(self.e.surfaces.layouts[self.master]);self._direction()
                    self._persist()
                    self.journal.mark('master_changed',master=side,reason='first_surviving_action')
                    self.e.event('recovery_master_changed',side,direction=self.e.cfg['direction'])
    def _append(self,side,cmd):
        self._consider_master(side)
        with self.lock:
            if not self.active or side!=self.master:return
            if not all(self._ready(s) for s in 'AB'):self._persist()
            dest='B' if side=='A' else 'A'
            compact=self.e.cfg.get('recovery_compact',False) and not self._destination_ready(dest)
            try:self.journal.append(side,cmd,compact=compact)
            except Exception as ex:self._fault('Запись журнала остановлена: '+str(ex));return
        self.wake.set()
    def rack(self,side,parts,bridge,transport='tcp'):
        if not self.active:return
        self._consider_master(side)
        from allen_heath_dlive_full_mirroring import classify
        for kind,raw in parts:
            if kind=='service':continue
            if kind!='f1':self._append(side,dict(kind='opaque',raw=raw.hex(),transport=transport,restorable=not self.loss_seen));continue
            label,f=classify(raw,bridge)
            if label in ('service','telemetry'):continue
            name=object_name(bridge.captured,f)
            if name in ('StageBox Scene Manager','Last Show Recalled Manager'):continue
            payload=bytes.fromhex(f['payload'])
            if phantom(name,f['target'],payload) or (not name and f['target']==0x1003 and len(payload)==1):
                self.e.event('phantom_blocked',side,name=name,target=hex(f['target']));continue
            if label=='captured_parameter':
                sig=(side,'rack',name,f['target'],f['payload'])
                with self.lock:
                    if name=='FX Global Tap Tempo' and f['target']==0x1000 and len(payload)==2:
                        period=int.from_bytes(payload,'big');previous=self.global_seen.get(side);self.global_seen[side]=period
                        if previous is None or previous==period:continue
                        if self.expected.get(sig,0)<=time.monotonic():
                            self.expected={s:t for s,t in self.expected.items() if s[:4]!=sig[:4]}
                    if self.expected.get(sig,0)>time.monotonic():continue
                self._append(side,dict(kind='rack',name=name,target=f['target'],payload=f['payload']))
            else:self._append(side,dict(kind='opaque',raw=raw.hex(),transport=transport,restorable=not self.loss_seen))
    def surface(self,side,name,f,source):
        if not self.active:return False
        self._consider_master(side)
        from surface_link import OBSERVE_ONLY,BANKS,LOCAL_STORAGE
        if source is not self.e.surfaces.sessions.get(side):return True
        if name in LOCAL_STORAGE:return True
        value=bytes.fromhex(f['payload']);target=f['target']
        if name in ORDER and target==0x1001:
            try:w=width(value)
            except ValueError:return True
            before=self.layouts[side].get(name)
            self.e.surfaces.layouts[side][name]=value
            # Delayed standby echoes must not roll the planned write shadow backwards.
            if side==self.master or before is None:self.layouts[side][name]=value
            if before is None or before==value or side!=self.master or not self.e.cfg.get('surface_banks'):return True
            if len(before)!=len(value):self._fault('Размер банка изменился; требуется новая сессия');return True
            widths={n:width(v) for n,v in self.layouts[side].items()}
            if set(widths)!=set(ORDER):return True
            offset=sum(widths[n] for n in ORDER[:ORDER.index(name)]);changes=[]
            for layer in range(6):
                for p in range(w):
                    i=(layer*w+p)*2
                    if before[i:i+2]!=value[i:i+2]:changes.append([layer,offset+p,value[i:i+2].hex()])
            self._append(side,dict(kind='layout',changes=changes));return True
        if name=='Surface Scene Manager' and target==0x100c and len(value)==2:
            index=int.from_bytes(value,'big')
            if self.e.cfg.get('scene_sync') and index<500 and self.scene_echo.get((side,index),0)<time.monotonic():
                self._append(side,dict(kind='scene_request',index=index))
            return True
        if name in OBSERVE_ONLY or (name not in BANKS and not self.e.cfg.get('surface_experimental')):return True
        if phantom(name,target,value):self.e.event('phantom_blocked',side,name=name);return True
        sig=(side,'surface',name,target,f['payload'])
        with self.lock:
            if self.expected.get(sig,0)>time.monotonic():return True
        self._append(side,dict(kind='surface',name=name,target=target,payload=f['payload']));return True
    def _endpoint(self,side,kind,token,role=None):
        e=self.e;source=e.bridges[side] if kind=='rack' else e.surfaces.sessions[side]
        obj=source.captured.id_for('StageBox Scene Manager') if kind=='rack' else source.by_name.get('Surface Scene Manager')
        client=source._client_id if kind=='rack' else source.client
        if not obj:raise RuntimeError('Не найден объект сцены '+side)
        def guard():
            if not self.active or token!=self.token or (role is not None and role!=self.role) or not self._ready(side):raise RuntimeError('Сессия восстановления изменилась')
            if source is not (e.bridges.get(side) if kind=='rack' else e.surfaces.sessions.get(side)):raise RuntimeError('Сессия заменена')
        def send(target,payload):
            guard()
            if kind=='rack':source.send_raw(frame(2,obj,client,target,payload),'tcp')
            else:source._f1(obj,client,target,payload)
        with self.lock:
            pair=self.rpc.get((side,kind))
            if pair and pair[0] is source:pair[1].guard=guard;pair[1].send=send;return pair[1]
            rpc=SceneRPC(kind,obj,client,send,guard);self.rpc[(side,kind)]=(source,rpc);return rpc
    @staticmethod
    def _pack(scene):return dict(kind=scene.kind,headers=[x.hex() for x in scene.headers],body=scene.body.hex())
    @staticmethod
    def _unpack(v):return Scene(v['kind'],tuple(bytes.fromhex(x) for x in v['headers']),bytes.fromhex(v['body']))
    def _apply(self,side,cmd,token,role=None):
        dest='B' if side=='A' else 'A';e=self.e;k=cmd['kind']
        if not self.active or token!=self.token or (role is not None and role!=self.role) or not self._destination_ready(dest):raise RuntimeError('Роль или готовность назначения изменились')
        if k in ('rack','surface'):
            obj=e.bridges[dest].captured.id_for(cmd['name']) if k=='rack' else e.surfaces.sessions[dest].by_name.get(cmd['name'])
            if not obj:raise ValueError('Нет объекта назначения '+cmd['name'])
            payload=bytes.fromhex(cmd['payload'])
            if phantom(cmd['name'],cmd['target'],payload):return
            with self.lock:
                signature=(dest,k,cmd['name'],cmd['target'],cmd['payload'])
                if cmd['name']=='FX Global Tap Tempo' and cmd['target']==0x1000:
                    self.expected={s:t for s,t in self.expected.items() if s[:4]!=signature[:4]}
                self.expected[signature]=time.monotonic()+5
            if k=='rack':e.bridges[dest].send_raw(frame(2,obj,e.bridges[dest]._client_id,cmd['target'],payload),'tcp')
            else:e.surfaces.sessions[dest].write_property(cmd['name'],cmd['target'],payload,e.surfaces.sessions[dest].generation)
        elif k=='layout':
            banks={n:bytearray(v) for n,v in self.layouts[dest].items()};changed=set();widths={n:width(v) for n,v in banks.items()}
            for layer,pos,code in cmd['changes']:
                remaining=pos
                for n in ORDER:
                    w=widths[n]
                    if remaining<w:
                        i=(layer*w+remaining)*2;banks[n][i:i+2]=bytes.fromhex(code);changed.add(n);break
                    remaining-=w
                else:e.event('recovery_strip_unmapped',side,layer=layer+1,strip=pos+1)
            for n in ORDER:
                if n in changed:
                    value=bytes(banks[n]);s=e.surfaces.sessions[dest];s.write_bank(n,0x1001,value,s.generation)
                    self.layouts[dest][n]=value
        elif k=='opaque':
            if self.loss_seen or not cmd['restorable']:raise ValueError('Неизвестные байты сохранены, но повтор после переподключения требует сопоставления')
            e.bridges[dest].send_raw(bytes.fromhex(cmd['raw']),cmd.get('transport','tcp'))
        elif k=='scene':
            scenes={kind:self._unpack(v) for kind,v in cmd['parts'].items()};rpcs={kind:self._endpoint(dest,kind,token,role) for kind in ('rack','surface')}
            backup={kind:rpc.read(cmd['index']) for kind,rpc in rpcs.items()}
            scenes['surface'],_=adapt_surface(scenes['surface'],{n:width(v) for n,v in self.layouts[dest].items()})
            scenes={kind:preserve_scene_preamps(scenes[kind],backup[kind])[0] for kind in scenes}
            path=pathlib.Path(self.path).with_name('recovery_scene_backup_'+str(time.time_ns())+'.json')
            path.write_text(json.dumps({kind:self._pack(s) for kind,s in backup.items()}))
            self.scene_echo[(dest,cmd['index'])]=time.monotonic()+30
            for kind in ('surface','rack'):rpcs[kind].write(cmd['index'],scenes[kind])
            for kind in ('surface','rack'):
                if rpcs[kind].read(cmd['index'])!=scenes[kind]:raise ValueError('Сцена после записи отличается: '+kind)
        else:raise ValueError('Неизвестная операция журнала')
    def _work(self):
        while not self.closed.is_set():
            self.wake.wait(.05);self.wake.clear()
            try:
                progressed=self._work_batch()
                if progressed is None:progressed=self._work_one()
                if progressed:
                    delay=max(0,self.next_batch-time.monotonic())
                    if delay:self.closed.wait(min(delay,.05))
                    self.wake.set()
            except Exception as ex:
                if self.active:self._fault('Ошибка журнала восстановления: '+str(ex))
    def _work_batch(self):
        # Same gate->controller lock order as packet reception. Selection,
        # compaction exclusion, fresh addressing and write are one fenced step.
        with self.io,self.e.gate,self.lock:
            if not self.active or self.error or self.both_lost:return False
            journal=self.journal;rows=journal.pending(32)
            if not rows:return False
            first=rows[0];seq,side,cmd,state=first
            if cmd['kind']!='rack':return None
            if side!=self.master:
                journal.set(seq,'superseded','surviving console became authoritative');return True
            if state!='pending':self._fault('Неопределённый результат отправки; журнал сохранён');return False
            dest='B' if side=='A' else 'A'
            if not self._destination_ready(dest):return False
            bridge=self.e.bridges[dest];frames=[];selected=[];signatures=[];total=0
            for row in rows:
                number,source,value,state=row
                if source!=side or state!='pending' or value['kind']!='rack':break
                payload=bytes.fromhex(value['payload'])
                if phantom(value['name'],value['target'],payload):
                    if selected:break
                    journal.set(number,'blocked_48v');self.e.event('phantom_blocked',side,name=value['name']);return True
                lookup=getattr(bridge.captured,'writable_id_for',bridge.captured.id_for)
                obj=lookup(value['name'])
                if not obj:
                    if bridge.probe_status().get('phase') in ('discovering','registering'):break
                    raise ValueError('Нет объекта назначения '+value['name'])
                raw=frame(2,obj,bridge._client_id,value['target'],payload)
                if total+len(raw)>32768 and selected:break
                frames.append(raw);selected.append(number);total+=len(raw)
                signatures.append((dest,'rack',value['name'],value['target'],value['payload']))
            if not selected:return False
            journal.set_many(selected,'sending')
            for signature in signatures:
                if signature[2]=='FX Global Tap Tempo' and signature[3]==0x1000:
                    self.expected={s:t for s,t in self.expected.items() if s[:4]!=signature[:4]}
                self.expected[signature]=time.monotonic()+5
            try:bridge.send_raw(b''.join(frames),'tcp')
            except Exception as ex:
                # Only explicitly absolute state controls may be retried after a
                # possibly partial TCP write. Other commands require inspection.
                retry=all(state_key(row[2]) for row in rows[:len(selected)]) and not self._destination_ready(dest)
                journal.set_many(selected,'pending' if retry else 'uncertain',str(ex));self.loss_seen=True
                if not retry:self._fault(str(ex))
                return False
            journal.set_many(selected,'sent');self.replayed_commands+=len(selected);self.replay_batches+=1
            self.next_batch=time.monotonic()+len(selected)/1000
            self.e.event('sent_'+side+'_to_'+dest,side,recovery=True,commands=len(selected),bytes=total)
            self.e.event('recovery_batch_sent',side,destination=dest,commands=len(selected),first_sequence=selected[0],last_sequence=selected[-1])
            return True
    def _work_one(self):
        with self.lock:
            if not self.active or self.error or self.both_lost:return
            journal=self.journal;row=journal.first();token=self.token;master=self.master;role=self.role
        if not row:return
        seq,side,cmd,state=row
        if side!=master:journal.set(seq,'superseded','surviving console became authoritative');return True
        if state=='uncertain':self._fault('Неопределённый результат предыдущей отправки; журнал сохранён');return
        dest='B' if side=='A' else 'A';sending=False
        try:
            if cmd['kind']=='scene_request':
                scenes={k:self._endpoint(side,k,token,role).read(cmd['index']) for k in ('rack','surface')}
                # Never substitute a later store for the event silently. Require a stable second read.
                for k in ('rack','surface'):
                    if self._endpoint(side,k,token,role).read(cmd['index'])!=scenes[k]:raise ValueError('Сцена изменилась во время чтения')
                cmd=dict(kind='scene',index=cmd['index'],parts={k:self._pack(v) for k,v in scenes.items()})
                journal.set(seq,'pending',body=cmd)
            if not self._destination_ready(dest):return False
            with self.io:
                if not self.active or token!=self.token or role!=self.role:return
                journal.set(seq,'sending');sending=True
                if cmd['kind']=='scene':self._apply(side,cmd,token,role)
                else:
                    with self.e.gate,self.lock:self._apply(side,cmd,token,role)
                journal.set(seq,'sent')
            self.e.event('sent_'+side+'_to_'+dest,side,recovery=True)
            self.replayed_commands+=1
            if cmd['kind']=='scene':self.e.event('recovery_scene_verified',side,scene=cmd['index']+1)
            self.e.event('recovery_command_sent',side,sequence=seq,destination=dest,command_kind=cmd['kind'],application_verified=cmd['kind']=='scene')
            return True
        except Exception as ex:
            with self.lock:
                stale=not self.active or token!=self.token or role!=self.role
                # Socket failure can happen after a write reached the hardware.
                # Keep the uncertain row; never silently repeat a non-idempotent operation.
                retry_state=(not sending or (cmd['kind'] in ('rack','surface','layout') and not self._ready(dest)))
                journal.set(seq,'pending' if retry_state else 'uncertain',str(ex))
                if stale:return
                self.loss_seen=True
                if (sending and not retry_state) or self._ready(dest):self._fault(str(ex))
    def _fault(self,message):
        with self.lock:
            self.error=message;self.last_error=message;self.phase='blocked';self._persist()
        self.e.event('recovery_blocked',error=message)
    def _monitor(self):
        while not self.closed.wait(.25):
            try:self._tick()
            except Exception as ex:
                if self.active:self._fault('Контроль соединений: '+str(ex))
                else:self.last_error=str(ex)
    def _tick(self):
        if not self.e.bridges:return
        self.e.poll_global_tempo()
        now=time.monotonic()
        if now-self.last_ping>=1:
            self.last_ping=now
            for side in 'AB':
                for kind,field in (('rack','ip'),('surface','surface_ip')):
                    key=side+'_'+kind;job=self.ping_jobs.get(key)
                    if job and job.done():
                        try:self.pings[key]=job.result()
                        except Exception as ex:self.pings[key]=dict(ok=False,error=str(ex))
                    if not job or job.done():self.ping_jobs[key]=self.pool.submit(ping,self.e.cfg[side][field],self.e.cfg[side].get('local_ip',''))
        if not self.active:return
        ready={s:self._ready(s) for s in 'AB'}
        with self.lock:
            if not self.active:return
            for side in 'AB':
                if self.health.get(side) and not ready[side]:
                    self._persist()
                    self.loss_seen=True;self.journal.mark('connection_lost',side=side,master=self.master)
                    self.e.event('recovery_connection_lost',side)
            if not any(ready.values()):self.both_lost=True;self.phase='both_unavailable'
            elif not self.both_lost and not ready[self.master]:
                peer='B' if self.master=='A' else 'A'
                if ready[peer]:
                    self.master=peer;self.role+=1;self.expected.clear();self.scene_echo.clear();self.layouts[self.master]=dict(self.e.surfaces.layouts[self.master]);self._direction();self.journal.mark('master_changed',master=peer)
                    self.e.event('recovery_master_changed',peer,direction=self.e.cfg['direction'])
            self.expected={k:v for k,v in self.expected.items() if v>now}
            self.scene_echo={k:v for k,v in self.scene_echo.items() if v>now}
            self.health=ready
            if not self.error and not self.both_lost:
                peer='B' if self.master=='A' else 'A'
                self.phase=('catching_up' if self.journal.first() else 'subscribing') if self._destination_ready(peer) and not ready[peer] else ('recording' if not all(ready.values()) else ('catching_up' if self.journal.first() else 'live'))
        for side in 'AB':
            b=self.e.bridges.get(side);s=self.e.surfaces.sessions.get(side)
            broken=not b or not b.status()['connected'] or not s or not s.status()['connected'] or s.status().get('silence_seconds',0)>4
            key=(side,id(b),id(s));first=self.first_seen.setdefault(key,now)
            if b and b.status().get('udp',{}).get('since_from_sec') is None and now-first>4:broken=True
            if not ready[side] and now-first>90:broken=True
            if b:broken=broken or not b.status().get('udp',{}).get('running',True) or b.probe_status().get('phase') in ('error','partial_requests_sent')
            if s:broken=broken or s.status().get('phase') in ('error','failed')
            if b and b.status().get('udp',{}).get('since_from_sec',0) is not None:
                broken=broken or b.status().get('udp',{}).get('since_from_sec',0)>4
            thread=self.retries.get(side)
            if broken and now>=self.retry_at.get(side,0) and (not thread or not thread.is_alive()):
                token=self.token;self.retry_at[side]=now+3
                thread=threading.Thread(target=self._reconnect,args=(side,token),daemon=True);self.retries[side]=thread;thread.start()
        if self.cleanup_due:self.purge_logs()
        self.wake.set()
    def _reconnect(self,side,token):
        e=self.e
        with e.control:
            if not self.active or token!=self.token:return
            old=e.bridges.get(side);surface=e.surfaces.sessions.get(side)
            bs=old.status() if old else {};ss=surface.status() if surface else {};udp=bs.get('udp',{})
            rack_bad=(not bs.get('connected') or not udp.get('running',True) or udp.get('since_from_sec') is None or
                      udp.get('since_from_sec',0)>4 or old.probe_status().get('phase') in ('error','partial_requests_sent'))
            surface_bad=not ss.get('connected') or ss.get('silence_seconds',0)>4 or ss.get('phase') in ('error','failed')
            key=(side,id(old),id(surface));stalled=time.monotonic()-self.first_seen.get(key,time.monotonic())>90
            if stalled:
                rack_bad=rack_bad or old.probe_status().get('phase')!='requests_sent'
                surface_bad=surface_bad or ss.get('phase')!='ready' or not all(n in e.surfaces.layouts[side] for n in ORDER)
            if not rack_bad and not surface_bad:return
            self.e.event('recovery_reconnect_attempt',side,rack=rack_bad,surface=surface_bad)
            if rack_bad and old:
                old.disk_log_enabled=lambda:False
                old.disconnect()
            if surface_bad and surface:surface.disconnect()
            for kind in ('rack','surface'):
                if not (rack_bad if kind=='rack' else surface_bad):continue
                with self.lock:
                    pair=self.rpc.pop((side,kind),None)
                    if pair:pair[1].cancel()
            b=e.factory(side,e.cfg[side],e.packet,e.event) if rack_bad else old
            if rack_bad:b.recovery_priority_names=self.journal.names()
            if surface_bad:
                s=e.surfaces.factory(side,{**e.cfg[side],'surface_controls_observe':e.cfg.get('surface_controls_observe',False)},e.surfaces.property,e.event)
                s.on_frame=e.surfaces.on_frame
            else:s=surface
            with e.gate:
                if not self.active or token!=self.token:return
                e.bridges[side]=b;e.surfaces.sessions[side]=s
                if surface_bad:e.surfaces.layouts[side]={};self.layouts[side]={}
            def connect_rack():
                b.serve();b.connect()
                if self.active and token==self.token and b.status()['connected']:b.probe_request(e.cfg['probe'])
            try:
                # Native connections are independent. A rack timeout must not
                # delay starting the Surface connection on the same system.
                if rack_bad and surface_bad:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        jobs=[pool.submit(connect_rack),pool.submit(s.connect)]
                        for job in jobs:job.result()
                elif rack_bad:connect_rack()
                else:s.connect()
            except Exception as ex:e.event('recovery_reconnect_failed',side,error=str(ex))
            finally:
                if not self.active or token!=self.token:
                    if rack_bad:b.disconnect()
                    if surface_bad:s.disconnect()
    def close(self):
        self.stop();self.closed.set();self.wake.set();self.monitor.join(timeout=2);self.worker.join(timeout=2)
        self.pool.shutdown(wait=False,cancel_futures=True)
        if not self.worker.is_alive():
            for journal in self.journals:journal.close()
