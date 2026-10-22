import json, tempfile, unittest
from datetime import date
from pathlib import Path
from daily_plan_fusion.engine import FusionEngine
from daily_plan_fusion.models import FusionItemInput, FusionRequest, Source
from daily_plan_orchestration.formatter import format_draft
from daily_plan_recommendation.models import Energy, ProjectSignal
from daily_plan_roadmap.review_sync import import_review
from daily_plan_roadmap.storage import RoadmapProgressStore

class RoadmapFusionIntegrationTests(unittest.TestCase):
    def test_review_to_roadmap_to_fusion_formatter(self):
        root=Path(tempfile.mkdtemp()); store=RoadmapProgressStore(root/'roadmap.db'); self.addCleanup(store.close)
        before=store.next_tasks(date(2026,9,5)); p=root/'review.json'; p.write_text(json.dumps({'review_id':'r','review_date':'2026-09-05','updates':[{'task_id':'foundation_task_1','status':'DONE'}]}),encoding='utf8'); import_review(p,store); store.update_status('milestone','A1','DONE',when=date(2026,9,5)); after=store.next_tasks(date(2026,9,5)); self.assertNotIn('foundation_task_1',[x.task_id for x in after]); self.assertIn('foundation_task_2',[x.task_id for x in after])
        request=FusionRequest('2026-09-06','evening',180,Energy.NORMAL,(FusionItemInput('完成作业',Source.USER_REQUIRED,60,kind='must_do'),),project_signals=(ProjectSignal('Foundations',3,True),),roadmap_candidates=(next(x for x in after if x.task_id=='foundation_task_2'),))
        draft=FusionEngine().fuse(request); text=format_draft(draft).text; self.assertTrue(any(x.roadmap_task_id=='foundation_task_2' for x in draft.items)); self.assertIn('【路线推荐】',text); self.assertIn('原因：',text); self.assertIn('状态：待确认',text); self.assertIn('确认计划',text); self.assertEqual(draft.state,'DRAFT_READY'); self.assertTrue(draft.confirmation_required); self.assertLessEqual(draft.total_recommended_minutes,120)
    def test_overload_and_low_energy(self):
        c=FusionItemInput('必须完成的作业',Source.USER_REQUIRED,200,kind='must_do'); roadmap=RoadmapProgressStore(':memory:'); self.addCleanup(roadmap.close); candidate=roadmap.next_tasks()[0]
        overloaded=FusionEngine().fuse(FusionRequest('2026-09-06','evening',180,Energy.NORMAL,(c,),roadmap_candidates=(candidate,))); self.assertTrue(overloaded.overload); self.assertFalse([x for x in overloaded.items if x.source==Source.SYSTEM_RECOMMENDED]); self.assertIn('未加入系统推荐',format_draft(overloaded).text)
        low=FusionEngine().fuse(FusionRequest('2026-09-06','evening',180,Energy.LOW,roadmap_candidates=(candidate,))); self.assertFalse(any(x.source==Source.SYSTEM_RECOMMENDED and x.estimated_minutes>30 for x in low.items))
    def test_fixed_external_window_and_static_compatibility(self):
        d=FusionEngine().fuse(FusionRequest('2026-09-06','evening',180,Energy.NORMAL,(FusionItemInput('上午课程',Source.USER_FIXED,120,consumes_available_capacity=False,fixed_time='09:00-11:00'),)))
        self.assertEqual(d.total_committed_minutes,0); self.assertTrue(d.items)

if __name__=='__main__': unittest.main()
