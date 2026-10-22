import json, tempfile, unittest
from datetime import date
from pathlib import Path
from daily_plan_roadmap.models import Status
from daily_plan_roadmap.review_sync import ReviewSyncError, import_review
from daily_plan_roadmap.storage import RoadmapProgressStore

class ReviewSyncTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp()); self.db=self.root/'r.db'; self.store=RoadmapProgressStore(self.db); self.addCleanup(self.store.close)
    def write(self,p):
        f=self.root/'review.json'; f.write_text(json.dumps(p,ensure_ascii=False),encoding='utf-8'); return f
    def base(self,updates): return {'review_id':'r-1','review_date':'2026-09-05','updates':updates}
    def test_valid_statuses_persist(self):
        p=self.write(self.base([{'task_id':'foundation_task_1','status':'DONE','note':'ok'},{'task_id':'modeling_task_1','status':'IN_PROGRESS'},{'task_id':'implementation_task_1','status':'BLOCKED','blocked_reason':'hardware'}])); self.assertTrue(import_review(p,self.store)['imported']); self.assertEqual(self.store.task_status('foundation_task_1'),Status.DONE)
    def test_unknown_malformed_and_atomic(self):
        for updates in ([{'task_id':'nope','status':'DONE'}],[{'task_id':'foundation_task_1','status':'BAD'}],[{'task_id':'foundation_task_1','status':'DONE'},{'task_id':'nope','status':'DONE'}]):
            with self.assertRaises(ReviewSyncError): import_review(self.write(self.base(updates)),self.store)
        self.assertEqual(self.store.task_status('foundation_task_1'),Status.NOT_STARTED)
    def test_duplicate_noop_and_conflict(self):
        p=self.write(self.base([{'task_id':'foundation_task_1','status':'IN_PROGRESS'}])); self.assertTrue(import_review(p,self.store)['imported']); self.assertFalse(import_review(p,self.store)['imported']); self.assertEqual(self.store.db.execute('SELECT count(*) FROM imported_reviews').fetchone()[0],1)
        self.write(self.base([{'task_id':'foundation_task_1','status':'DONE'}]))
        with self.assertRaises(ReviewSyncError): import_review(self.root/'review.json',self.store)
    def test_restart_ledger_and_progress(self):
        p=self.write(self.base([{'task_id':'foundation_task_1','status':'IN_PROGRESS'}])); import_review(p,self.store); self.store.close(); self.store=RoadmapProgressStore(self.db); self.addCleanup(self.store.close); self.assertEqual(self.store.task_status('foundation_task_1'),Status.IN_PROGRESS); self.assertFalse(import_review(p,self.store)['imported'])
    def test_done_cannot_regress(self):
        p=self.write(self.base([{'task_id':'foundation_task_1','status':'DONE'}])); import_review(p,self.store)
        with self.assertRaises(ReviewSyncError): import_review(self.write({'review_id':'r-2','review_date':'2026-09-06','updates':[{'task_id':'foundation_task_1','status':'IN_PROGRESS'}]}),self.store)

    def test_delayed_import_uses_review_date(self):
        p=self.write(self.base([{'task_id':'foundation_task_1','status':'IN_PROGRESS'}])); import_review(p,self.store)
        row=self.store.db.execute('SELECT last_progress_at,started_at FROM progress WHERE entity_id=?',('foundation_task_1',)).fetchone()
        self.assertEqual(row['last_progress_at'],'2026-09-05'); self.assertEqual(row['started_at'],'2026-09-05')

    def test_same_date_same_payload_different_review_id_is_noop(self):
        p=self.write(self.base([{'task_id':'foundation_task_1','status':'IN_PROGRESS'}])); import_review(p,self.store)
        p2=self.write({'review_id':'r-2','review_date':'2026-09-05','updates':[{'task_id':'foundation_task_1','status':'IN_PROGRESS'}]})
        self.assertFalse(import_review(p2,self.store)['imported']); self.assertEqual(self.store.db.execute('SELECT count(*) FROM imported_reviews').fetchone()[0],1)

    def test_different_date_same_status_is_new_event(self):
        import_review(self.write(self.base([{'task_id':'foundation_task_1','status':'IN_PROGRESS'}])),self.store)
        p=self.write({'review_id':'r-2','review_date':'2026-09-08','updates':[{'task_id':'foundation_task_1','status':'IN_PROGRESS'}]})
        self.assertTrue(import_review(p,self.store)['imported']); self.assertEqual(self.store.db.execute('SELECT last_progress_at FROM progress WHERE entity_id=?',('foundation_task_1',)).fetchone()[0],'2026-09-08')

    def test_blocked_to_in_progress_clears_reason(self):
        import_review(self.write(self.base([{'task_id':'foundation_task_1','status':'BLOCKED','blocked_reason':'x'}])),self.store)
        p=self.write({'review_id':'r-2','review_date':'2026-09-06','updates':[{'task_id':'foundation_task_1','status':'IN_PROGRESS'}]}); import_review(p,self.store)
        self.assertIsNone(self.store.db.execute('SELECT blocked_reason FROM progress WHERE entity_id=?',('foundation_task_1',)).fetchone()[0])

    def test_atomic_rollback_includes_ledger(self):
        p=self.write(self.base([{'task_id':'foundation_task_1','status':'IN_PROGRESS'},{'task_id':'nope','status':'DONE'}]))
        with self.assertRaises(ReviewSyncError): import_review(p,self.store)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM imported_reviews').fetchone()[0],0)
    def test_note_bounded_and_payload_has_no_sensitive_fields(self):
        with self.assertRaises(ReviewSyncError): import_review(self.write(self.base([{'task_id':'foundation_task_1','status':'DONE','note':'x'*501}])),self.store)
        self.assertNotIn('recipient',self.store.db.execute('SELECT value_json FROM profile_meta').fetchall().__str__())

    def test_explicit_milestone_update_unlocks_existing_eligibility(self):
        p=self.write({'review_id':'m-1','review_date':'2026-09-05','updates':[{'task_id':'modeling_task_1','status':'DONE'}],'milestone_updates':[{'milestone_id':'B1','status':'DONE'}]})
        self.assertTrue(import_review(p,self.store)['imported']); self.assertEqual(self.store.milestone_status('B1'),Status.DONE)
        self.assertIn('modeling_task_2',[x.task_id for x in self.store.next_tasks()])

    def test_milestone_transition_matrix(self):
        self.assertRaises(ReviewSyncError,import_review,self.write({'review_id':'m-bad','review_date':'2026-09-05','updates':[],'milestone_updates':[{'milestone_id':'B1','status':'BLOCKED'}]}),self.store)
        p=self.write({'review_id':'m-ok','review_date':'2026-09-05','updates':[],'milestone_updates':[{'milestone_id':'B1','status':'IN_PROGRESS'}]}); self.assertTrue(import_review(p,self.store)['imported'])
        with self.assertRaises(ReviewSyncError): import_review(self.write({'review_id':'m-reg','review_date':'2026-09-06','updates':[],'milestone_updates':[{'milestone_id':'B1','status':'NOT_STARTED'}]}),self.store)

    def test_unknown_milestone_and_atomic_rollback(self):
        p=self.write({'review_id':'m-atomic','review_date':'2026-09-05','updates':[{'task_id':'modeling_task_1','status':'DONE'}],'milestone_updates':[{'milestone_id':'unknown','status':'DONE'}]})
        with self.assertRaises(ReviewSyncError): import_review(p,self.store)
        self.assertEqual(self.store.task_status('modeling_task_1'),Status.NOT_STARTED); self.assertEqual(self.store.db.execute('SELECT count(*) FROM imported_reviews').fetchone()[0],0)

if __name__=='__main__': unittest.main()
