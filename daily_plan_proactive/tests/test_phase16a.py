import tempfile, unittest
from datetime import datetime, timezone
from daily_plan_proactive.scheduler import ProactiveScheduler, FakeProactiveTransport, TZ
from daily_plan_proactive.recommendations import DisabledRecommendationProvider
from daily_plan_proactive.real_transport import DeliveryResult
from daily_plan_proactive.wechat_bridge import ProactiveInboundBridge
def t(s): return datetime.fromisoformat(s).replace(tzinfo=TZ)
class Phase16ATests(unittest.TestCase):
 def make(self,available=True):
  f=tempfile.NamedTemporaryFile(suffix='.db',delete=False); f.close(); x=FakeProactiveTransport(available); s=ProactiveScheduler(f.name,x); self.addCleanup(s.close); return f.name,x,s
 def test_normal_and_duplicate(self):
  p,x,s=self.make(); self.assertTrue(s.tick(t('2026-09-04T22:00')).sent); self.assertFalse(s.tick(t('2026-09-04T22:01')).sent); self.assertEqual(len(x.sent),1); s.close()
 def test_reminder_once(self):
  p,x,s=self.make(); s.tick(t('2026-09-04T22:00')); self.assertTrue(s.tick(t('2026-09-04T22:30')).sent); self.assertFalse(s.tick(t('2026-09-04T23:00')).sent); self.assertEqual(len(x.sent),2); s.close()
 def test_reply_blocks_reminder(self):
  p,x,s=self.make(); s.tick(t('2026-09-04T22:00')); s.record_user_reply(t('2026-09-04T22:10')); self.assertEqual(s.tick(t('2026-09-04T22:30')).status,'user_replied'); self.assertEqual(len(x.sent),1)
 def test_skip_pause_resume(self):
  p,x,s=self.make(); s.skip_tonight(t('2026-09-04T21:00')); self.assertEqual(s.tick(t('2026-09-04T22:00')).status,'skipped'); s.pause_until('2026-09-05'); self.assertEqual(s.tick(t('2026-09-05T22:00')).status,'paused'); s.resume(); self.assertTrue(s.tick(t('2026-09-06T22:00')).sent)
 def test_restart(self):
  p,x,s=self.make(); s.tick(t('2026-09-04T22:00')); s.close(); s=ProactiveScheduler(p,x); self.addCleanup(s.close); self.assertFalse(s.tick(t('2026-09-04T22:01')).sent); self.assertEqual(len(x.sent),1)
 def test_unavailable_recovery_and_expiry(self):
  p,x,s=self.make(False); self.assertEqual(s.tick(t('2026-09-04T22:00')).status,'unavailable'); x.available=True; self.assertTrue(s.tick(t('2026-09-04T23:59')).sent)
  p,x,s=self.make(False); s.tick(t('2026-09-04T22:00')); self.assertEqual(s.tick(t('2026-09-05T02:00')).status,'skipped')
 def test_timezone_weekend_and_no_holiday_exception(self):
  p,x,s=self.make(); self.assertTrue(s.tick(t('2026-09-05T22:00')).sent); self.assertEqual(s.tick(datetime(2026,9,6,14,0,tzinfo=timezone.utc)).status,'proactive_sent'); self.assertTrue(s.tick(t('2026-10-01T22:00')).sent)
 def test_recommendations_disabled(self): self.assertFalse(DisabledRecommendationProvider().enabled); self.assertEqual(DisabledRecommendationProvider().recommend({}),[])
 def test_ambiguous_result_never_retries(self):
  p,x,s=self.make(); x.send=lambda text,key,now: DeliveryResult('UNKNOWN',True); self.assertEqual(s.tick(t('2026-09-04T22:00')).status,'ambiguous'); self.assertEqual(s.tick(t('2026-09-04T22:30')).status,'ambiguous')
 def test_bridge_only_suppresses_for_allowlisted_direct_reply(self):
  from ilink_receive_probe.probe.models import InboundEvent
  p,x,s=self.make(); s.tick(t('2026-09-04T22:00')); bridge=ProactiveInboundBridge(s,'me','bot')
  self.assertFalse(bridge.accept(InboundEvent('a','m','other','n','','bot'),t('2026-09-04T22:10'))); self.assertTrue(bridge.accept(InboundEvent('a','m','me','n','','bot'),t('2026-09-04T22:10'))); self.assertEqual(s.tick(t('2026-09-04T22:30')).status,'user_replied')
