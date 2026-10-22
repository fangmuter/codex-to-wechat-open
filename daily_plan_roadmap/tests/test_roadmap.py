import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from daily_plan_roadmap.candidate_adapter import dedupe_candidates
from daily_plan_roadmap.models import Status
from daily_plan_roadmap.storage import RoadmapProgressStore
from daily_plan_roadmap.task_source import RoadmapTaskSource


class RoadmapTests(unittest.TestCase):
    def setUp(self):
        self.path=Path(tempfile.mkdtemp())/'roadmap.db'; self.store=RoadmapProgressStore(self.path); self.addCleanup(self.store.close); self.source=RoadmapTaskSource(self.store)
    def test_prerequisite_lock(self):
        self.assertEqual([x.task_id for x in self.source.candidates(date(2026,9,5))],['foundation_task_1','modeling_task_1','implementation_task_1'])
    def test_unlock_after_done(self):
        self.store.update_status('task','foundation_task_1',Status.DONE,when=date(2026,9,5)); self.store.update_status('milestone','A1',Status.DONE,when=date(2026,9,5)); self.assertEqual([x.task_id for x in self.source.candidates(date(2026,9,5))],['foundation_task_2','modeling_task_1','implementation_task_1'])
    def test_current_milestone_preference(self):
        self.store.update_status('milestone','A1',Status.IN_PROGRESS); self.assertEqual(self.source.candidates()[0].roadmap_milestone,'A1')
    def test_next_milestone_after_completion(self):
        self.store.update_status('task','foundation_task_1',Status.DONE,when=date(2026,9,5)); self.store.update_status('milestone','A1',Status.DONE,when=date(2026,9,5)); self.assertEqual(self.source.candidates()[0].roadmap_milestone,'A2')
    def test_persistence_after_restart(self):
        self.store.update_status('task','foundation_task_1',Status.IN_PROGRESS,when=date(2026,9,1),short_note='progress'); self.store.close(); self.store=RoadmapProgressStore(self.path); self.addCleanup(self.store.close); self.assertEqual(self.store.task_status('foundation_task_1'),Status.IN_PROGRESS); self.assertEqual(self.store.next_tasks(date(2026,9,5))[0].days_since_progress,4)
    def test_deterministic_next_task(self):
        first=self.source.candidates(date(2026,9,5)); second=self.source.candidates(date(2026,9,5)); self.assertEqual(first,second)
    def test_recommended_is_not_done(self):
        self.assertNotEqual(self.source.candidates()[0].task_id,''); self.assertNotEqual(self.store.task_status('foundation_task_1'),Status.DONE)
        self.store.update_status('task','foundation_task_1',Status.DONE,when=date(2026,9,5)); self.assertNotIn('foundation_task_1',[x.task_id for x in self.source.candidates()])
    def test_static_roadmap_dedupe_by_stable_task_identity(self):
        candidates=self.source.candidates(); remaining=dedupe_candidates(candidates,[candidates[0].task_id]); self.assertNotIn(candidates[0].task_id,[x.task_id for x in remaining])
        self.assertEqual(len(dedupe_candidates(candidates,[])),len(candidates)); self.assertEqual(candidates[0].candidate_key,'roadmap:foundation_task_1')

    def test_parallel_lanes_and_hard_control_lock(self):
        ids=[x.task_id for x in self.source.candidates(date(2026,9,5))]
        self.assertIn('modeling_task_1',ids); self.assertIn('implementation_task_1',ids)
        self.assertNotIn('systems_task_1',ids); self.assertNotIn('quality_task_1',ids)

    def test_never_progress_is_distinct_from_today(self):
        self.assertIsNone(next(x for x in self.source.candidates() if x.task_id=='modeling_task_1').days_since_progress)
        self.store.update_status('task','foundation_task_1',Status.IN_PROGRESS,when=date(2026,9,5))
        self.assertEqual(next(x for x in self.source.candidates(date(2026,9,5)) if x.task_id=='foundation_task_1').days_since_progress,0)

    def test_candidate_limit_is_deterministic(self):
        result=self.source.candidates(date(2026,9,5),limit=99)
        self.assertLessEqual(len(result),4)
        self.assertEqual(result,self.source.candidates(date(2026,9,5),limit=99))

if __name__=='__main__': unittest.main()
