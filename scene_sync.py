"""Experimental saved-scene transfer, from captured 2.12 0x105/107/108 streams.

No current-state Store shortcut. Reads and compares complete serialized scenes.
No cross-device atomic transaction exists: partial writes are reported explicitly.
"""
import collections
import hashlib
import threading
import time
from dataclasses import dataclass
from ahnet_lab import frame
from layout_map import ORDER

MAX_SCENE = 8 * 1024 * 1024
HEADERS = {'surface': (328, 576, 4), 'rack': (328, 1, 1, 5, 1, 1, 96, 804, 4403, 4)}


@dataclass(frozen=True)
class Scene:
    kind: str
    headers: tuple
    body: bytes

    @property
    def name(self):
        return self.headers[0].split(b'\0',1)[0].decode('utf-8',errors='replace')

    def chunks(self):
        return list(self.headers) + [self.body[p:p+35000] for p in range(0,len(self.body),35000)]

    def digest(self):
        return hashlib.sha256(b''.join(self.headers)+self.body).hexdigest()


class SceneCollector:
    def __init__(self,kind):
        self.kind=kind;self.parts=[];self.body=bytearray();self.expected=None;self.value=None

    def feed(self,payload):
        lengths=HEADERS[self.kind]
        if len(self.parts)<len(lengths):
            if len(payload)!=lengths[len(self.parts)]:
                raise ValueError('Неожиданный заголовок сцены: '+str((self.kind,len(self.parts),len(payload))))
            self.parts.append(payload)
            if len(self.parts)==len(lengths):
                self.expected=int.from_bytes(payload,'big')
                if self.expected>MAX_SCENE:raise ValueError('Сцена превышает лимит 8 МиБ')
        else:
            if not payload or len(payload)>35000:raise ValueError('Некорректный блок данных сцены')
            self.body.extend(payload)
            if len(self.body)>self.expected:raise ValueError('Лишние байты в сцене')
        if self.expected is not None and len(self.body)==self.expected:
            self.value=Scene(self.kind,tuple(self.parts),bytes(self.body))
        return self.value


def records(body):
    result=[];pos=0
    while pos<len(body):
        if pos+2>len(body):raise ValueError('Оборванная длина записи Surface')
        n=int.from_bytes(body[pos:pos+2],'big');pos+=2
        record=body[pos:pos+n];pos+=n
        if not n or len(record)!=n or b'\0' not in record:raise ValueError('Некорректная запись Surface')
        name,data=record.split(b'\0',1)
        result.append((name.decode('ascii'),data))
    if len({n for n,_ in result})!=len(result):raise ValueError('Повторяющиеся записи Surface')
    return result


def adapt_surface(scene,dest_widths):
    """Observed name-length-value records; bank data is 01,width,6*width pairs."""
    rs=records(scene.body);banks={}
    for name,data in rs:
        if name in ORDER:
            if len(data)<2 or data[0]!=1 or len(data)!=2+12*data[1]:
                raise ValueError('Неизвестный формат сохранённой раскладки '+name)
            banks[name]=(data[1],data[2:])
    if set(banks)!=set(ORDER) or set(dest_widths)!=set(ORDER):
        raise ValueError('Нет геометрии всех трёх банков сцены')
    sw=[banks[n][0] for n in ORDER];dw=[int(dest_widths[n]) for n in ORDER]
    if any(w<0 or w>64 for w in dw):raise ValueError('Некорректная ширина банка')
    # Map absolute strip positions for each layer; extra destination positions blank.
    layers=[]
    for layer in range(6):
        row=b''.join(banks[n][1][layer*w*2:(layer+1)*w*2] for n,w in zip(ORDER,sw))
        layers.append(row[:sum(dw)*2].ljust(sum(dw)*2,b'\0'))
    converted={};offset=0
    for n,w in zip(ORDER,dw):
        converted[n]=b'\x01'+bytes([w])+b''.join(row[offset*2:(offset+w)*2] for row in layers)
        offset+=w
    encoded=[]
    for name,data in rs:
        record=name.encode('ascii')+b'\0'+converted.get(name,data)
        if len(record)>65535:raise ValueError('Слишком длинная запись Surface')
        encoded.append(len(record).to_bytes(2,'big')+record)
    body=b''.join(encoded)
    headers=(*scene.headers[:-1],len(body).to_bytes(4,'big'))
    return Scene('surface',headers,body),max(0,sum(sw)-sum(dw))*6


