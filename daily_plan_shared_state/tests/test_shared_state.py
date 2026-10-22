import json, tempfile, unittest
from pathlib import Path
from datetime import datetime, timezone
from daily_plan_mock.planner.service import DailyPlanService
from daily_plan_mock.planner.models import State
from daily_plan_shared_state.exporter import SharedStateExporter
from daily_plan_fusion.models import PlanDraft, PlanItem, Source
from daily_plan_recommendation.models import Energy
from daily_plan_shared_state import export_shared_state
from daily_plan_shared_state.formatter import format_summary

class SharedStateTests(unittest.TestCase):
    def setUp(self): self.root=Path(tempfile.mkdtemp())
    def plan(self):
        return {'plan_date':'2026-09-06','confirmed_at':'2026-09-05T22:00:00+08:00','revision':'rev-1','planning_window':'19:00-23:00','available_minutes':240,'energy':'NORMAL','user_fixed':[{'title':'课程'}],'user_required':[{'title':'作业','minutes':60}],'carryover':[],'user_arranged':[],'system_recommendations':[{'title':'复习','minutes':90,'source':'SYSTEM_RECOMMENDED'}],'roadmap_recommendations':[{'title':'示例任务','minutes':60,'recommendation_origin':'ROADMAP','roadmap_task_id':'implementation_task_1','roadmap_stage':'C','roadmap_milestone':'C1','deterministic_reason':'下一可执行任务'}],'total_user_minutes':60,'total_recommendation_minutes':150,'buffer_minutes':30}
    def test_confirmed_plan_export_and_schema(self):
        out=export_shared_state(self.root,confirmed_plan=self.plan(),generated_at='2026-09-06T00:00:00Z'); data=json.loads(Path(out['json']).read_text(encoding='utf-8')); self.assertEqual(data['latest_confirmed_plan']['revision'],'rev-1'); self.assertEqual(data['schema_version'],1)
    def test_draft_not_exported_as_confirmed(self):
        out=export_shared_state(self.root,confirmed_plan=None); data=json.loads(Path(out['json']).read_text()); self.assertIsNone(data['latest_confirmed_plan'])
    def test_roadmap_metadata_and_recommendation_not_done(self):
        p=self.plan(); out=export_shared_state(self.root,confirmed_plan=p); d=json.loads(Path(out['json']).read_text(encoding='utf-8')); rec=d['latest_confirmed_plan']['roadmap_recommendations'][0]; self.assertEqual(rec['recommendation_origin'],'ROADMAP'); self.assertNotEqual(rec.get('status'),'DONE')
    def test_review_and_roadmap_are_structured(self):
        out=export_shared_state(self.root,review_summary={'review_id':'r1','review_date':'2026-09-05','completed_tasks':['modeling_task_1'],'milestone_updates':[{'milestone_id':'B1','status':'DONE'}]},roadmap_summary={'active_stages':['A'],'next_eligible_tasks':['implementation_task_1']}); text=Path(out['markdown']).read_text(encoding='utf-8'); self.assertIn('modeling_task_1',text); self.assertIn('implementation_task_1',text)
    def test_summary_reflects_fixed_and_carryover_authoritative_fields(self):
        text=format_summary({'latest_confirmed_plan':{'user_fixed':[{'title':'示例课程'}],'carryover':[{'title':'示例课程'}]},'roadmap_summary':{},'generated_at':'fixed'}); self.assertIn('固定事项：示例课程',text); self.assertIn('顺延事项：示例课程',text)
    def test_forbidden_fields_fail_closed(self):
        with self.assertRaises(ValueError): export_shared_state(self.root,confirmed_plan={'token':'x'})
    def test_atomic_json_and_markdown_are_deterministic(self):
        p=self.plan(); a=export_shared_state(self.root,confirmed_plan=p,generated_at='fixed'); first=Path(a['json']).read_text(encoding='utf-8'); b=export_shared_state(self.root,confirmed_plan=p,generated_at='fixed'); self.assertEqual(first,Path(b['json']).read_text(encoding='utf-8')); self.assertTrue(Path(b['markdown']).exists())
    def test_missing_destination_fails_closed(self):
        with self.assertRaises(OSError): export_shared_state(self.root/'missing',confirmed_plan=self.plan())
    def test_only_confirmed_planner_session_can_use_hook(self):
        planner=DailyPlanService(self.root/'planner.db'); sid=planner.trigger(datetime.now(timezone.utc));
        with self.assertRaises(ValueError): SharedStateExporter(self.root).export_confirmed_planner(planner.storage)
        planner.make_draft({'must_do':[{'title':'作业','estimated_minutes':60}],'fixed_events':[],'optional':[],'available_minutes':120},datetime.now(timezone.utc))
        planner.storage.db.execute('UPDATE planning_sessions SET state=? WHERE id=?',(State.CONFIRMED.value,planner.storage.session()['id'])); planner.storage.db.commit()
        out=SharedStateExporter(self.root).export_confirmed_planner(planner.storage,available_minutes=120,energy='NORMAL',generated_at='fixed')
        self.assertTrue(Path(out['json']).exists()); planner.storage.close()

    def test_confirmed_fusion_metadata_is_exported(self):
        planner=DailyPlanService(self.root/'fusion.db'); planner.trigger(datetime.now(timezone.utc)); planner.make_draft({'must_do':[],'fixed_events':[],'optional':[],'available_minutes':240},datetime.now(timezone.utc)); planner.storage.db.execute('UPDATE planning_sessions SET state=?',(State.CONFIRMED.value,)); planner.storage.db.commit()
        draft=PlanDraft('2026-09-07','19:00-23:00',240,Energy.NORMAL,(PlanItem('示例任务',Source.SYSTEM_RECOMMENDED,60,2,None,None,None,'下一可执行任务','ROADMAP','implementation_task_1','C','C1'),),0,60,180,False)
        out=SharedStateExporter(self.root).export_confirmed_fusion(planner.storage,draft,revision='r',generated_at='fixed')
        data=json.loads(Path(out['json']).read_text(encoding='utf-8')); self.assertEqual(data['latest_confirmed_plan']['roadmap_recommendations'][0]['roadmap_task_id'],'implementation_task_1'); planner.storage.close()

if __name__=='__main__': unittest.main()
