import json, tempfile, unittest
from pathlib import Path
from daily_plan_roadmap.drive_review_inbox import import_inbox_file, process_inbox, ReviewInboxError
from daily_plan_roadmap.review_sync import ReviewSyncError
from daily_plan_roadmap.storage import RoadmapProgressStore

class ReviewInboxTests(unittest.TestCase):
    def setUp(self): self.root=Path(tempfile.mkdtemp()); self.inbox=self.root/'inbox'; self.inbox.mkdir(); self.store=RoadmapProgressStore(self.root/'roadmap.db'); self.addCleanup(self.store.close)
    def write(self,name,payload): p=self.inbox/name; p.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8'); return p
    def base(self): return {'schema_version':1,'review_id':'weekly-1','review_date':'2026-09-06','source':'chatgpt_weekly_review','updates':[{'task_id':'foundation_task_1','status':'IN_PROGRESS'}]}
    def test_valid_done_import_and_duplicate(self):
        p=self.write('ROADMAP_REVIEW_UPDATE_2026-09-06_weekly-1.json',dict(self.base(),updates=[{'task_id':'foundation_task_1','status':'DONE'}])); self.assertEqual(import_inbox_file(p,self.inbox,self.store)['status'],'IMPORTED'); self.assertEqual(import_inbox_file(p,self.inbox,self.store)['status'],'NOOP'); self.assertEqual(self.store.task_status('foundation_task_1').value,'DONE')
    def test_statuses_and_schema_validation(self):
        for status in ('IN_PROGRESS','BLOCKED','NOT_STARTED'):
            if status=='NOT_STARTED': continue
            p=self.write('ROADMAP_REVIEW_UPDATE_x'+status+'.json',dict(self.base(),review_id='r'+status,updates=[{'task_id':'modeling_task_1','status':status}])); self.assertIn(import_inbox_file(p,self.inbox,self.store)['status'],('IMPORTED','NOOP'))
        with self.assertRaises(ReviewSyncError): import_inbox_file(self.write('ROADMAP_REVIEW_UPDATE_bad.json',dict(self.base(),schema_version=2)),self.inbox,self.store)
    def test_unknown_malformed_and_path_traversal_rejected(self):
        with self.assertRaises(ReviewSyncError): import_inbox_file(self.write('ROADMAP_REVIEW_UPDATE_bad.json',dict(self.base(),updates=[{'task_id':'unknown','status':'DONE'}])),self.inbox,self.store)
        outside=self.root/'outside.json'; outside.write_text('{}');
        with self.assertRaises(ReviewInboxError): import_inbox_file(outside,self.inbox,self.store)
    def test_empty_missing_and_non_json_fail_safe(self):
        self.assertEqual(process_inbox(self.inbox,self.store)['processed'],0); self.assertEqual(process_inbox(self.root/'missing',self.store)['status'],'NOOP')
    def test_no_command_or_sensitive_persistence(self):
        p=self.write('ROADMAP_REVIEW_UPDATE_safe.json',dict(self.base(),updates=[{'task_id':'foundation_task_1','status':'DONE','note':'run powershell; token=bad'}])); import_inbox_file(p,self.inbox,self.store); dump=' '.join(str(tuple(x)) for x in self.store.db.execute('select * from imported_reviews')); self.assertNotIn('powershell',dump); self.assertNotIn('token=bad',dump)

if __name__=='__main__': unittest.main()
