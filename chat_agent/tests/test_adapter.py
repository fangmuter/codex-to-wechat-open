import unittest
from datetime import datetime, timedelta, timezone

from chat_agent.adapter import ChatAgentReceiverAdapter
from chat_agent.contacts import ContactPairingStore
from chat_agent.audit import ChatAuditStore, RECOVERY_EVENT
from chat_agent.drafts import DraftStore, PENDING
from chat_agent.inbox import ChatInboxStore
from chat_agent.model import ModelResult
from chat_agent.notifications import CallbackNotificationSink, RecoveryNotification
from chat_agent.pipeline import ChatDraftPipeline
from chat_agent.router import ChatAgentRouter
from ilink_receive_probe.probe.models import InboundEvent


UTC = timezone.utc


class _Storage:
    def inbound(self, account_scope, message_id):
        return {"context_ref": f"stored-{account_scope}-{message_id}"}


class _Receiver:
    def __init__(self, events):
        self.events = list(events)
        self.storage = _Storage()
        self.state = "IDLE"

    def receive_once(self):
        current, self.events = self.events, []
        return current


class _AcknowledgingReceiver(_Receiver):
    def __init__(self, events):
        super().__init__(events)
        self.acknowledgements = []

    def acknowledge(self, events):
        self.acknowledgements.append(tuple(events))


class _RecoveryReceiver:
    def __init__(self, recovery_events):
        self.state = "WECHAT_UNAVAILABLE"
        self.last_events = ()
        self.recovery_events = tuple(recovery_events)

    def receive_once(self):
        self.state = "IDLE"
        self.last_events = self.recovery_events
        return list(self.recovery_events)


class _Provider:
    def __init__(self):
        self.calls = []

    def generate(self, messages, *, timeout_seconds):
        self.calls.append((messages, timeout_seconds))
        return ModelResult("审核草稿", provider="fake")


