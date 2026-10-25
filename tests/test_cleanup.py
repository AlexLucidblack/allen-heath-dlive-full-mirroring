import sys
import pathlib
import threading
import time
import unittest

APP=pathlib.Path(__file__).resolve().parents[1]
sys.path[:0]=[str(APP),str(APP/'tests')]
import allen_heath_dlive_full_mirroring as app
sys.modules['mirror_app']=app
import test_redundancy as fixtures
from test_direct import wait_for


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RedundancyTests();self.f.setUp();self.e=self.f.e;self.r=self.f.r
        self.root=pathlib.Path(self.f.tmp.name)

    def tearDown(self):
        self.f.tearDown()

    def disk_files(self):
        return [p for p in self.root.iterdir() if p.name.startswith(('direct_','session_','recovery_'))]

    def healthy_clean(self):
        wait_for(lambda:not self.e.disk_logging and not self.disk_files() and not self.r.journal.first())

    def test_two_outages_cleanup_then_live_commands_without_disk_regrowth(self):
        self.healthy_clean()
        config=self.e.config_path.read_bytes()
        export=self.root/'user_export.jsonl';export.write_text('keep')
        for failed,master in [('A','B'),('B','A')]:
            self.r.retry_at[failed]=time.monotonic()+100
            self.f.loss(failed,'surface');wait_for(lambda:self.r.master==master)
            for n in range(6):self.e.bridges[master].operator(0x1280,n.to_bytes(2,'big'))
            self.assertTrue(self.r.journal.first())
            self.assertFalse(self.r.purge_logs())
            self.assertTrue(self.e.disk_logging)
            self.assertNotEqual(self.r.journal.storage_path,':memory:')
            self.assertTrue(pathlib.Path(self.r.path).exists())
            self.f.reconnect(failed)
            self.healthy_clean()
            self.assertEqual(self.r.master,master)
            self.assertEqual(self.r.journal.storage_path,':memory:')
            before=len(self.e.bridges[failed].sent)
            self.e.bridges[master].operator(0x1280,b'\x33\x44')
            wait_for(lambda:len(self.e.bridges[failed].sent)>before)
            self.e.event('test_after_cleanup')
            self.assertFalse(self.disk_files())
        self.assertEqual(self.e.config_path.read_bytes(),config)
        self.assertEqual(export.read_text(),'keep')

    def test_blocked_queue_survives_automatic_cleanup_but_disconnect_deletes(self):
        self.healthy_clean()
        self.r._fault('injected delivery failure')
        self.r.journal.append('A',dict(kind='rack',name='Input Mixer Module, Number 01',target=0x1280,payload='12 34'))
        self.assertFalse(self.r.purge_logs())
        self.assertTrue(pathlib.Path(self.r.path).exists())
        old=self.root/'session_old.jsonl';old.write_text('old traffic')
        sidecar=self.root/'recovery_old.sqlite3-wal';sidecar.write_text('old wal')
        self.e.disconnect()
        self.assertFalse(self.r.active)
        self.assertFalse(self.disk_files())
        self.assertEqual(self.r.journal.pending_count,0)
        self.e.event('late_callback_after_disconnect')
        self.r._tick()
        time.sleep(.1)
        self.assertFalse(self.disk_files())

    def test_previous_stopped_queue_also_prevents_automatic_deletion(self):
        from redundancy import Journal
        self.healthy_clean()
        old=Journal(self.root/'recovery_previous.sqlite3')
        old.append('A',dict(kind='opaque',raw='1234'))
        self.r.journals.append(old);self.r.cleanup_due=True
        self.assertFalse(self.r.purge_logs())
        self.assertTrue((self.root/'recovery_previous.sqlite3').exists())
        self.e.disconnect()
        self.assertFalse(self.disk_files())
        self.assertFalse(any(j.first() for j in self.r.journals))

    def test_concurrent_live_commands_and_cleanup_do_not_drop_queue(self):
        self.healthy_clean()
        total=100
        def produce():
            for n in range(total):self.e.bridges['A'].operator(0x1280,n.to_bytes(2,'big'))
        producer=threading.Thread(target=produce);producer.start()
        for _ in range(8):
            self.r.cleanup_due=True;self.r.purge_logs()
        producer.join(3)
        self.assertFalse(producer.is_alive())
        wait_for(lambda:len(self.f.frames('B'))==total)
        self.assertEqual([int.from_bytes(bytes.fromhex(f['payload']),'big') for f in self.f.frames('B')],list(range(total)))


if __name__=='__main__':
    unittest.main()
