import pathlib
import tempfile
import time
import unittest

from captured_rx import CapturedSubscriptions,DATA
from redundancy import Journal
from recovery_queue import state_key
import test_redundancy as fixtures
from test_direct import wait_for
NAMES=fixtures.NAMES


def command(value,target=0x1280,name=NAMES[0]):
    return dict(kind='rack',name=name,target=target,payload=value.to_bytes(2,'big').hex())


class QueueTests(unittest.TestCase):
    def test_absolute_values_compact_and_barriers_preserve_order(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(pathlib.Path(d)/'queue.sqlite3')
            for v in range(30):j.append('A',command(v),compact=True)
            self.assertEqual([r[2] for r in j.pending()],[command(29)])
            barrier=dict(kind='rack',name='FX Manager 04',target=0x1002,payload='01f4')
            j.append('A',barrier,compact=True)
            for v in range(30,60):j.append('A',command(v),compact=True)
            rows=j.pending()
            self.assertEqual([r[2] for r in rows],[command(29),barrier,command(59)])
            self.assertEqual(j.pending_count,3)
            self.assertEqual(j.compacted,58)
            j.set_many([rows[0][0],rows[1][0]],'sent')
            self.assertEqual(j.pending_count,1)
            self.assertEqual(j.pending_bytes,len(__import__('json').dumps(command(59))))
            j.close()

    def test_no_compaction_across_handover_or_claimed_sending_row(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(pathlib.Path(d)/'queue.sqlite3')
            a=j.append('A',command(1),True);j.set(a,'sending')
            b=j.append('A',command(2),True)
            j.mark('master_changed',master='B')
            c=j.append('A',command(3),True)
            self.assertEqual([r[0] for r in j.pending()],[a,b,c]);j.close()

    def test_unproven_controls_are_never_compacted(self):
        for value in (dict(kind='opaque',raw='1234'),dict(kind='layout',changes=[[0,1,'0002']]),
                      command(500,0x1002,'FX Manager 04'),command(50,0x1002,NAMES[1]),command(1,0x1007)):
            self.assertIsNone(state_key(value))

    def test_priority_discovery_uses_names_without_cached_object_ids(self):
        class Bridge:recovery_priority_names=['FX Manager 05','Input Mixer Module, Number 01']
        subscription=CapturedSubscriptions(Bridge(),'capture_all')
        self.assertEqual([s['name'] for s in subscription.specs[:2]],Bridge.recovery_priority_names)
        self.assertEqual(len(subscription.specs),len(DATA['objects']))
        self.assertFalse(subscription.by_name)


class FastRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.RedundancyTests();self.fixture.setUp()
        self.e=self.fixture.e;self.r=self.fixture.r
    def tearDown(self):self.fixture.tearDown()

    def test_surface_reconnect_preserves_healthy_mixrack_session(self):
        before=self.e.bridges['A'];surface=self.e.surfaces.sessions['A']
        self.fixture.loss('A','surface');wait_for(lambda:self.r.master=='B')
        self.fixture.reconnect('A')
        self.assertIs(self.e.bridges['A'],before)
        self.assertIsNot(self.e.surfaces.sessions['A'],surface)

    def test_mixrack_reconnect_preserves_healthy_surface(self):
        before=self.e.surfaces.sessions['A'];rack=self.e.bridges['A']
        self.fixture.loss('A','rack');wait_for(lambda:self.r.master=='B')
        self.fixture.reconnect('A')
        self.assertIs(self.e.surfaces.sessions['A'],before)
        self.assertIsNot(self.e.bridges['A'],rack)

    def test_batch_fifo_uses_fresh_ids_with_no_dropped_intermediate_values(self):
        self.fixture.loss('B','rack');wait_for(lambda:not self.r.health['B'])
        for n in range(160):self.e.bridges['A'].operator(0x1280,n.to_bytes(2,'big'))
        self.fixture.reconnect('B')
        wait_for(lambda:self.r.replayed_commands==160)
        self.assertEqual([bytes.fromhex(f['payload']) for f in self.fixture.frames('B')],[n.to_bytes(2,'big') for n in range(160)])
        self.assertLessEqual(self.r.replay_batches,6)
        self.assertTrue(all(f['a']==self.e.bridges['B'].ids[NAMES[0]] for f in self.fixture.frames('B')))

    def test_offline_compaction_keeps_final_value_and_fx_barrier(self):
        self.e.cfg['recovery_compact']=True
        self.fixture.loss('B','surface');wait_for(lambda:not self.r.health['B'])
        for n in range(100):self.e.bridges['A'].operator(0x1280,n.to_bytes(2,'big'))
        self.e.bridges['A'].operator(0x1002,b'\x01\xf4','FX Manager 04')
        for n in range(100,200):self.e.bridges['A'].operator(0x1280,n.to_bytes(2,'big'))
        self.assertEqual(self.r.journal.pending_count,3)
        self.fixture.reconnect('B');wait_for(lambda:self.r.replayed_commands==3)
        self.assertEqual([f['payload'] for f in self.fixture.frames('B')],['00 63','01 f4','00 c7'])
        self.assertEqual(self.r.journal.compacted,198)

    def test_writes_begin_before_full_catalogue_but_master_stays_ineligible(self):
        dest=self.e.bridges['B'];dest.probe_status=lambda:dict(phase='discovering')
        self.assertFalse(self.r._ready('B'));self.assertTrue(self.r._destination_ready('B'))
        self.e.bridges['A'].operator(0x1280,b'\x12\x34')
        wait_for(lambda:self.r.replayed_commands==1)
        self.assertEqual(self.r.master,'A')

    def test_unresolved_head_waits_without_skipping_following_operations(self):
        dest=self.e.bridges['B'];dest.probe_status=lambda:dict(phase='discovering')
        obj=dest.ids.pop(NAMES[0])
        self.e.bridges['A'].operator(0x1280,b'\x11\x22')
        self.e.bridges['A'].operator(0x1002,b'\x01\xf4','FX Manager 04')
        time.sleep(.12);self.assertFalse(dest.sent);self.assertIsNone(self.r.error)
        dest.ids[NAMES[0]]=obj
        wait_for(lambda:self.r.replayed_commands==2)
        self.assertEqual([f['payload'] for f in self.fixture.frames('B')],['11 22','01 f4'])

    def test_scene_and_show_events_stay_local_even_experimental(self):
        self.e.cfg.update(scene_sync=True,surface_experimental=True)
        source=self.e.surfaces.sessions['A']
        for name in ('Surface Scene Manager','Scene Manager Intermediate','Library File Manager'):
            self.e.surfaces.property('A',name,dict(target=0x100c,payload='00 10'),source.generation,source=source)
        self.assertIsNone(self.r.journal.first())

    def test_resolved_id_waits_until_object_subscription_was_sent(self):
        dest=self.e.bridges['B'];dest.probe_status=lambda:dict(phase='registering')
        dest.captured.writable_id_for=lambda name:None
        self.e.bridges['A'].operator(0x1280,b'\x11\x22')
        time.sleep(.12);self.assertFalse(dest.sent)
        dest.captured.writable_id_for=dest.ids.get
        wait_for(lambda:self.r.replayed_commands==1)

    def test_partial_batch_with_unproven_operation_blocks_instead_of_repeating(self):
        dest=self.e.bridges['B']
        def partial(raw,transport):
            dest.sent.append((raw,transport));dest.connected=False
            raise OSError('partial write containing FX operation')
        dest.send_raw=partial
        self.e.bridges['A'].operator(0x1002,b'\x01\xf4','FX Manager 04')
        wait_for(lambda:bool(self.r.error))
        self.assertEqual(self.r.journal.stats().get('uncertain'),1)
        self.assertEqual(self.r.replayed_commands,0)