class SceneRPC:
    """One outstanding untagged RPC per endpoint. Timeout quarantines until reconnect."""
    def __init__(self,kind,obj,client,send,guard,timeout=8):
        self.kind=kind;self.obj=obj;self.client=client;self.send=send;self.guard=guard
        self.timeout=timeout;self.lock=threading.RLock();self.serial=threading.Lock()
        self.done=threading.Event();self.pending=None;self.error=None;self.result=None;self.poisoned=False

    def observe(self,f):
        if (f['op'],f['a'],f['b'],f['target'])!=(3,self.client,self.obj,2):return False
        with self.lock:
            if self.pending is None:return False
            try:
                payload=bytes.fromhex(f['payload'])
                if self.pending=='ack':
                    if payload:raise ValueError('Неожиданный ответ записи сцены')
                    self.result=True;self.done.set()
                else:
                    value=self.pending.feed(payload)
                    if value is not None:self.result=value;self.done.set()
            except ValueError as ex:self.error=str(ex);self.poisoned=True;self.done.set()
            return True

    def cancel(self):
        with self.lock:
            if self.pending is not None:
                self.error='Передача сцен остановлена';self.poisoned=True;self.done.set()

    def _call(self,pending,requests):
        with self.serial:
            with self.lock:
                if self.poisoned:raise RuntimeError('Переподключи системы после ошибки RPC сцены')
                self.pending=pending;self.error=None;self.result=None;self.done.clear()
            try:
                for target,payload in requests:
                    self.guard();self.send(target,payload)
                if not self.done.wait(self.timeout):
                    self.poisoned=True;raise RuntimeError('Тайм-аут RPC сцены; требуется переподключение')
                self.guard()
                if self.error:raise RuntimeError(self.error)
                return self.result
            except Exception:
                self.poisoned=True;raise
            finally:
                with self.lock:self.pending=None

    def read(self,index):
        return self._call(SceneCollector(self.kind),[(0x105,index.to_bytes(2,'big'))])

    def write(self,index,scene):
        if scene.kind!=self.kind:raise ValueError('Неверный тип сцены')
        bracket=self.kind=='rack'
        if bracket:self.guard();self.send(0x1028,b'\x01')
        try:
            return self._call('ack',[(0x107,index.to_bytes(2,'big'))]+[(0x108,p) for p in scene.chunks()])
        finally:
            if bracket:
                self.guard();self.send(0x1028,b'\x00')


