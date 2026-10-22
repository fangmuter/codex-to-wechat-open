import unittest
from datetime import datetime, timezone

from chat_agent.audit import ChatAuditStore, SEND_EVENT
from chat_agent.contacts import ContactPairingStore
from chat_agent.drafts import (
    APPROVED,
    DELIVERY_UNCONFIRMED_STATE,
    DraftConflictError,
    DraftStore,
    SEND_UNKNOWN,
    SENT_CONFIRMED,
)
from chat_agent.send_coordinator import (
    ContextUnavailableError,
    ManualSendCoordinator,
    SendConfirmationRequired,
    SendNotAllowedError,
)
from ilink_receive_probe.probe.models import InboundEvent
from ilink_receive_probe.probe.send_result import (
    API_ACCEPTED_WITH_RECEIPT,
    ClassifiedSendResult,
    DELIVERY_UNCONFIRMED,
)


UTC = timezone.utc


class _Transport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def send(self, context, idempotency_key, text):
        self.calls.append((context, idempotency_key, text))
        return self.result


def _contact_store():
    store = ContactPairingStore()
    event = InboundEvent(
        "account", "m1", "sender", "received", "你好", "bot",
        "one_to_one", "raw", "recipient",
    )
    at = datetime.now(UTC).replace(microsecond=0)
    store.observe_candidate("account", "sender", "bot", "m1", observed_at=at)
    store.confirm_candidate("account", "sender", "bot", alias="小号", now=at)
    return store


def _drafts(contact_store):
    drafts = DraftStore()
    created = datetime.now(UTC).replace(microsecond=0)
    row = drafts.create_draft(
        "account", "account\x1fsender\x1fbot", "batch-1", ["m1"],
        "m1", "ctx-ref", "草稿内容",
        created_at=created,
    )
    drafts.approve(row["draft_id"], now=created)
    return drafts, row["draft_id"]


def _context():
    return {
        "account_scope": "account",
        "sender_fingerprint": "sender",
        "bot_fingerprint": "bot",
        "recipient": "raw-recipient",
        "context_token": "raw-context",
        "conversation_type": "one_to_one",
        "is_forwarded": False,
    }


class ManualSendCoordinatorTests(unittest.TestCase):
    def test_requires_second_explicit_confirmation_and_records_receipt(self):
        contacts = _contact_store()
        drafts, draft_id = _drafts(contacts)
        transport = _Transport(
            ClassifiedSendResult(API_ACCEPTED_WITH_RECEIPT, True, False, receipt_id="receipt-1")
        )
        audit = ChatAuditStore()
        coordinator = ManualSendCoordinator(
            drafts, transport, contacts,
            lambda ref: _context(),
            audit=audit,
        )
        with self.assertRaises(SendConfirmationRequired):
            coordinator.send_approved(draft_id)
        outcome = coordinator.send_approved(draft_id, confirmed=True)
        self.assertEqual(outcome.status, SENT_CONFIRMED)
        self.assertEqual(outcome.receipt_id, "receipt-1")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1], draft_id)
        events = audit.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, SEND_EVENT)
        self.assertEqual(events[0].send_state, API_ACCEPTED_WITH_RECEIPT)
        self.assertNotIn("raw-context", repr(events))
        with self.assertRaises(DraftConflictError):
            coordinator.send_approved(draft_id, confirmed=True)
        audit.close()
        drafts.close()
        contacts.close()

    def test_unconfirmed_result_is_not_marked_sent_and_not_retryable(self):
        contacts = _contact_store()
        drafts, draft_id = _drafts(contacts)
        transport = _Transport(ClassifiedSendResult(DELIVERY_UNCONFIRMED, False, False))
        coordinator = ManualSendCoordinator(
            drafts, transport, contacts,
            lambda ref: _context(),
        )
        outcome = coordinator.send_approved(draft_id, confirmed=True)
        self.assertEqual(outcome.status, DELIVERY_UNCONFIRMED_STATE)
        self.assertFalse(outcome.confirmed)
        self.assertFalse(outcome.retry_allowed)
        self.assertIsNone(drafts.get(draft_id)["sent_at"])
        drafts.close()
        contacts.close()

    def test_context_and_contact_policy_fail_closed_before_transport(self):
        contacts = _contact_store()
        drafts, draft_id = _drafts(contacts)
        transport = _Transport(ClassifiedSendResult(API_ACCEPTED_WITH_RECEIPT, True, False))
        coordinator = ManualSendCoordinator(drafts, transport, contacts, lambda _ref: None)
        with self.assertRaises(ContextUnavailableError):
            coordinator.send_approved(draft_id, confirmed=True)
        self.assertEqual(drafts.get(draft_id)["status"], APPROVED)
        self.assertEqual(transport.calls, [])
        contacts.set_mode("account", "sender", "bot", "disabled")
        with self.assertRaises(SendNotAllowedError):
            ManualSendCoordinator(
                drafts, transport, contacts,
                lambda _ref: _context(),
            ).send_approved(draft_id, confirmed=True)
        self.assertEqual(transport.calls, [])
        drafts.close()
        contacts.close()

    def test_invalid_transport_result_becomes_unknown_and_is_not_retried(self):
        contacts = _contact_store()
        drafts, draft_id = _drafts(contacts)
        coordinator = ManualSendCoordinator(
            drafts, _Transport(object()), contacts,
            lambda _ref: _context(),
        )
        outcome = coordinator.send_approved(draft_id, confirmed=True)
        self.assertEqual(outcome.status, SEND_UNKNOWN)
        self.assertFalse(outcome.confirmed)
        with self.assertRaises(DraftConflictError):
            coordinator.send_approved(draft_id, confirmed=True)
        drafts.close()
        contacts.close()

    def test_context_for_another_contact_is_rejected_before_transport(self):
        contacts = _contact_store()
        drafts, draft_id = _drafts(contacts)
        transport = _Transport(
            ClassifiedSendResult(API_ACCEPTED_WITH_RECEIPT, True, False, receipt_id="receipt")
        )
        wrong = _context()
        wrong["sender_fingerprint"] = "other-sender"
        coordinator = ManualSendCoordinator(
            drafts, transport, contacts, lambda _ref: wrong,
        )
        with self.assertRaises(ContextUnavailableError):
            coordinator.send_approved(draft_id, confirmed=True)
        self.assertEqual(transport.calls, [])
        self.assertEqual(drafts.get(draft_id)["status"], APPROVED)
        drafts.close()
        contacts.close()


if __name__ == "__main__":
    unittest.main()
