import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from daily_plan_fusion.engine import FusionEngine
from daily_plan_fusion.models import FusionItemInput, FusionRequest, Source
from daily_plan_fusion.planner_adapter import fusion_request_from_phase14
from daily_plan_recommendation.models import Energy, ProjectSignal
from daily_plan_mock.planner.service import DailyPlanService

def item(title,source,minutes,**kw): return FusionItemInput(title,source,minutes,**kw)
def req(available=180,energy=Energy.NORMAL,items=(),**kw): return FusionRequest('2026-09-05','19:00-23:00',available,energy,tuple(items),**kw)

class FusionTests(unittest.TestCase):
 def fuse(self,*args,**kwargs): return FusionEngine().fuse(req(*args,**kwargs))
 def test_only_user_work_can_leave_no_recommendation_space(self):
  d=self.fuse(180,items=[item('作业',Source.USER_REQUIRED,180)]); self.assertEqual(d.total_recommended_minutes,0); self.assertFalse(d.overload)
 def test_capacity_adds_recommendations_after_user_work(self):
  d=self.fuse(240,items=[item('作业',Source.USER_REQUIRED,60)]); self.assertGreater(d.total_recommended_minutes,0); self.assertLessEqual(d.total_committed_minutes+d.total_recommended_minutes,d.available_minutes)
 def test_fixed_event_outside_window_is_not_charged_twice(self):
  d=self.fuse(240,items=[item('上午示例课程',Source.USER_FIXED,120,consumes_available_capacity=False,fixed_time='09:00-11:00')]); self.assertEqual(d.total_committed_minutes,0); self.assertEqual(d.remaining_buffer_minutes,240-d.total_recommended_minutes)
 def test_user_sources_precede_recommendations(self):
  items=[item('必须作业',Source.USER_REQUIRED,30),item('顺延实验',Source.CARRYOVER,30),item('自己的复习',Source.USER_ARRANGED,30)]; d=self.fuse(240,items=items); self.assertEqual([x.source for x in d.items[:3]],[Source.USER_REQUIRED,Source.CARRYOVER,Source.USER_ARRANGED])
 def test_overload_preserves_user_items_without_recommendations(self):
  d=self.fuse(180,items=[item('必须作业',Source.USER_REQUIRED,120),item('顺延实验',Source.CARRYOVER,120)]); self.assertTrue(d.overload); self.assertEqual(d.total_recommended_minutes,0); self.assertEqual(len(d.items),2); self.assertTrue(d.warnings)
 def test_capacity_key_counts_once(self):
  d=self.fuse(240,items=[item('报告',Source.USER_FIXED,60,capacity_key='report'),item('报告',Source.USER_REQUIRED,60,capacity_key='report')]); self.assertEqual(d.total_committed_minutes,60)
 def test_same_title_with_distinct_capacity_keys_counts_twice(self):
  d=self.fuse(240,items=[item('报告',Source.USER_REQUIRED,60,capacity_key='report-a'),item('报告',Source.CARRYOVER,60,capacity_key='report-b')]); self.assertEqual(d.total_committed_minutes,120)
 def test_low_energy_no_deep_recommendation(self):
  d=self.fuse(240,Energy.LOW); self.assertTrue(all(x.estimated_minutes<=30 for x in d.items if x.source==Source.SYSTEM_RECOMMENDED))
 def test_fixed_time_conflict_is_preserved_and_marked(self):
  d=self.fuse(240,items=[item('示例课程',Source.USER_FIXED,90,fixed_time='14:00-15:30'),item('示例课程',Source.USER_FIXED,60,fixed_time='15:00-16:00')]); self.assertEqual(len(d.conflicts),1); self.assertEqual(sum(x.source==Source.USER_FIXED for x in d.items),2)
 def test_fixed_time_boundaries_and_conflict_are_canonical(self):
  edge=[item('A',Source.USER_FIXED,60,fixed_time='10:00-11:00'),item('B',Source.USER_FIXED,60,fixed_time='11:00-12:00')]; self.assertEqual(self.fuse(240,items=edge).conflicts,())
  overlap=[item('B',Source.USER_FIXED,60,fixed_time='10:30-11:30'),item('A',Source.USER_FIXED,60,fixed_time='10:00-11:00')]; self.assertEqual(self.fuse(240,items=overlap).conflicts,self.fuse(240,items=list(reversed(overlap))).conflicts)
 def test_sources_reasons_and_confirmation_gate(self):
  d=self.fuse(240); rec=[x for x in d.items if x.source==Source.SYSTEM_RECOMMENDED]; self.assertTrue(rec and all(x.reason for x in rec)); self.assertTrue(d.confirmation_required); self.assertEqual(d.state,'DRAFT_READY')
 def test_deterministic_draft(self): self.assertEqual(self.fuse(240,project_signals=(ProjectSignal('Projects',3,True),)),self.fuse(240,project_signals=(ProjectSignal('Projects',3,True),)))
 def test_input_reordering_has_canonical_output(self):
  items=[item('B',Source.USER_ARRANGED,30),item('A',Source.USER_REQUIRED,30),item('C',Source.CARRYOVER,30)]; self.assertEqual(self.fuse(240,items=items),self.fuse(240,items=list(reversed(items))))
 def test_phase14_adapter_and_confirmation_gate(self):
  s=DailyPlanService(); self.addCleanup(s.storage.close); now=datetime(2026,9,4,22,tzinfo=timezone(timedelta(hours=8))); s.trigger(now)
  s.make_draft({'must_do':[{'title':'完成作业','estimated_minutes':60}],'fixed_events':[{'title':'上午示例课程','estimated_minutes':120,'consumes_available_capacity':False,'fixed_time':'09:00-11:00'}],'optional':[]},now)
  request=fusion_request_from_phase14(s.storage.session(),planning_window='19:00-23:00',available_minutes=240,energy=Energy.NORMAL)
  d=FusionEngine().fuse(request); self.assertEqual(s.storage.session()['state'],'DRAFT_READY'); self.assertTrue(d.confirmation_required); self.assertEqual(d.state,'DRAFT_READY'); self.assertEqual(d.total_committed_minutes,60); self.assertTrue(any(x.title=='上午示例课程' for x in d.items)); self.assertLessEqual(d.total_recommended_minutes,180)
 def test_fusion_reuses_phase17_engine(self):
  original=__import__('daily_plan_recommendation.engine',fromlist=['RecommendationEngine']).RecommendationEngine.recommend
  with patch('daily_plan_fusion.engine.RecommendationEngine.recommend',autospec=True,side_effect=original) as call:
   self.fuse(240); self.assertTrue(call.called)
 def test_hard_capacity_never_exceeded(self):
  for minutes in (30,60,90,120,180,240):
   d=self.fuse(minutes); self.assertLessEqual(d.total_committed_minutes+d.total_recommended_minutes,d.available_minutes)

if __name__=='__main__': unittest.main()
