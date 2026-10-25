"""Six-layer, two-byte strip entries observed in the native Surface readback.

Channel codes stay opaque. Match strip positions left-to-right on each layer,
using the measured width of each bank; never truncate a flat byte array.
"""
ORDER=('Channel Left Bank Switcher','Channel Middle Bank Switcher','Channel Right Bank Switcher')
LAYERS=6


def width(raw):
    if len(raw) % (LAYERS*2) or len(raw)>LAYERS*2*64:
        raise ValueError('Неподдерживаемая длина раскладки Surface: '+str(len(raw)))
    return len(raw)//(LAYERS*2)


def plan_changes(name,before,after,source_banks,destination_banks):
    """Return destination bank payloads and unmappable (layer, strip) pairs.

    Both complete baselines are required. Preserve unrelated destination slots.
    Never claim to copy slots beyond the destination's physical capacity.
    """
    if name not in ORDER:raise ValueError('Unknown bank')
    if any(n not in b for b in (source_banks,destination_banks) for n in ORDER):
        raise ValueError('Нет исходной раскладки всех банков обеих Surface')
    sw={n:width(source_banks[n]) for n in ORDER}
    dw={n:width(destination_banks[n]) for n in ORDER}
    w=width(before)
    if width(after)!=w or sw[name]!=w:
        raise ValueError('Размер банка изменился; переподключи Surface')
    source_offset=sum(sw[n] for n in ORDER[:ORDER.index(name)])
    output={n:bytearray(destination_banks[n]) for n in ORDER}
    missing=[];changed=[]
    for layer in range(LAYERS):
        for slot in range(w):
            i=2*(layer*w+slot)
            if before[i:i+2]==after[i:i+2]:continue
            logical=source_offset+slot;remaining=logical;target=None
            for bank in ORDER:
                if remaining<dw[bank]:target=bank;break
                remaining-=dw[bank]
            if target is None:
                missing.append((layer+1,logical+1));continue
            j=2*(layer*dw[target]+remaining)
            if output[target][j:j+2]!=after[i:i+2]:
                output[target][j:j+2]=after[i:i+2]
                changed.append(dict(layer=layer+1,strip=logical+1,bank=target,slot=remaining+1))
    result={n:bytes(v) for n,v in output.items() if v!=destination_banks[n]}
    return result,missing,changed
