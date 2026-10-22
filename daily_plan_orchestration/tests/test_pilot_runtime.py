import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daily_plan_mock.planner.service import DailyPlanService, MockTransport
from daily_plan_orchestration.pilot_runtime import SupervisedPilot
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
NOW=lambda: datetime(2026,9,5,22,10,tzinfo=TZ)

class PilotRuntimeTests(unittest.TestCase):
    def test_fake_receive_to_confirm_uses_existing_boundaries(self):
        root=Path(tempfile.mkdtemp()); probe=ProbeStorage(root/'probe.db'); planner=DailyPlanService(root/'planner.db',MockTransport()); bridge=None; meta=None; scheduler=None
        self.addCleanup(probe.close); self.addCleanup(planner.storage.close)
        try:
            context=ReplyContextStore(root,300,lambda:0,lambda value:b'T:'+value[::-1],lambda value:value[2:][::-1])
            reply=FakeReplyOnlyTransport(); bridge=SessionBridge(planner,probe,context,reply,root/'bridge.db'); meta=SessionMetaStore(root/'meta.db'); scheduler=ProactiveScheduler(root/'reminder.db',FakeProactiveTransport())
            orchestrator=Phase19Orchestrator(meta,'19:00-23:00'); adapter=OrchestrationAdapter(bridge,orchestrator,ProactiveInboundBridge(scheduler,'me','bot'))
            messages=('开始明日计划','完成：x\n顺延：复盘','必须：完成作业','固定：09:00 示例课程','可用时间：4小时\n精力：中','确认计划')
            batches=[]
            for index,text in enumerate(messages):
                event=InboundEvent('acct',f'm{index}','me','now',text,'bot','one_to_one',f'context-{index}',f'recipient-{index}')
                batches.append(Batch((event,),str(index)))
            receiver=Receiver(FakeIlinkTransport(batches),probe,'me','bot',context_store=context)
            pilot=SupervisedPilot(receiver,adapter,planner,meta,NOW)
            for _ in messages: pilot.process_once()
            result=pilot.evidence
            self.assertEqual(result.accepted_inbound_count,6); self.assertEqual(result.rejected_inbound_count,0)
            self.assertTrue(result.reminder_suppression_hook_invoked); self.assertTrue(result.reached_draft_ready)
            self.assertTrue(result.available_minutes_persisted); self.assertTrue(result.energy_persisted)
            self.assertTrue(result.fusion_invoked); self.assertTrue(result.recommendation_invoked)
            self.assertEqual(result.formatted_draft_api_state,'ACKED_API'); self.assertTrue(result.exact_confirmation_received)
            self.assertTrue(result.revision_matched); self.assertTrue(result.confirmed); self.assertEqual(result.confirmation_reply_api_state,'ACKED_API')
            self.assertFalse(result.duplicate_reply_attempted); self.assertEqual(scheduler.transport.sent,[])
        finally:
            if bridge: bridge.close()
            if meta: meta.close()
            if scheduler: scheduler.close()

if __name__=='__main__': unittest.main()
