"""48V stays local. Only observed analogue-input target 0x1003 is blocked live."""
from scene_sync import Scene,records

def analogue(name):
    return bool(name and 'analogue input' in name.lower())

def phantom(name,target,payload):
    return analogue(name) and target==0x1003

def preserve_scene_preamps(source,destination):
    """Conservative: keep entire local preamp records, not guessed phantom offsets."""
    src=records(source.body);dst=dict(records(destination.body));result=[];kept=0
    for name,data in src:
        if analogue(name):
            if name not in dst:
                raise ValueError('48V: нет локального блока предусилителя в сцене назначения: '+name)
            data=dst[name];kept+=1
        raw=name.encode('ascii')+b'\0'+data
        result.append(len(raw).to_bytes(2,'big')+raw)
    body=b''.join(result)
    return Scene(source.kind,(*source.headers[:-1],len(body).to_bytes(4,'big')),body),kept

def object_name(captured,f):
    if not captured:return None
    # op3 is client->object notification addressing, op2 addresses object first.
    first,second=(f['b'],f['a']) if f['op']==3 else (f['a'],f['b'])
    return captured.name_for(first) or captured.name_for(second)
