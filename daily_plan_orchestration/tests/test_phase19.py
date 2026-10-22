import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daily_plan_mock.planner.models import State
from daily_plan_mock.planner.service import DailyPlanService, MockTransport
from daily_plan_fusion.engine import FusionEngine
from daily_plan_fusion.models import FusionItemInput, FusionRequest, Source
from daily_plan_recommendation.models import Energy
from daily_plan_orchestration.formatter import format_draft
from daily_plan_orchestration.service import OrchestrationAdapter, Phase19Orchestrator
from daily_plan_orchestration.session_meta import SessionMetaStore
from daily_plan_proactive.scheduler import FakeProactiveTransport, ProactiveScheduler
from daily_plan_proactive.wechat_bridge import ProactiveInboundBridge
from daily_plan_wechat_integration.session_bridge import SessionBridge
from ilink_receive_probe.probe.models import Batch, InboundEvent
from ilink_receive_probe.probe.receiver import Receiver
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.reply_transport import FakeReplyOnlyTransport
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.transport import FakeIlinkTransport

TZ=timezone(timedelta(hours=8))
def at(hour,minute=0): return datetime(2026,9,5,hour,minute,tzinfo=TZ)
def encrypt(data): return b'T:'+data[::-1]
def decrypt(data): return data[2:][::-1]

