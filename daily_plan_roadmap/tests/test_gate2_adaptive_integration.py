import json, tempfile, unittest
from datetime import date
from pathlib import Path
from daily_plan_roadmap.storage import RoadmapProgressStore
from daily_plan_roadmap.task_source import RoadmapTaskSource
from daily_plan_roadmap.drive_review_inbox import import_inbox_file
from daily_plan_recommendation.engine import RecommendationEngine
from daily_plan_recommendation.models import RecommendationRequest, Energy, ProjectSignal
from daily_plan_fusion.engine import FusionEngine
from daily_plan_fusion.models import FusionRequest, FusionItemInput, Source
from daily_plan_orchestration.formatter import format_draft

class RoadmapReviewIntegrationTests(unittest.TestCase):
 def setUp(self):
  self.root=Path(tempfile.mkdtemp()); self.inbox=self.root/'inbox'; self.inbox.mkdir(); self.store=RoadmapProgressStore(self.root/'roadmap.db'); self.source=RoadmapTaskSource(self.store)
  self.day=date(2030,1,6); self.base=tuple(self.source.candidates(self.day,limit=4)); self.assertTrue(any(x.task_id=='foundation_task_1' for x in self.base))
 def tearDown(self): self.store.close()
 def request(self,candidates,signals=()): return RecommendationRequest('2026-09-07',240,Energy.NORMAL,project_signals=tuple(signals),roadmap_candidates=tuple(candidates))
 def test_review_changes_source_recommendation_and_fusion(self):
  baseline=RecommendationEngine().recommend(self.request(self.base)).recommendations; self.assertTrue(any(x.roadmap_task_id=='foundation_task_1' for x in baseline))
  p=self.inbox/'ROADMAP_REVIEW_UPDATE_sample-001.json'; p.write_text(json.dumps({'schema_version':1,'review_id':'sample-review-001','review_date':'2030-01-06','source':'chatgpt_weekly_review','updates':[{'task_id':'foundation_task_1','status':'DONE','note':'explicit review'}]},ensure_ascii=False),encoding='utf-8')
  self.assertEqual(import_inbox_file(p,self.inbox,self.store)['status'],'IMPORTED'); self.assertEqual(self.store.task_status('foundation_task_1').value,'DONE')
  self.store.close(); self.store=RoadmapProgressStore(self.root/'roadmap.db'); self.source=RoadmapTaskSource(self.store); after_candidates=tuple(self.source.candidates(self.day,limit=4)); after_req=self.request(after_candidates,(ProjectSignal('Projects',3,True),)); after_req=RecommendationRequest(after_req.target_date,after_req.available_minutes,after_req.energy,after_req.commitments,after_req.project_signals,after_req.progress,('Coursework','Foundations'),after_req.roadmap_candidates); after=RecommendationEngine().recommend(after_req).recommendations
  self.assertFalse(any(x.roadmap_task_id=='foundation_task_1' for x in after)); self.assertTrue(any(x.recommendation_origin=='ROADMAP' for x in after)); self.assertNotEqual([(x.title,x.roadmap_task_id) for x in baseline],[(x.title,x.roadmap_task_id) for x in after])
  req=FusionRequest('2030-01-07','19:00-23:00',180,Energy.NORMAL,(FusionItemInput('示例作业',Source.USER_REQUIRED,60,capacity_key='sample-homework'),),recently_recommended_domains=('Coursework','Foundations'),roadmap_candidates=after_candidates); draft=FusionEngine().fuse(req); text=format_draft(draft).text
  self.assertEqual(draft.state,'DRAFT_READY'); self.assertLessEqual(draft.total_recommended_minutes,120); self.assertNotIn('foundation_task_1',text); self.assertIn('【路线推荐】',text)
 def test_duplicate_review_is_noop_and_recommendation_does_not_mutate_progress(self):
  p=self.inbox/'ROADMAP_REVIEW_UPDATE_sample-duplicate.json'; payload={'schema_version':1,'review_id':'sample-review-duplicate','review_date':'2030-01-06','source':'chatgpt_weekly_review','updates':[{'task_id':'foundation_task_1','status':'DONE'}]}; p.write_text(json.dumps(payload),encoding='utf-8'); import_inbox_file(p,self.inbox,self.store); first=self.store.db.execute("select last_progress_at from progress where entity_id='foundation_task_1'").fetchone()[0]; self.assertEqual(import_inbox_file(p,self.inbox,self.store)['status'],'NOOP'); self.assertEqual(first,self.store.db.execute("select last_progress_at from progress where entity_id='foundation_task_1'").fetchone()[0]); before=self.store.task_status('foundation_task_1'); RecommendationEngine().recommend(self.request(tuple(self.source.candidates(self.day)))); self.assertEqual(before,self.store.task_status('foundation_task_1'))
 def test_confirmation_semantics_do_not_mark_recommendation_done(self):
  candidate=next(x for x in self.base if x.task_id=='foundation_task_1'); self.assertEqual(self.store.task_status(candidate.task_id).value,'NOT_STARTED'); RecommendationEngine().recommend(self.request(self.base)); self.assertEqual(self.store.task_status(candidate.task_id).value,'NOT_STARTED')

if __name__=='__main__': unittest.main()
