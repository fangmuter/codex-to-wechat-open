import unittest
from daily_plan_fusion.models import PlanDraft, PlanItem, Source
from daily_plan_orchestration.formatter import format_draft
from daily_plan_recommendation.models import Energy

class RoadmapFormatterTests(unittest.TestCase):
    def draft(self,items):
        return PlanDraft('2026-09-06','evening',180,Energy.NORMAL,tuple(items),60,120,0,False,(),(),(),True,'DRAFT_READY')
    def test_static_and_roadmap_labels_and_reason(self):
        static=PlanItem('静态任务',Source.SYSTEM_RECOMMENDED,30,10,reason='静态理由',recommendation_origin='STATIC')
        roadmap=PlanItem('路线任务',Source.SYSTEM_RECOMMENDED,60,20,reason='路线阶段 E / milestone E1 的下一可执行任务',recommendation_origin='ROADMAP',roadmap_task_id='implementation_task_1',roadmap_stage='E',roadmap_milestone='E1')
        text=format_draft(self.draft((static,roadmap))).text
        self.assertIn('【系统推荐的】',text); self.assertIn('【路线推荐】',text); self.assertIn('路线阶段 E',text)
    def test_labels_do_not_change_capacity_or_output(self):
        a=PlanItem('路线任务',Source.SYSTEM_RECOMMENDED,60,20,reason='r',recommendation_origin='ROADMAP')
        b=PlanItem('路线任务',Source.SYSTEM_RECOMMENDED,60,20,reason='r',recommendation_origin='ROADMAP')
        self.assertEqual(format_draft(self.draft((a,))).text,format_draft(self.draft((b,))).text); self.assertIn('系统推荐：120min',format_draft(self.draft((a,))).text)

    def test_recommendation_labels_are_separate_sections(self):
        static = PlanItem('静态任务', Source.SYSTEM_RECOMMENDED, 30, 10,
                          reason='静态理由', recommendation_origin='STATIC')
        roadmap = PlanItem('路线任务', Source.SYSTEM_RECOMMENDED, 60, 20,
                           reason='路线理由', recommendation_origin='ROADMAP')
        text = format_draft(self.draft((static, roadmap))).text
        self.assertIn('【系统推荐的】\n- 静态任务 30min\n  原因：静态理由', text)
        self.assertIn('【路线推荐】\n- 路线任务 60min\n  原因：路线理由', text)
        self.assertNotIn('静态理由【路线推荐】', text)

if __name__=='__main__': unittest.main()