class Phase19Tests(unittest.TestCase):
 def setUp(self):
  self.root=Path(tempfile.mkdtemp()); self.n=0; self.probe=ProbeStorage(self.root/'probe.db'); self.addCleanup(self.probe.close); self.context=ReplyContextStore(self.root,300,lambda:0,encrypt,decrypt); self.planner=DailyPlanService(self.root/'planner.db',MockTransport()); self.addCleanup(self.planner.storage.close); self.reply=FakeReplyOnlyTransport(); self.bridge=SessionBridge(self.planner,self.probe,self.context,self.reply,self.root/'bridge.db'); self.addCleanup(self.bridge.close); self.meta=SessionMetaStore(self.root/'meta.db'); self.addCleanup(self.meta.close); self.scheduler=ProactiveScheduler(self.root/'scheduler.db',FakeProactiveTransport()); self.addCleanup(self.scheduler.close); self.orchestrator=Phase19Orchestrator(self.meta,'19:00-23:00'); self.adapter=OrchestrationAdapter(self.bridge,self.orchestrator,ProactiveInboundBridge(self.scheduler,'me','bot'))
 def deliver(self,text,mid=None,sender='me',bot='bot',conversation='one_to_one'):
  self.n+=1; event=InboundEvent('acct',mid or f'm{self.n}',sender,'now',text,bot,conversation,f'ctx-{self.n}',f'user-{self.n}'); Receiver(FakeIlinkTransport([Batch((event,),str(self.n))]),self.probe,'me','bot',context_store=self.context).receive_once(); return self.adapter.accept_inbound(event,at(22,10)),event
 def to_draft(self):
  self.deliver('开始明日计划'); self.deliver('完成：x\n顺延：复盘'); self.deliver('必须：完成作业'); self.deliver('固定：09:00 示例课程'); return self.deliver('可用时间：4小时\n精力：中')
 def test_valid_reply_suppresses_reminder_then_starts_planner(self):
  self.scheduler.tick(at(22)); status,_=self.deliver('开始明日计划'); self.assertEqual(status,'ACKED_API'); self.assertEqual(self.scheduler.tick(at(22,30)).status,'user_replied')
 def test_rejected_inbound_does_not_enter_planner(self):
  status,_=self.deliver('开始明日计划',sender='other'); self.assertEqual(status,'REJECTED'); self.assertIsNone(self.planner.storage.session())
 def test_end_to_end_draft_fusion_formatter_and_gate(self):
  status,_=self.to_draft(); self.assertEqual(status,'ACKED_API'); session=self.planner.storage.session(); self.assertEqual(session['state'],State.DRAFT_READY.value); text=self.reply.calls[-1][1]; self.assertIn('明日计划草稿',text); self.assertIn('【系统推荐的】',text); self.assertIn('确认计划',text); self.assertEqual(self.orchestrator.last_draft.total_committed_minutes,60); self.assertTrue(self.orchestrator.last_draft.confirmation_required)
 def test_exact_confirm_uses_phase14_and_non_exact_does_not(self):
  self.to_draft(); self.deliver('确认'); self.assertEqual(self.planner.storage.session()['state'],State.DRAFT_READY.value); self.deliver('确认计划'); self.assertEqual(self.planner.storage.session()['state'],State.CONFIRMED.value)
 def test_exact_confirm_is_fail_closed_without_current_fusion_revision(self):
  self.to_draft(); sid=self.planner.storage.session()['id']; self.meta.db.execute('DELETE FROM session_meta WHERE planner_session_id=?',(sid,)); self.meta.db.commit()
  self.deliver('确认计划')
  self.assertEqual(self.planner.storage.session()['state'],State.DRAFT_READY.value)
 def test_modification_invalidates_and_regenerates_revision(self):
  self.to_draft(); sid=self.planner.storage.session()['id']; old=self.meta.get(sid)['revision']; self.deliver('修改必须：新任务'); new=self.meta.get(sid)['revision']; self.assertNotEqual(old,new); self.assertIn('新任务',self.reply.calls[-1][1]); self.assertTrue(self.orchestrator.can_confirm(self.planner.storage.session(),self.bridge.store.session()))
 def test_structured_capacity_and_energy_persist_and_rebuild_fusion_request(self):
  self.to_draft(); session=self.planner.storage.session(); record=self.meta.get(session['id'])
  self.assertEqual((record['planning_window'],record['available_minutes'],record['energy']),('19:00-23:00',240,'中'))
  request=self.orchestrator.fusion_request_from_metadata(session)
  self.assertEqual((request.planning_window,request.available_minutes,request.energy.value),('19:00-23:00',240,'NORMAL'))
 def test_modify_available_time_invalidates_revision_and_exact_confirm_uses_refusion(self):
  self.to_draft(); sid=self.planner.storage.session()['id']; old=self.meta.get(sid)['revision']
  self.deliver('修改可用时间：2小时\n精力：高'); record=self.meta.get(sid); request=self.orchestrator.fusion_request_from_metadata(self.planner.storage.session())
  self.assertNotEqual(old,record['revision']); self.assertEqual((record['available_minutes'],record['energy']),(120,'高')); self.assertEqual((request.available_minutes,request.energy.value),(120,'HIGH'))
  self.deliver('确认计划'); self.assertEqual(self.planner.storage.session()['state'],State.CONFIRMED.value)
 def test_overload_has_no_system_recommendation_and_visible_warning(self):
  self.to_draft(); self.deliver('修改可用时间：30分钟\n精力：低'); draft=self.orchestrator.last_draft
  self.assertTrue(draft.overload); self.assertFalse([x for x in draft.items if x.source==Source.SYSTEM_RECOMMENDED]); self.assertIn('未加入系统推荐',self.reply.calls[-1][1])
 def test_conflict_and_formatter_are_deterministic(self):
  request=FusionRequest('2026-09-06','19:00-23:00',240,Energy.NORMAL,(FusionItemInput('课A',Source.USER_FIXED,0,consumes_available_capacity=False,fixed_time='10:00-11:00'),FusionItemInput('课B',Source.USER_FIXED,0,consumes_available_capacity=False,fixed_time='10:30-11:30')))
  first=format_draft(FusionEngine().fuse(request)).text; second=format_draft(FusionEngine().fuse(request)).text
  self.assertEqual(first,second); self.assertIn('固定时间冲突：课A 与 课B',first); self.assertIn('状态：待确认',first)
 def test_structured_fixed_time_conflict_reaches_formatter(self):
  self.deliver('开始明日计划'); self.deliver('完成：x'); self.deliver('必须：完成作业'); self.deliver('固定：10:00-11:00 课A；10:30-11:30 课B'); self.deliver('可用时间：4小时\n精力：中')
  self.assertIn('固定时间冲突：课A 与 课B',self.reply.calls[-1][1])
 def test_restart_recovers_meta_without_auto_confirm_or_duplicate_reply(self):
  _,event=self.to_draft(); sid=self.planner.storage.session()['id']; calls=len(self.reply.calls); self.bridge.close(); self.meta.close(); self.planner.storage.close(); self.planner=DailyPlanService(self.root/'planner.db',MockTransport()); self.addCleanup(self.planner.storage.close); self.meta=SessionMetaStore(self.root/'meta.db'); self.addCleanup(self.meta.close); self.bridge=SessionBridge(self.planner,self.probe,self.context,self.reply,self.root/'bridge.db'); self.addCleanup(self.bridge.close); self.orchestrator=Phase19Orchestrator(self.meta,'19:00-23:00'); self.adapter=OrchestrationAdapter(self.bridge,self.orchestrator); session=self.planner.storage.session(); request=self.orchestrator.fusion_request_from_metadata(session)
  self.assertEqual(session['state'],State.DRAFT_READY.value); self.assertIsNotNone(self.meta.get(sid)); self.assertEqual(request.available_minutes,240); self.assertEqual(len(self.reply.calls),calls); self.assertEqual(self.adapter.accept_inbound(event,at(22,10)),'DUPLICATE_IGNORED'); self.assertEqual(len(self.reply.calls),calls)
  self.deliver('确认计划'); self.assertEqual(self.planner.storage.session()['state'],State.CONFIRMED.value)
 def test_duplicate_inbound_does_not_duplicate_reply(self):
  status,event=self.deliver('开始明日计划',mid='same'); calls=len(self.reply.calls); state=self.planner.storage.session()['state']; self.assertEqual(self.adapter.accept_inbound(event,at(22,10)),'DUPLICATE_IGNORED'); self.assertEqual(len(self.reply.calls),calls); self.assertEqual(self.planner.storage.session()['state'],state)
 def test_rejected_inbound_does_not_suppress_reminder(self):
  self.scheduler.tick(at(22)); self.deliver('开始明日计划',sender='other'); self.assertEqual(self.scheduler.tick(at(22,30)).status,'reminder_sent')
 def test_orchestration_storage_has_no_context_or_recipient(self):
  self.to_draft(); dump=' '.join(str(tuple(x)) for x in self.meta.db.execute('SELECT * FROM session_meta')); self.assertNotIn('ctx-',dump); self.assertNotIn('user-',dump); self.assertNotIn('context_token',dump)
 def test_window_external_fixed_event_does_not_reduce_net_capacity(self):
  self.to_draft(); draft=self.orchestrator.last_draft
  self.assertEqual(draft.available_minutes,240); self.assertEqual(draft.total_committed_minutes,60)
  self.assertLessEqual(draft.total_committed_minutes+draft.total_recommended_minutes,draft.available_minutes)

 def test_friendly_input_uses_existing_orchestration_flow(self):
  self.deliver('开始明日计划')
  status,_=self.deliver('明天必须完成示例作业，晚上有4小时，精神不错。')
  self.assertEqual(status,'ACKED_API')
  self.assertEqual(self.planner.storage.session()['state'],State.DRAFT_READY.value)
  record=self.meta.get(self.planner.storage.session()['id'])
  self.assertEqual((record['available_minutes'],record['energy']),(240,'高'))
  self.assertTrue(self.orchestrator.last_draft.confirmation_required)

if __name__=='__main__': unittest.main()
