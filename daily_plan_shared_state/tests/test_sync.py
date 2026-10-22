import hashlib, tempfile, unittest
from pathlib import Path
from daily_plan_shared_state.sync import sync_shared_state, SyncError

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp()); self.source=self.root/'source'; self.drive=self.root/'drive'; self.dest=self.drive/'ChatGPT'/'DailyPlanSharedState'; self.source.mkdir(); self.dest.mkdir(parents=True)
        (self.source/'DAILY_PLAN_SHARED_STATE.json').write_text('{"schema_version":1}\n',encoding='utf-8'); (self.source/'DAILY_PLAN_SHARED_SUMMARY.md').write_text('# Summary\n',encoding='utf-8'); (self.source/'planner.db').write_text('must not copy',encoding='utf-8')
    def test_allowlisted_files_and_hashes(self):
        r=sync_shared_state(self.source,self.dest,drive_root=self.drive); self.assertEqual(set(r['files']),{'DAILY_PLAN_SHARED_STATE.json','DAILY_PLAN_SHARED_SUMMARY.md'}); self.assertEqual(hashlib.sha256((self.source/'DAILY_PLAN_SHARED_STATE.json').read_bytes()).hexdigest(),hashlib.sha256((self.dest/'DAILY_PLAN_SHARED_STATE.json').read_bytes()).hexdigest()); self.assertFalse((self.dest/'planner.db').exists())
    def test_duplicate_sync_is_idempotent(self):
        a=sync_shared_state(self.source,self.dest,drive_root=self.drive); b=sync_shared_state(self.source,self.dest,drive_root=self.drive); self.assertEqual(a,b)
    def test_outside_drive_rejected(self):
        with self.assertRaises(SyncError): sync_shared_state(self.source,self.root/'elsewhere',drive_root=self.drive)
    def test_missing_destination_and_source_fail_closed(self):
        with self.assertRaises(SyncError): sync_shared_state(self.source,self.root/'missing',drive_root=self.drive)
        with self.assertRaises(SyncError): sync_shared_state(self.root/'missing',self.dest,drive_root=self.drive)
    def test_source_unchanged_and_non_allowlisted_not_copied(self):
        before=(self.source/'DAILY_PLAN_SHARED_STATE.json').read_bytes(); sync_shared_state(self.source,self.dest,drive_root=self.drive); self.assertEqual(before,(self.source/'DAILY_PLAN_SHARED_STATE.json').read_bytes()); self.assertFalse((self.dest/'planner.db').exists())

if __name__=='__main__': unittest.main()