class AdapterTests(unittest.TestCase):
    def test_adapter_uses_stored_context_reference_and_does_not_send(self):
        event = InboundEvent(
            "account", "m1", "sender", "received", "你好", "bot",
            "one_to_one", "RAW_TOKEN_MUST_NOT_BE_STORED", "recipient",
        )
        contacts = ContactPairingStore()
        inbox = ChatInboxStore()
        router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
        router.pair_candidate(event)
        router.contacts.confirm_candidate(
            "account", "sender", "bot", alias="小号",
            now=datetime(2026, 9, 10, tzinfo=UTC),
        )
        drafts = DraftStore()
        pipeline = ChatDraftPipeline(router, _Provider(), drafts)
        receiver = _Receiver([event])
        adapter = ChatAgentReceiverAdapter(receiver, pipeline)
        at = datetime(2026, 9, 10, 12, tzinfo=UTC)
        first = adapter.process_once(now=at)
        self.assertEqual(first.accepted_count, 1)
        self.assertEqual(first.flushed, ())
        second = adapter.process_once(now=at + timedelta(seconds=8))
        self.assertEqual(len(second.flushed), 1)
        draft = drafts.get(second.flushed[0].draft_id)
        self.assertEqual(draft["status"], PENDING)
        self.assertEqual(draft["context_ref"], "stored-account-m1")
        self.assertNotIn("RAW_TOKEN", draft["context_ref"])

        pipeline.close()
        drafts.close()
        router.close()

    def test_adapter_notifies_when_a_new_review_draft_is_ready(self):
        event = InboundEvent(
            "account", "m2", "sender", "received", "你好", "bot",
            "one_to_one", "RAW_TOKEN", "recipient",
        )
        contacts = ContactPairingStore()
        inbox = ChatInboxStore()
        router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
        router.pair_candidate(event)
        router.contacts.confirm_candidate(
            "account", "sender", "bot", alias="小号",
            now=datetime(2026, 9, 10, tzinfo=UTC),
        )
        drafts = DraftStore()
        pipeline = ChatDraftPipeline(router, _Provider(), drafts)
        events = []
        receiver = _Receiver([event])
        adapter = ChatAgentReceiverAdapter(
            receiver, pipeline, notification_sink=CallbackNotificationSink(events.append)
        )
        at = datetime(2026, 9, 10, 12, tzinfo=UTC)
        adapter.process_once(now=at)
        adapter.process_once(now=at + timedelta(seconds=8))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, PENDING)
        self.assertEqual(adapter.notification_errors, [])
        pipeline.close()
        drafts.close()
        router.close()

    def test_recovery_batch_is_skipped_and_only_count_is_notified(self):
        recovery = InboundEvent(
            "account", "old-1", "sender", "received", "旧消息", "bot",
            "one_to_one", "RAW", "recipient",
        )
        contacts = ContactPairingStore()
        inbox = ChatInboxStore()
        router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
        router.pair_candidate(recovery)
        router.contacts.confirm_candidate(
            "account", "sender", "bot", alias="小号",
            now=datetime(2026, 9, 10, tzinfo=UTC),
        )
        drafts = DraftStore()
        audit = ChatAuditStore()
        provider = _Provider()
        pipeline = ChatDraftPipeline(router, provider, drafts, audit=audit)
        notices = []
        receiver = _RecoveryReceiver([recovery, recovery])
        adapter = ChatAgentReceiverAdapter(
            receiver, pipeline, notification_sink=CallbackNotificationSink(notices.append)
        )
        result = adapter.process_once(now=datetime(2026, 9, 10, 12, tzinfo=UTC))
        self.assertTrue(result.recovered)
        self.assertEqual(result.accepted_count, 0)
        self.assertEqual(result.missed_count, 2)
        self.assertEqual(provider.calls, [])
        self.assertEqual(notices, [RecoveryNotification(2)])
        recovery_events = audit.list_events()
        self.assertEqual(len(recovery_events), 1)
        self.assertEqual(recovery_events[0].event_type, RECOVERY_EVENT)
        self.assertEqual(recovery_events[0].source_count, 2)
        pipeline.close()
        audit.close()
        drafts.close()
        router.close()

    def test_unknown_contact_is_observed_for_pairing_but_not_auto_bound(self):
        event = InboundEvent(
            "account", "candidate-1", "new-sender", "received", "候选消息", "bot",
            "one_to_one", "RAW", "recipient",
        )
        contacts = ContactPairingStore()
        inbox = ChatInboxStore()
        router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
        drafts = DraftStore()
        pipeline = ChatDraftPipeline(router, _Provider(), drafts)
        observed = []
        adapter = ChatAgentReceiverAdapter(
            _Receiver([event]), pipeline, pairing_observer=observed.append
        )
        adapter.process_once(now=datetime(2026, 9, 10, 12, tzinfo=UTC))
        self.assertEqual(observed, [event])
        self.assertIsNone(contacts.get("account", "new-sender", "bot"))
        pipeline.close()
        drafts.close()
        router.close()

    def test_default_adapter_composition_records_router_pairing_candidate(self):
        event = InboundEvent(
            "account", "candidate-default", "new-sender", "received", "候选消息", "bot",
            "one_to_one", "RAW", "recipient",
        )
        contacts = ContactPairingStore()
        inbox = ChatInboxStore()
        router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
        drafts = DraftStore()
        pipeline = ChatDraftPipeline(router, _Provider(), drafts)
        adapter = ChatAgentReceiverAdapter(_Receiver([event]), pipeline)
        adapter.process_once(now=datetime(2026, 9, 10, 12, tzinfo=UTC))
        candidates = contacts.list_candidates(now=datetime(2026, 9, 10, 12, tzinfo=UTC))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].preview_text, "候选消息")
        self.assertIsNone(contacts.get("account", "new-sender", "bot"))
        pipeline.close()
        drafts.close()
        router.close()

    def test_notification_errors_are_type_only(self):
        event = InboundEvent(
            "account", "notify-error", "sender", "received", "你好", "bot",
            "one_to_one", "RAW", "recipient",
        )
        contacts = ContactPairingStore()
        inbox = ChatInboxStore()
        router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
        router.pair_candidate(event)
        router.contacts.confirm_candidate(
            "account", "sender", "bot", alias="小号",
            now=datetime(2026, 9, 10, tzinfo=UTC),
        )
        drafts = DraftStore()
        pipeline = ChatDraftPipeline(router, _Provider(), drafts)

        def broken(_event):
            raise RuntimeError("secret message body")

        adapter = ChatAgentReceiverAdapter(
            _Receiver([event]), pipeline, notification_sink=CallbackNotificationSink(broken)
        )
        at = datetime(2026, 9, 10, 12, tzinfo=UTC)
        adapter.process_once(now=at)
        adapter.process_once(now=at.replace(second=8))
        self.assertEqual(adapter.notification_errors, ["RuntimeError"])
        pipeline.close()
        drafts.close()
        router.close()

    def test_adapter_acknowledges_after_pipeline_processing(self):
        event = InboundEvent(
            "account", "ack-1", "new-sender", "received", "候选消息", "bot",
            "one_to_one", "RAW", "recipient",
        )
        contacts = ContactPairingStore()
        inbox = ChatInboxStore()
        router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
        drafts = DraftStore()
        pipeline = ChatDraftPipeline(router, _Provider(), drafts)
        receiver = _AcknowledgingReceiver([event])
        adapter = ChatAgentReceiverAdapter(receiver, pipeline)
        adapter.process_once(now=datetime(2026, 9, 10, 12, tzinfo=UTC))
        self.assertEqual(len(receiver.acknowledgements), 1)
        self.assertEqual(receiver.acknowledgements[0][0].message_id, "ack-1")
        pipeline.close()
        drafts.close()
        router.close()


if __name__ == "__main__":
    unittest.main()
