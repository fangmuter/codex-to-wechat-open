import tempfile, unittest
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import timezone, timedelta
from daily_plan_mock.planner.service import DailyPlanService, MockTransport
from daily_plan_mock.planner.models import State

try: TZ=ZoneInfo('Asia/Shanghai')
except ZoneInfoNotFoundError: TZ=timezone(timedelta(hours=8),'Asia/Shanghai')
def t(h,m=0,d=2): return datetime(2026,9,d,h,m,tzinfo=TZ)

class Phase11Tests(unittest.TestCase):
    def service(self, transport=None):
        service=DailyPlanService(tempfile.mktemp(suffix='.db'),transport)
        self.addCleanup(service.storage.close)
        return service
    def ready(self,s):
        s.trigger(t(22));
        for i in range(5): s.receive('me',str(i),{'text':'x'},t(22))
        s.receive('me','draft',{'text':'x','draft':{'must_do':[{'title':'A','estimated_minutes':30}], 'available_minutes':60}},t(22))
    def test_trigger_reminder_once_and_expiry(self):
        s=self.service(); self.assertEqual(s.scheduler.tick(t(22)),State.ASK_REVIEW); self.assertEqual(s.scheduler.tick(t(22,30)),State.WAITING_2230_REMINDER); self.assertEqual(s.scheduler.tick(t(22,31)),State.WAITING_2230_REMINDER)
    def test_skip_pause_resume(self):
        s=self.service(); s.trigger(t(22)); self.assertEqual(s.skip_tonight(t(22)),State.SKIPPED_TONIGHT); self.assertEqual(s.pause('2026-09-05'),State.PAUSED_UNTIL_DATE); self.assertEqual(s.resume(),State.IDLE)
    def test_multiround_and_draft_limit_overload(self):
        s=self.service(); s.trigger(t(22));
        for i in range(4): s.receive('me',str(i),{'text':'x'},t(22))
        s.receive('me','4',{'text':'x','draft':{'must_do':[{'title':str(i),'estimated_minutes':60} for i in range(5)],'available_minutes':60}},t(22)); self.assertEqual(s.storage.session()['state'],State.DRAFT_READY.value); import json; self.assertEqual(len(json.loads(s.storage.session()['draft_json'])['must_do']),3); self.assertTrue(json.loads(s.storage.session()['draft_json'])['overload'])
    def test_exact_confirmation_only(self):
        s=self.service(); self.ready(s); self.assertEqual(s.receive('me','bad',{'text':'确认'},t(22)),State.DRAFT_READY); self.assertEqual(s.receive('me','ok',{'text':' 确认计划\n'},t(22)),State.CONFIRMED)
    def test_dedupe(self):
        s=self.service(); s.trigger(t(22)); a=s.receive('me','same',{'text':'x'},t(22)); b=s.receive('me','same',{'text':'x'},t(22)); self.assertEqual(a,b); self.assertEqual(s.storage.db.execute('select count(*) from inbound_messages').fetchone()[0],1)
    def test_midnight_and_expired(self):
        s=self.service(); self.ready(s); self.assertEqual(s.storage.session()['target_date'],'2026-09-03'); self.assertEqual(s.receive('me','confirm',{'text':'确认计划'},t(1,59,3)),State.CONFIRMED); s2=self.service(); self.ready(s2); self.assertEqual(s2.receive('me','confirm',{'text':'确认计划'},t(2,1,3)),State.DRAFT_EXPIRED_AFTER_0200)
    def test_partial_and_retry(self):
        tr=MockTransport({'CODEX':False}); s=self.service(tr); self.ready(s); self.assertEqual(s.receive('me','c',{'text':'确认计划'},t(22)),State.CONFIRMED_PARTIAL); tr.outcomes['CODEX']=True; self.assertEqual(s.deliver(s.storage.plan(s.storage.db.execute('select plan_id from plans').fetchone()[0])['plan_id']),State.CONFIRMED)
    def test_recovery_and_emergency(self):
        path=tempfile.mktemp(suffix='.db'); s=DailyPlanService(path); self.addCleanup(s.storage.close); s.trigger(t(22)); s.storage.close(); s2=DailyPlanService(path); self.addCleanup(s2.storage.close); self.assertEqual(s2.storage.session()['target_date'],'2026-09-03'); s2.emergency_disable(); self.assertEqual(s2.receive('me','x',{'text':'x'},t(22)),State.EMERGENCY_DISABLED)
    def test_renderer_stable(self):
        s=self.service(); self.ready(s); self.assertEqual(s.receive('me','c',{'text':'确认计划'},t(22)),State.CONFIRMED); row=s.storage.db.execute('select canonical_text from plans').fetchone(); self.assertEqual(row['canonical_text'],row['canonical_text'])
    def test_after_2230_no_new_reminder(self):
        s=self.service(); s.trigger(t(22)); s.scheduler.tick(t(22,30)); self.assertEqual(s.scheduler.tick(t(23)),State.WAITING_2230_REMINDER)
    def test_pause_blocks_scheduler(self):
        s=self.service(); s.pause('2026-09-05'); self.assertEqual(s.scheduler.tick(t(22)),State.PAUSED_UNTIL_DATE)
    def test_resume_allows_trigger(self):
        s=self.service(); s.pause('2026-09-05'); s.resume(); self.assertEqual(s.scheduler.tick(t(22)),State.ASK_REVIEW)
    def test_old_tasks_not_auto_carried(self):
        s=self.service(); s.trigger(t(22)); self.assertIsNone(s.storage.db.execute('select * from tasks').fetchone())
    def test_explicit_carryover_stage(self):
        s=self.service(); s.trigger(t(22));
        for i in range(4): s.receive('me',str(i),{'text':'x'},t(22))
        self.assertEqual(s.storage.session()['state'],State.ASK_CARRYOVER.value)
    def test_fourth_must_do_overload(self):
        s=self.service(); self.ready(s); self.assertLessEqual(len(__import__('json').loads(s.storage.session()['draft_json'])['must_do']),3)
    def test_confirmation_variants_rejected(self):
        for text in ['好','可以','确认一下','我确认计划了','确认计划吧','👍','“确认计划”']:
            s=self.service(); self.ready(s); self.assertEqual(s.receive('me',text,{'text':text},t(22)),State.DRAFT_READY)
    def test_plan_id_immutable(self):
        s=self.service(); self.ready(s); s.receive('me','c',{'text':'确认计划'},t(22)); p=s.storage.db.execute('select plan_id from plans').fetchone()[0]; s.deliver(p); self.assertEqual(s.storage.db.execute('select count(*) from plans').fetchone()[0],1)
    def test_outbox_three_destinations(self):
        s=self.service(); self.ready(s); s.receive('me','c',{'text':'确认计划'},t(22)); self.assertEqual(s.storage.db.execute('select count(*) from outbox').fetchone()[0],3)
    def test_wechat_failure_partial(self):
        s=self.service(MockTransport({'WECHAT':False})); self.ready(s); self.assertEqual(s.receive('me','c',{'text':'确认计划'},t(22)),State.CONFIRMED_PARTIAL)
    def test_local_ack(self):
        s=self.service(); self.ready(s); s.receive('me','c',{'text':'确认计划'},t(22)); self.assertEqual(s.storage.db.execute("select status from outbox where destination='LOCAL'").fetchone()[0],'ACK')
    def test_stable_idempotency_key(self):
        s=self.service(); self.ready(s); s.receive('me','c',{'text':'确认计划'},t(22)); keys=[r['idempotency_key'] for r in s.storage.outbox()]; s.deliver(s.storage.db.execute('select plan_id from plans').fetchone()[0]); self.assertEqual(keys,[r['idempotency_key'] for r in s.storage.outbox()])
    def test_settings_persist(self):
        s=self.service(); s.emergency_disable(); self.assertEqual(s.storage.setting('wechat_gateway_enabled'),'false')
    def test_emergency_enable_local(self):
        s=self.service(); s.emergency_disable(); s.enable(); self.assertEqual(s.storage.setting('wechat_gateway_enabled'),'true')
    def test_target_date_pinned(self):
        s=self.service(); s.trigger(t(22)); self.assertEqual(s.storage.session()['target_date'],'2026-09-03')
    def test_schema_foreign_keys(self):
        s=self.service(); self.assertEqual(s.storage.db.execute('pragma foreign_keys').fetchone()[0],1)
    def test_cleanup_keeps_recent(self):
        s=self.service(); s.trigger(t(22)); s.storage.cleanup('2026-08-04'); self.assertIsNotNone(s.storage.session())

if __name__=='__main__': unittest.main()
