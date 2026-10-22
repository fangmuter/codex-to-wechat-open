import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from daily_plan_proactive.real_transport import BoundIlinkProactiveTransport, DeliveryLedger
from ilink_receive_probe.probe.proactive_send import AmbiguousProactiveSendError
from ilink_receive_probe.probe.recipient_binding import RecipientBindingUnavailable
from daily_plan_proactive.supervised import in_2200_pilot_window, in_2230_reminder_window, reminder_preconditions
from daily_plan_proactive.scheduler import TZ
from daily_plan_proactive.neartime import production_2200_state
from daily_plan_proactive.scheduler import ProactiveScheduler, FakeProactiveTransport

TEST_RECIPIENT_FINGERPRINT = "abcdef012345"


class FakeBinding:
    def __init__(self, value='recipient'): self.value=value
    def load_for_proactive_send(self, fingerprint):
        if fingerprint != TEST_RECIPIENT_FINGERPRINT: raise RecipientBindingUnavailable()
        return self.value

class Phase16CTests(unittest.TestCase):
    def make(self):
        ledger=DeliveryLedger(Path(tempfile.mkdtemp())/'ledger.db'); self.addCleanup(ledger.close)
        transport=BoundIlinkProactiveTransport('test-token',FakeBinding(),ledger,TEST_RECIPIENT_FINGERPRINT)
        return ledger,transport
    def test_dpapi_binding_only_and_duplicate_is_durable(self):
        ledger,transport=self.make(); transport._api.send_text=lambda *args: {'ret':0}
        now=datetime(2026,9,4,22); self.assertEqual(transport.send('x','k',now).state,'ACKED_API'); self.assertEqual(transport.send('x','k',now).state,'ACKED_API'); self.assertEqual(len(transport.calls),1)
        row=ledger.get('k'); self.assertEqual(row['recipient_fingerprint'],TEST_RECIPIENT_FINGERPRINT); self.assertNotIn('recipient',' '.join(str(v) for v in row))
    def test_ambiguous_is_not_retried(self):
        _,transport=self.make(); transport._api.send_text=lambda *args: (_ for _ in ()).throw(AmbiguousProactiveSendError())
        now=datetime(2026,9,4,22); self.assertEqual(transport.send('x','k',now).state,'UNKNOWN'); self.assertEqual(transport.send('x','k',now).state,'UNKNOWN'); self.assertEqual(len(transport.calls),1)
    def test_missing_binding_fails_before_network(self):
        ledger=DeliveryLedger(Path(tempfile.mkdtemp())/'ledger.db'); self.addCleanup(ledger.close)
        class Missing:
            def load_for_proactive_send(self, fp): raise RecipientBindingUnavailable()
        transport=BoundIlinkProactiveTransport('test-token',Missing(),ledger,TEST_RECIPIENT_FINGERPRINT)
        with self.assertRaises(RecipientBindingUnavailable): transport.send('x','k',datetime.now())
        self.assertEqual(transport.calls,[])
    def test_transport_exposes_no_context_or_reply_context_surface(self):
        self.assertFalse(any('context' in name.lower() or 'reply' in name.lower() for name in dir(BoundIlinkProactiveTransport)))
    def test_supervised_runner_only_allows_2200_to_2229_shanghai(self):
        self.assertTrue(in_2200_pilot_window(datetime(2026,9,4,22,0,tzinfo=TZ)))
        self.assertTrue(in_2200_pilot_window(datetime(2026,9,4,22,29,tzinfo=TZ)))
        self.assertFalse(in_2200_pilot_window(datetime(2026,9,4,21,59,tzinfo=TZ)))
        self.assertFalse(in_2200_pilot_window(datetime(2026,9,4,22,30,tzinfo=TZ)))
    def test_near_time_namespace_and_production_state_are_isolated(self):
        root=Path(tempfile.mkdtemp()); production=root/'proactive.db'; near=root/'near.db'; now=datetime(2026,9,4,20,tzinfo=TZ)
        self.assertTrue(production_2200_state(production,now)['eligible'])
        transport=FakeProactiveTransport(); scheduler=ProactiveScheduler(near,transport,proactive_due=now,reminder_enabled=False,key_namespace='phase16c:near-time-pilot-001')
        self.addCleanup(scheduler.close); self.assertTrue(scheduler.tick(now).sent)
        self.assertTrue(transport.sent[0][1].startswith('phase16c:near-time-pilot-001:'))
        state=production_2200_state(production,now); self.assertFalse(state['proactive_sent']); self.assertFalse(state['idempotency_consumed']); self.assertFalse(state['reminder_modified']); self.assertTrue(state['eligible'])
    def test_reminder_window_and_fail_closed_preconditions(self):
        root=Path(tempfile.mkdtemp()); scheduler=ProactiveScheduler(root/'production.db',FakeProactiveTransport()); self.addCleanup(scheduler.close)
        at2200=datetime(2026,9,4,22,0,tzinfo=TZ); at2230=datetime(2026,9,4,22,30,tzinfo=TZ)
        self.assertFalse(in_2230_reminder_window(at2200)); self.assertTrue(in_2230_reminder_window(at2230)); self.assertFalse(reminder_preconditions(scheduler,at2230))
        scheduler.tick(at2200); self.assertTrue(reminder_preconditions(scheduler,at2230)); scheduler.record_user_reply(datetime(2026,9,4,22,10,tzinfo=TZ)); self.assertFalse(reminder_preconditions(scheduler,at2230))
