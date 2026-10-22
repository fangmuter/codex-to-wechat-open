import tempfile
import unittest
from daily_plan_recommendation.engine import RecommendationEngine
from daily_plan_recommendation.models import Commitment, DomainProgress, Energy, ProjectSignal, RecommendationRequest, SYSTEM_RECOMMENDED
from daily_plan_recommendation.storage import RecommendationHistory

class EngineTests(unittest.TestCase):
 def req(self,available,energy=Energy.NORMAL,commitments=(),**extra): return RecommendationRequest('2026-09-05',available,energy,tuple(commitments),**extra)
 def test_no_capacity(self): self.assertEqual(RecommendationEngine().recommend(self.req(60,commitments=[Commitment('must',60,'must_do')])).recommendations,())
 def test_30_to_60_gets_one_light_max(self):
  r=RecommendationEngine().recommend(self.req(60)); self.assertLessEqual(len(r.recommendations),1); self.assertTrue(all(x.intensity=='LIGHT' for x in r.recommendations))
 def test_two_to_four_hours_gets_one_or_two_focus(self):
  r=RecommendationEngine().recommend(self.req(180,Energy.HIGH)); self.assertGreaterEqual(len([x for x in r.recommendations if x.intensity=='DEEP']),1); self.assertLessEqual(len([x for x in r.recommendations if x.intensity=='DEEP']),2)
 def test_capacity_reserves_fixed_must_and_carryover(self):
  c=[Commitment('fixed',30,'fixed_event'),Commitment('deadline',30,'deadline'),Commitment('must',30,'must_do'),Commitment('carry',30,'carryover')]; r=RecommendationEngine().recommend(self.req(180,commitments=c)); self.assertEqual(r.reserved_minutes,120); self.assertTrue(all(x.estimated_minutes<=r.remaining_minutes for x in r.recommendations))
 def test_net_available_does_not_charge_fixed_event_outside_window(self):
  r=RecommendationEngine().recommend(self.req(240,commitments=[Commitment('morning course',120,'fixed_event',False)])); self.assertEqual(r.reserved_minutes,0); self.assertEqual(r.remaining_minutes,240)
 def test_user_arranged_inside_window_reduces_capacity(self):
  r=RecommendationEngine().recommend(self.req(240,commitments=[Commitment('homework',60,'self_arranged',True)])); self.assertEqual(r.remaining_minutes,180)
 def test_same_capacity_key_is_only_reserved_once(self):
  c=[Commitment('lab report',60,'fixed_event',True,'lab-report'),Commitment('lab report',60,'must_do',True,'lab-report')]; r=RecommendationEngine().recommend(self.req(240,commitments=c)); self.assertEqual(r.reserved_minutes,60); self.assertEqual(r.remaining_minutes,180)
 def test_stale_high_priority_domain_is_boosted(self):
  r=RecommendationEngine().recommend(self.req(90,progress=(DomainProgress('Foundations',20),))); self.assertEqual(r.recommendations[0].domain,'Foundations')
 def test_recent_domain_is_not_overrecommended(self):
  r=RecommendationEngine().recommend(self.req(180,Energy.HIGH,recently_recommended_domains=('Coursework',))); self.assertNotEqual(r.recommendations[0].domain,'Coursework')
 def test_low_energy_has_no_deep_tasks(self): self.assertFalse(any(x.intensity=='DEEP' for x in RecommendationEngine().recommend(self.req(240,Energy.LOW)).recommendations))
 def test_high_energy_course_project_can_deepen(self):
  r=RecommendationEngine().recommend(self.req(180,Energy.HIGH,project_signals=(ProjectSignal('Projects',3,True),))); self.assertTrue(any(x.domain=='Projects' and x.intensity=='DEEP' for x in r.recommendations))
 def test_urgency_can_override_a_higher_long_term_priority(self):
  r=RecommendationEngine().recommend(self.req(90,Energy.HIGH,project_signals=(ProjectSignal('Projects',3,True),))); self.assertEqual(r.recommendations[0].domain,'Projects')
 def test_equal_score_uses_stable_priority_tie_break(self):
  r=RecommendationEngine().recommend(self.req(180,Energy.HIGH,project_signals=(ProjectSignal('Projects',2,True),),progress=(DomainProgress('Projects',2),))); self.assertEqual(r.recommendations[0].domain,'Coursework')
 def test_titles_are_actionable_not_domain_only(self):
  r=RecommendationEngine().recommend(self.req(240,Energy.HIGH)); self.assertTrue(all(x.title != x.domain and any(v in x.title for v in ('Review','Complete','Practice','Study','Clarify','Implement','Reproduce','Compare','Consolidate','Read')) for x in r.recommendations))
 def test_hard_capacity_limit_all_budgets_and_energy_levels(self):
  for minutes in (30,60,90,120,180,240):
   for energy in Energy:
    r=RecommendationEngine().recommend(self.req(minutes,energy)); self.assertLessEqual(sum(x.estimated_minutes for x in r.recommendations),r.remaining_minutes)
 def test_output_is_deterministic_and_explainable(self):
  q=self.req(180,Energy.HIGH,progress=(DomainProgress('Programming',12),)); a=RecommendationEngine().recommend(q); b=RecommendationEngine().recommend(q); self.assertEqual(a,b); self.assertTrue(all(x.reason and x.source==SYSTEM_RECOMMENDED for x in a.recommendations))
 def test_history_is_local_and_explicit(self):
  h=RecommendationHistory(tempfile.mktemp('.db')); self.addCleanup(h.close); r=RecommendationEngine().recommend(self.req(60)); h.record('2026-09-05',r.recommendations); self.assertEqual(h.recent_domains(),tuple(x.domain for x in r.recommendations))

if __name__=='__main__': unittest.main()