class SceneSync:
    def __init__(self,engine):
        self.engine=engine;self.lock=threading.RLock();self.wake=threading.Event();self.closed=threading.Event()
        self.pending={};self.endpoints={};self.expected={};self.counts=collections.Counter()
        self.state=dict(phase='idle',error=None,last_result=None,active=None,last_error=None)
        self.thread=threading.Thread(target=self._run,daemon=True,name='scene-sync');self.thread.start()

    def status(self):
        with self.lock:
            return dict(self.state,counts=dict(self.counts),pending=len(self.pending),
                        enabled=self.engine.cfg.get('scene_sync',False),hardware_verified=False,
                        policy='read_saved_source; adapt_surface_banks; backup_destination; write; readback_compare; no_automatic_retry')

    def stop(self):
        with self.lock:
            self.pending.clear();self.expected.clear()
            for _,rpc in self.endpoints.values():rpc.cancel()
            if self.state['phase'] not in ('error','partial'):self.state['phase']='stopped'
        self.wake.set()

    def reset(self):
        self.stop()
        with self.lock:self.endpoints.clear();self.state.update(phase='idle',error=None,active=None)

    def require_ready(self):
        with self.lock:
            if self.state['phase'] in ('error','partial') or any(rpc.poisoned for _,rpc in self.endpoints.values()):
                raise RuntimeError('Переподключи системы после ошибки или прерывания передачи сцены')
        for side in ('A','B'):
            rack=self.engine.bridges[side]
            if not rack.captured or not rack.captured.id_for('StageBox Scene Manager'):
                raise RuntimeError('Нет подписки StageBox Scene Manager на '+side)
            if not self.engine.surfaces.sessions.get(side) or not self.engine.surfaces.sessions[side].by_name.get('Surface Scene Manager'):
                raise RuntimeError('Нет Surface Scene Manager на '+side)

    def updated(self,side,index):
        e=self.engine;now=time.monotonic()
        if not e.running or not e.cfg.get('scene_sync') or not 0<=index<500 or getattr(e.surfaces,'error',None):return
        if e.cfg['direction']==({'A':'B_to_A','B':'A_to_B'}[side]):return
        with self.lock:
            self.expected={k:v for k,v in self.expected.items() if v>now}
            if self.expected.get((side,index),0)>now:
                self.counts['echo_suppressed']+=1;return
            if self.state['phase'] in ('error','partial'):return
            if len(self.pending)>=500 and (side,index) not in self.pending:return
            self.pending[(side,index)]=(now+.5,e.epoch)
            self.counts['updates']+=1
        e.event('scene_update_queued',side,scene=index+1);self.wake.set()

    def observe(self,side,kind,source,f):
        with self.lock:pair=self.endpoints.get((side,kind))
        if pair and pair[0] is source:return pair[1].observe(f)
        return False

    def _endpoint(self,side,kind,epoch):
        e=self.engine
        source=e.bridges[side] if kind=='rack' else e.surfaces.sessions[side]
        if kind=='rack':
            obj=source.captured.id_for('StageBox Scene Manager');client=source._client_id;op=2
        else:obj=source.by_name.get('Surface Scene Manager');client=source.client;op=1
        if not obj:raise RuntimeError('Не найден объект сцены '+side+' '+kind)
        def guard():
            if not e.running or e.epoch!=epoch:raise RuntimeError('Сессия передачи сцен остановлена или заменена')
            current=e.bridges.get(side) if kind=='rack' else e.surfaces.sessions.get(side)
            if current is not source or not source.status()['connected'] or getattr(e.surfaces,'error',None):raise RuntimeError('Соединение сцены потеряно')
        def send(target,payload):
            with e.gate:
                guard()
                if kind=='rack':source.send_raw(frame(op,obj,client,target,payload),'tcp')
                else:source._f1(obj,client,target,payload)
        with self.lock:
            pair=self.endpoints.get((side,kind))
            if pair and pair[0] is source:
                pair[1].guard=guard;pair[1].send=send;return pair[1]
            rpc=SceneRPC(kind,obj,client,send,guard);self.endpoints[(side,kind)]=(source,rpc);return rpc

    def _run(self):
        while not self.closed.is_set():
            self.wake.wait(.1);self.wake.clear()
            with self.lock:
                ready=next(((key,v) for key,v in self.pending.items() if time.monotonic()>=v[0]),None)
                if ready is None:continue
                key,(_,epoch)=ready;self.pending.pop(key,None)
            side,index=key;e=self.engine;dest='B' if side=='A' else 'A'
            if not e.running or e.epoch!=epoch:continue
            written=[];backup={}
            try:
                with self.lock:self.state.update(phase='reading',error=None,active=dict(side=side,destination=dest,scene=index+1))
                endpoints={(s,k):self._endpoint(s,k,epoch) for s in (side,dest) for k in ('rack','surface')}
                scenes={kind:endpoints[(side,kind)].read(index) for kind in ('rack','surface')}
                if any(endpoints[(side,k)].read(index)!=scenes[k] for k in ('rack','surface')):
                    raise RuntimeError('Сцена изменилась во время чтения; запись отменена')
                widths={n:len(e.surfaces.layouts[dest][n])//12 for n in ORDER}
                scenes['surface'],dropped=adapt_surface(scenes['surface'],widths)
                for kind in ('rack','surface'):backup[kind]=endpoints[(dest,kind)].read(index)
                from safety_filter import preserve_scene_preamps
                scenes={k:preserve_scene_preamps(v,backup[k])[0] for k,v in scenes.items()}
                # Keep an exact recovery file before first destination write.
                import json,pathlib
                path=pathlib.Path(e.log_path).parent/('scene_backup_'+str(time.time_ns())+'_'+dest+'_'+str(index+1)+'.json')
                path.write_text(json.dumps(dict(scene=index+1,side=dest,parts={k:dict(headers=[p.hex() for p in v.headers],body=v.body.hex()) for k,v in backup.items()})),encoding='utf-8')
                e.event('scene_destination_backup',dest,scene=index+1,path=str(path))
                with self.lock:
                    self.state['phase']='writing';self.state['backup']=str(path);self.expected[(dest,index)]=time.monotonic()+30
                # Captured Director ordering: Surface part, then MixRack part.
                for kind in ('surface','rack'):
                    written.append(kind) # even a failed write may have modified the destination
                    endpoints[(dest,kind)].write(index,scenes[kind])
                with self.lock:self.state['phase']='verifying'
                for kind in ('surface','rack'):
                    got=endpoints[(dest,kind)].read(index)
                    if got!=scenes[kind]:raise RuntimeError('Прочитанная сцена отличается от отправленной: '+kind)
                result=dict(scene=index+1,source=side,destination=dest,name=scenes['rack'].name,
                            hashes={k:v.digest() for k,v in scenes.items()},unmapped_layer_positions=dropped,
                            readback_verified=True,hardware_verified=False,backup=str(path))
                with self.lock:self.state.update(phase='ready',active=None,last_result=result);self.counts['verified']+=1
                e.event('scene_copy_readback_verified',side,**result)
            except Exception as ex:
                if e.epoch!=epoch:
                    e.event('scene_copy_cancelled',side,scene=index+1,possibly_written=written,error=str(ex));continue
                phase='partial' if written else 'error'
                with self.lock:
                    self.pending.clear();self.state.update(phase=phase,error=str(ex),active=None,
                        last_error=dict(scene=index+1,source=side,destination=dest,error=str(ex),phase=phase,possibly_written=written))
                    self.counts['failed']+=1
                e.event('scene_copy_'+phase,side,scene=index+1,destination=dest,error=str(ex),possibly_written=written,
                        automatic_rollback=False,reason='No atomic scene transaction; destination backup retained')

    def close(self):
        self.closed.set();self.stop();self.wake.set();self.thread.join(timeout=2)
