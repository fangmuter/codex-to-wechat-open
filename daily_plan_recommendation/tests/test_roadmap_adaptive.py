import json, tempfile, unittest
from pathlib import Path
from daily_plan_recommendation.engine import RecommendationEngine
from daily_plan_recommendation.models import Commitment, DomainProgress, Energy, ProjectSignal, RecommendationRequest
from daily_plan_roadmap.models import RoadmapCandidate, Status
from daily_plan_roadmap.review_sync import import_review
from daily_plan_roadmap.storage import RoadmapProgressStore

class AdaptiveTests(unittest.TestCase):
    def setUp(self): self.engine=RecommendationEngine()
    def req(self,minutes=180,energy=Energy.NORMAL,**kw): return RecommendationRequest('2026-09-05',minutes,energy,**kw)
    def candidate(self,task='t',domain='Programming',days=None,intensity='DEEP',minutes=60,canonical=None): return RoadmapCandidate(task,'完成一项示例实现并记录结果',domain,minutes,intensity,'E','E1',days,canonical_task_id=canonical)
    def test_review_sync_progress_drives_real_source_and_unlock(self):
        root=Path(tempfile.mkdtemp()); store=RoadmapProgressStore(root/'r.db'); self.addCleanup(store.close)
        p=root/'review.json'; p.write_text(json.dumps({'review_id':'x','review_date':'2026-09-05','updates':[{'task_id':'foundation_task_1','status':'DONE'}]},ensure_ascii=False),encoding='utf8'); import_review(p,store); store.update_status('milestone','A1',Status.DONE,when=__import__('datetime').date(2026,9,5))
        ids=[x.task_id for x in store.next_tasks()]; self.assertIn('foundation_task_2',ids); self.assertNotIn('foundation_task_1',ids)
        c=next(x for x in store.next_tasks() if x.task_id=='foundation_task_2'); out=self.engine.recommend(self.req(120,roadmap_candidates=(c,))); self.assertTrue(any(x.roadmap_task_id==c.task_id for x in out.recommendations)); self.assertEqual(store.task_status('foundation_task_1'),Status.DONE)
    def test_never_progress_mapping_and_stale_boost(self):
        signal=(ProjectSignal('Programming',3,True),)
        fresh=self.engine.recommend(self.req(90,project_signals=signal,roadmap_candidates=(self.candidate(days=None),))).recommendations[0]
        stale=self.engine.recommend(self.req(90,project_signals=signal,roadmap_candidates=(self.candidate(days=7),))).recommendations[0]; self.assertGreater(stale.priority,fresh.priority)
    def test_urgency_beats_stale_roadmap(self):
        r=self.engine.recommend(self.req(90,project_signals=(ProjectSignal('Coursework',3,True),),roadmap_candidates=(self.candidate(days=7),))); self.assertEqual(r.recommendations[0].domain,'Coursework')
    def test_capacity_and_energy(self):
        self.assertEqual(self.engine.recommend(self.req(0,roadmap_candidates=(self.candidate(),))).recommendations,())
        self.assertFalse(any(x.intensity=='DEEP' and x.recommendation_origin=='ROADMAP' for x in self.engine.recommend(self.req(180,Energy.LOW,roadmap_candidates=(self.candidate(),))).recommendations))
        self.assertTrue(any(x.recommendation_origin=='ROADMAP' for x in self.engine.recommend(self.req(120,Energy.HIGH,project_signals=(ProjectSignal('Programming',3,True),),roadmap_candidates=(self.candidate(),))).recommendations))
    def test_recent_domain_penalty_and_blocked_absent(self):
        r=self.engine.recommend(self.req(120,recently_recommended_domains=('Programming',),roadmap_candidates=(self.candidate(),))); self.assertNotEqual(r.recommendations[0].domain,'Programming')
    def test_static_roadmap_duplicate_scored_once(self):
        r=self.engine.recommend(self.req(180,project_signals=(ProjectSignal('Programming',3,True),),roadmap_candidates=(self.candidate(canonical='static:Programming:DEEP'),))); self.assertEqual(sum(x.domain=='Programming' for x in r.recommendations),1); self.assertEqual(next(x for x in r.recommendations if x.domain=='Programming').recommendation_origin,'ROADMAP')
    def test_same_domain_different_task_preserved(self):
        other=self.candidate(task='adc',canonical='static:Programming:ADC'); timer=self.candidate(task='timer',canonical='static:Programming:TIMER')
        r=self.engine.recommend(self.req(180,project_signals=(ProjectSignal('Programming',3,True),),roadmap_candidates=(other,timer))); self.assertGreaterEqual(sum(x.recommendation_origin=='ROADMAP' for x in r.recommendations),1)
    def test_metadata_reason_deterministic_and_no_progress_mutation(self):
        c=self.candidate(days=4); q=self.req(120,project_signals=(ProjectSignal('Programming',3,True),),roadmap_candidates=(c,)); a=self.engine.recommend(q); b=self.engine.recommend(q); self.assertEqual(a,b); self.assertEqual(a.recommendations[0].recommendation_origin,'ROADMAP'); self.assertEqual(a.recommendations[0].roadmap_task_id,'t'); self.assertIn('4 天',a.recommendations[0].reason); self.assertIn('路线阶段',a.recommendations[0].reason)

if __name__=='__main__': unittest.main()
