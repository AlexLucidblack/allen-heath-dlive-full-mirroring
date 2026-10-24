"""Only proven absolute controls may lose intermediate offline values.

Every other operation is an ordering barrier. In particular FX, relative
controls, layout changes, routing and opaque packets are never compacted.
"""
import json
import re


def state_key(command):
    if command.get('kind') != 'rack':
        return None
    name = command.get('name', '')
    if not re.fullmatch(r'Input Mixer Module, Number \d+', name):
        return None
    target = command.get('target')
    payload = bytes.fromhex(command.get('payload', ''))
    if (target == 0x1280 and len(payload) == 2 or
            target == 0x1298 and payload in (b'\x00', b'\x01')):
        return json.dumps([command['kind'], name, target])
    return None
