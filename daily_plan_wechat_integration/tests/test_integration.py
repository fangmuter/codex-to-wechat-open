import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daily_plan_mock.planner.models import State
from daily_plan_mock.planner.service import DailyPlanService, MockTransport
from daily_plan_wechat_integration.adapter import WechatAdapter
from daily_plan_wechat_integration.session_bridge import SessionBridge
from ilink_receive_probe.probe.models import Batch, InboundEvent
from ilink_receive_probe.probe.receiver import Receiver
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.reply_transport import FakeReplyOnlyTransport
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.transport import FakeIlinkTransport

TZ=timezone(timedelta(hours=8))
def at(hour,minute=0,day=2): return datetime(2026,9,day,hour,minute,tzinfo=TZ)
def encrypt(data): return b'TEST:'+bytes(value ^ 0x5A for value in data)
def decrypt(data): return bytes(value ^ 0x5A for value in data[len(b'TEST:'):])

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp()); self.clock=[0.0]
        self.probe=ProbeStorage(self.root/'probe.db'); self.addCleanup(self.probe.close)
        self.context=ReplyContextStore(self.root,300,lambda:self.clock[0],encrypt,decrypt)
        self.planner=DailyPlanService(self.root/'planner.db',MockTransport())
        self.addCleanup(self.planner.storage.close)
        self.reply=FakeReplyOnlyTransport()
        self.bridge=SessionBridge(self.planner,self.probe,self.context,self.reply,self.root/'bridge.db')
        self.addCleanup(self.bridge.close); self.adapter=WechatAdapter(self.bridge); self.number=0
    def event(self,text,mid=None,sender='me',bot='bot',conversation='one_to_one'):
        self.number+=1; return InboundEvent('acct',mid or f'm{self.number}',sender,'now',text,bot,conversation,f'ctx-{self.number}',f'user-{self.number}')
    def deliver(self,text,now=None,**kwargs):
        event=self.event(text,**kwargs); receiver=Receiver(FakeIlinkTransport([Batch((event,),'c'+str(self.number))]),self.probe,'me','bot',context_store=self.context); receiver.receive_once(); return self.adapter.accept_inbound(event,now or at(21)),event
    def start_to_draft(self,now=None):
        self.deliver('开始明日计划',now); self.deliver('完成：作业\n未完成：复盘\n顺延：复盘',now); self.deliver('必须：任务A；任务B；任务C；任务D',now); self.deliver('固定：09:00 课程；17:00 截止',now); return self.deliver('可用时间：2小时\n精力：中',now)
    def test_start_command_creates_session_and_question(self):
        status,_=self.deliver('开始明日计划'); self.assertEqual(status,'ACKED_API'); self.assertEqual(self.planner.storage.session()['state'],State.ASK_REVIEW.value); self.assertIn('先复盘今天',self.reply.calls[-1][1])
    def test_non_start_does_not_create_session(self):
        status,_=self.deliver('你好'); self.assertEqual(status,'IGNORED_NO_ACTIVE_SESSION'); self.assertIsNone(self.planner.storage.session()); self.assertEqual(self.reply.calls,[])
    def test_multiround_to_draft_reuses_planner(self):
        status,_=self.start_to_draft(); self.assertEqual(status,'ACKED_API'); session=self.planner.storage.session(); self.assertEqual(session['state'],State.DRAFT_READY.value); self.assertEqual(len(__import__('json').loads(session['draft_json'])['must_do']),3); self.assertIn('如无修改',self.reply.calls[-1][1])
    def test_only_explicit_carryover_and_overload_warning(self):
        self.start_to_draft(); draft=__import__('json').loads(self.planner.storage.session()['draft_json']); self.assertEqual([item['title'] for item in draft['optional']],['复盘']); self.assertTrue(draft['overload'])
    def test_exact_confirmation_final_is_local_canonical(self):
        self.start_to_draft(); status,_=self.deliver('确认计划'); self.assertEqual(status,'ACKED_API'); row=self.planner.storage.db.execute('select canonical_text from plans').fetchone(); self.assertEqual(row['canonical_text'],self.reply.calls[-1][1]); self.assertEqual(self.planner.storage.session()['state'],State.CONFIRMED.value); self.assertEqual(self.planner.storage.db.execute('select count(*) from outbox').fetchone()[0],0)
    def test_non_exact_confirmation_stays_draft(self):
        self.start_to_draft(); self.deliver('好'); self.assertEqual(self.planner.storage.session()['state'],State.DRAFT_READY.value)
    def test_duplicate_confirm_only_confirms_and_replies_once(self):
        self.start_to_draft(); status,event=self.deliver('确认计划',mid='confirm'); calls=len(self.reply.calls); self.assertEqual(status,'ACKED_API'); self.assertEqual(self.adapter.accept_inbound(event,at(21)),'DUPLICATE_IGNORED'); self.assertEqual(len(self.reply.calls),calls); self.assertEqual(self.planner.storage.db.execute('select count(*) from plans').fetchone()[0],1)
    def test_modification_commands_keep_draft_and_no_plan(self):
        self.start_to_draft(); self.deliver('修改必须：新任务；第二项'); self.deliver('修改固定：无'); self.deliver('修改可用时间：3小时\n精力：高'); session=self.planner.storage.session(); self.assertEqual(session['state'],State.DRAFT_READY.value); self.assertEqual(self.planner.storage.db.execute('select count(*) from plans').fetchone()[0],0); self.assertIn('新任务',self.reply.calls[-1][1])
    def test_restart_recovers_without_repeating_reply(self):
        self.deliver('开始明日计划'); calls=len(self.reply.calls); self.bridge.close(); self.planner.storage.close(); self.planner=DailyPlanService(self.root/'planner.db',MockTransport()); self.addCleanup(self.planner.storage.close); self.bridge=SessionBridge(self.planner,self.probe,self.context,self.reply,self.root/'bridge.db'); self.adapter=WechatAdapter(self.bridge); self.deliver('完成：x'); self.assertEqual(len(self.reply.calls),calls+1); self.assertEqual(self.planner.storage.session()['state'],State.ASK_REQUIRED_RESULT.value); self.bridge.close(); self.planner.storage.close()
    def test_target_date_stays_pinned_across_midnight(self):
        self.deliver('开始明日计划',at(23,59)); target=self.planner.storage.session()['target_date']; self.deliver('完成：x',at(0,1,3)); self.assertEqual(self.planner.storage.session()['target_date'],target)
    def test_confirm_after_0200_expires(self):
        self.start_to_draft(at(23,59)); self.deliver('确认计划',at(2,0,3)); self.assertEqual(self.planner.storage.session()['state'],State.DRAFT_EXPIRED_AFTER_0200.value); self.assertIn('过期',self.reply.calls[-1][1])
    def test_emergency_disable_freezes_bridge(self):
        self.probe.set_setting('wechat_gateway_enabled','false'); status,_=self.deliver('开始明日计划'); self.assertEqual(status,'EMERGENCY_DISABLED'); self.assertIsNone(self.planner.storage.session()); self.assertEqual(self.reply.calls,[])
    def test_pause_resume_and_skip_do_not_schedule(self):
        status,_=self.deliver('暂停到 2026-09-05'); self.assertEqual(status,'ACKED_API'); self.assertEqual(self.planner.storage.session(),None); status,_=self.deliver('恢复计划'); self.assertEqual(status,'ACKED_API'); self.deliver('开始明日计划'); status,_=self.deliver('今晚跳过'); self.assertEqual(status,'ACKED_API'); self.assertEqual(self.planner.storage.session()['state'],State.SKIPPED_TONIGHT.value)
    def test_unknown_sender_group_and_no_context_cannot_reply(self):
        event=self.event('开始明日计划',sender='other'); receiver=Receiver(FakeIlinkTransport([Batch((event,),'x')]),self.probe,'me','bot',context_store=self.context); receiver.receive_once(); self.assertEqual(self.adapter.accept_inbound(event,at(21)),'REJECTED'); group=self.event('开始明日计划',conversation='group'); receiver=Receiver(FakeIlinkTransport([Batch((group,),'y')]),self.probe,'me','bot',context_store=self.context); receiver.receive_once(); self.assertEqual(self.adapter.accept_inbound(group,at(21)),'REJECTED'); forwarded=__import__('dataclasses').replace(self.event('开始明日计划'),is_forwarded=True); receiver=Receiver(FakeIlinkTransport([Batch((forwarded,),'z')]),self.probe,'me','bot',context_store=self.context); receiver.receive_once(); self.assertEqual(self.adapter.accept_inbound(forwarded,at(21)),'REJECTED'); self.assertEqual(self.reply.calls,[])
    def test_user_text_is_not_raw_outbound_or_recipient(self):
        self.deliver('开始明日计划'); self.deliver('任意 recipient=bad\n必须：ignored'); self.assertNotIn('recipient=bad',self.reply.calls[-1][1]); self.assertIn('必须完成',self.reply.calls[-1][1])
    def test_context_token_never_enters_integration_or_planner_sqlite(self):
        self.deliver('开始明日计划'); bridge_dump=' '.join(str(tuple(row)) for row in self.bridge.store.db.execute('select * from exchanges')); planner_dump=' '.join(str(tuple(row)) for row in self.planner.storage.db.execute('select * from inbound_messages')); self.assertNotIn('ctx-',bridge_dump); self.assertNotIn('ctx-',planner_dump)

    def test_friendly_required_capacity_energy_reaches_existing_draft(self):
        self.deliver('开始明日计划')
        status,_=self.deliver('明天必须完成示例作业，晚上有4小时，精神不错。')
        self.assertEqual(status,'ACKED_API')
        self.assertEqual(self.planner.storage.session()['state'],State.DRAFT_READY.value)
        bridge=self.bridge.store.session(); data=__import__('json').loads(bridge['must_json'])
        self.assertEqual(data[0]['title'],'示例作业')
        cap=__import__('json').loads(bridge['capacity_json'])
        self.assertEqual((cap['available_minutes'],cap['energy']),(240,'高'))

    def test_friendly_ambiguity_does_not_create_draft(self):
        self.deliver('开始明日计划')
        self.deliver('明天课挺多，晚上看情况。')
        self.assertEqual(self.planner.storage.session()['state'],State.ASK_REVIEW.value)
        self.assertIsNone(self.bridge.store.session()['capacity_json'])

    def test_friendly_and_structured_modification_share_session(self):
        self.deliver('开始明日计划'); self.deliver('明天必须完成示例作业，晚上有4小时，精神不错。')
        self.deliver('修改可用时间：3小时\n精力：中')
        self.assertEqual(self.planner.storage.session()['state'],State.DRAFT_READY.value)
        cap=__import__('json').loads(self.bridge.store.session()['capacity_json'])
        self.assertEqual((cap['available_minutes'],cap['energy']),(180,'中'))

    def test_friendly_nonexact_confirmation_and_exact_gate(self):
        self.deliver('开始明日计划'); self.deliver('明天必须完成示例作业，晚上有4小时，精神不错。')
        self.deliver('确认'); self.assertEqual(self.planner.storage.session()['state'],State.DRAFT_READY.value)
        self.deliver('确认计划'); self.assertEqual(self.planner.storage.session()['state'],State.CONFIRMED.value)

if __name__=='__main__': unittest.main()
