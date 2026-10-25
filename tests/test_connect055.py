import copy
import unittest
from ahnet_lab import frame
from mirror_app import RealBridge,DEFAULT_CONFIG

class Connect055Tests(unittest.TestCase):
    def test_observed_op4_reply_remains_unidentified_and_is_reported(self):
        logs=[];b=RealBridge('A',copy.deepcopy(DEFAULT_CONFIG['A']),lambda *a,**k:None,lambda *a,**k:logs.append((a,k)))
        b._client_id=40016
        raw=frame(4,40016,0,2,b'\0\0\0\7')
        b.observe_raw_tcp(raw)
        self.assertFalse(b._identified.is_set())
        self.assertIn('op=4',b.status()['connection_error'])
        self.assertEqual(b.status()['unexpected_identity_reply']['raw'],raw.hex(' '))
        self.assertTrue(any(a[0]=='unexpected_identity_reply' and k['code']==7 for a,k in logs))
    def test_registration_batch_preserves_frame_order_and_bytes(self):
        b=object.__new__(RealBridge);writes=[]
        b.send_raw=lambda raw,transport:writes.append((raw,transport))
        parts=[frame(2,500,77,t,p) for t,p in [(23,b'first'),(24,b'\0\0\0\1'),(24,b'\0\0\0\2')]]
        b.send_f1_batch(parts)
        self.assertEqual(writes,[(b''.join(parts),'tcp')])
