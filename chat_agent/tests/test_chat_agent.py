import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from chat_agent.contacts import (
    ConsentRequiredError,
    ContactPairingStore,
    PairingExpiredError,
)
from chat_agent.drafts import (
    APPROVED,
    DELIVERY_UNCONFIRMED_STATE,
    DraftConflictError,
    DraftExpiredError,
    DraftStore,
    SensitiveConfirmationRequired,
    SUPERSEDED,
)
from ilink_receive_probe.probe.send_result import (
    API_ACCEPTED_WITH_RECEIPT,
    DEFINITE_FAILED,
    DELIVERY_UNCONFIRMED,
    INVALID_RESPONSE,
    classify_send_response,
)
from ilink_receive_probe.probe.reply_transport import (
    FakeReplyOnlyTransport,
    RenderedReply,
    ReplyOnlyTransport,
    UnconfirmedSendError,
)
import ilink_receive_probe.probe.reply_transport as reply_transport_module
from chat_agent.transport import ChatReplyTransport
from chat_agent.inbox import CLAIMED, COMPLETED, ChatInboxStore, stable_event_key
from chat_agent.router import (
    CHAT_AGENT_DISABLED,
    DUPLICATE_IGNORED,
    IGNORED_CONTACT,
    MATCHED_CONTACT,
    ChatAgentRouter,
)
from ilink_receive_probe.probe.models import InboundEvent


UTC = timezone.utc


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class ChatAgentTests(unittest.TestCase):
    def test_send_result_requires_explicit_receipt_for_confirmation(self):
        confirmed = classify_send_response({"ret": 0, "message_id": 123})
        self.assertEqual(confirmed.state, API_ACCEPTED_WITH_RECEIPT)
        self.assertTrue(confirmed.confirmed)
        self.assertEqual(confirmed.receipt_id, "123")

        unconfirmed = classify_send_response({"ret": 0})
        self.assertEqual(unconfirmed.state, DELIVERY_UNCONFIRMED)
        self.assertFalse(unconfirmed.confirmed)
        self.assertFalse(unconfirmed.retry_allowed)

        self.assertEqual(classify_send_response({"ret": -14}).state, DEFINITE_FAILED)
        self.assertEqual(classify_send_response({}).state, INVALID_RESPONSE)

    def test_strict_reply_transport_requires_receipt(self):
        original = reply_transport_module.urlopen
        self.addCleanup(setattr, reply_transport_module, "urlopen", original)
        reply_transport_module.urlopen = lambda *_args, **_kwargs: _Response(b'{"ret":0}')
        transport = ReplyOnlyTransport("token", require_receipt=True)
        with self.assertRaises(UnconfirmedSendError):
            transport.reply_bound_rendered_context(
                {"recipient": "recipient", "context_token": "ctx"},
                "key",
                RenderedReply("draft"),
            )
        reply_transport_module.urlopen = lambda *_args, **_kwargs: _Response(
            b'{"ret":0,"message_id":42}'
        )
        result = transport.reply_bound_rendered_context(
            {"recipient": "recipient", "context_token": "ctx"},
            "key-2",
            RenderedReply("draft"),
        )
        self.assertEqual(result["message_id"], 42)

    def test_chat_transport_exposes_unconfirmed_without_retry(self):
        unconfirmed = ChatReplyTransport(
            transport=FakeReplyOnlyTransport(),
        ).send(
            {"recipient": "recipient", "context_token": "ctx"},
            "key",
            "draft",
        )
        self.assertEqual(unconfirmed.state, DELIVERY_UNCONFIRMED)
        self.assertFalse(unconfirmed.retry_allowed)

        confirmed = ChatReplyTransport(
            transport=FakeReplyOnlyTransport(require_receipt=True),
        ).send(
            {"recipient": "recipient", "context_token": "ctx"},
            "key-2",
            "draft",
        )
        self.assertEqual(confirmed.state, API_ACCEPTED_WITH_RECEIPT)
        self.assertTrue(confirmed.confirmed)

    def test_pairing_requires_manual_confirmation_and_consent_for_auto(self):
        store = ContactPairingStore()
        self.addCleanup(store.close)
        observed = datetime(2026, 9, 9, 12, tzinfo=UTC)
        store.observe_candidate("chat", "sender", "bot", "m1", observed_at=observed)
        with self.assertRaises(ConsentRequiredError):
            store.confirm_candidate(
                "chat", "sender", "bot", alias="小号", mode="auto",
                now=observed,
            )
        contact = store.confirm_candidate(
            "chat", "sender", "bot", alias="小号", mode="review",
            now=observed,
        )
        self.assertEqual(contact.mode, "review")
        self.assertTrue(store.is_allowed("chat", "sender", "bot"))
        store.record_consent("chat", "sender", "bot", now=observed)
        self.assertEqual(
            store.set_mode("chat", "sender", "bot", "auto", now=observed).mode,
            "auto",
        )

    def test_inbox_claim_is_account_scoped_and_replay_safe(self):
        store = ChatInboxStore()
        self.addCleanup(store.close)
        event = {"message_id": "m1", "from_user_id": "sender", "text": "hello"}
        first = store.claim("account-a", event)
        self.assertTrue(first.claimed)
        self.assertEqual(first.state, CLAIMED)
        duplicate = store.claim("account-a", event)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.key, stable_event_key(event))
        other_account = store.claim("account-b", event)
        self.assertTrue(other_account.claimed)
        store.complete("account-a", first.key)
        replay_after_complete = store.claim("account-a", event)
        self.assertTrue(replay_after_complete.duplicate)
        self.assertEqual(replay_after_complete.state, COMPLETED)

    def test_inbox_claim_is_single_winner_across_threads(self):
        store = ChatInboxStore()
        self.addCleanup(store.close)
        event = {"message_id": "threaded", "from_user_id": "sender", "text": "hello"}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _index: store.claim("account", event), range(8)))
        self.assertEqual(sum(result.claimed for result in results), 1)
        self.assertEqual(sum(result.duplicate for result in results), 7)

    def test_expired_candidate_cannot_bind(self):
        store = ContactPairingStore()
        self.addCleanup(store.close)
        observed = datetime(2026, 9, 9, 12, tzinfo=UTC)
        store.observe_candidate("chat", "sender", "bot", "m1", observed_at=observed)
        with self.assertRaises(PairingExpiredError):
            store.confirm_candidate(
                "chat", "sender", "bot", alias="过期",
                now=observed + timedelta(minutes=5),
            )

    def test_pairing_candidate_lists_bounded_preview_until_expiry(self):
        store = ContactPairingStore()
        self.addCleanup(store.close)
        observed = datetime(2026, 9, 9, 12, tzinfo=UTC)
        store.observe_candidate(
            "chat", "sender", "bot", "m1", observed_at=observed,
            preview_text="候选消息内容",
        )
        candidates = store.list_candidates(now=observed + timedelta(seconds=1))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].preview_text, "候选消息内容")
        self.assertEqual(
            store.list_candidates(now=observed + timedelta(minutes=5)), ()
        )
        store.confirm_candidate("chat", "sender", "bot", alias="小号", now=observed)
        self.assertEqual(store.list_candidates(now=observed + timedelta(seconds=1)), ())

    def test_router_never_auto_binds_unknown_sender(self):
        router = ChatAgentRouter(enabled=True)
        self.addCleanup(router.close)
        event = InboundEvent(
            "chat", "m1", "unknown-sender", "now", "hello", "bot",
            "one_to_one", "ctx", "raw-sender",
        )
        self.assertEqual(router.route(event).route, IGNORED_CONTACT)
        self.assertTrue(router.pair_candidate(event))
        self.assertEqual(router.route(event).route, IGNORED_CONTACT)
        router.contacts.confirm_candidate(
            "chat", "unknown-sender", "bot", alias="小号",
            now=datetime(2026, 9, 9, 12, tzinfo=UTC),
        )
        matched = router.route(
            event, now=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
        )
        self.assertEqual(matched.route, MATCHED_CONTACT)
        self.assertEqual(
            router.route(
                event, now=datetime(2026, 9, 9, 12, 1, 30, tzinfo=UTC),
            ).route,
            DUPLICATE_IGNORED,
        )

    def test_router_disabled_is_fail_closed(self):
        router = ChatAgentRouter(enabled=False)
        self.addCleanup(router.close)
        event = InboundEvent(
            "chat", "m1", "sender", "now", "hello", "bot",
            "one_to_one", "ctx", "raw-sender",
        )
        self.assertEqual(router.route(event).route, CHAT_AGENT_DISABLED)

    def test_new_batch_supersedes_old_draft_and_sensitive_send_is_gated(self):
        store = DraftStore()
        self.addCleanup(store.close)
        created = datetime(2026, 9, 9, 12, tzinfo=UTC)
        first = store.create_draft(
            "chat", "sender", "batch-1", ["m1"], "m1", "ctx-1", "第一版",
            created_at=created,
        )
        second = store.create_draft(
            "chat", "sender", "batch-2", ["m1", "m2"], "m2", "ctx-2", "第二版",
            created_at=created + timedelta(seconds=8),
        )
        self.assertEqual(store.get(first["draft_id"])["status"], SUPERSEDED)
        with self.assertRaises(SensitiveConfirmationRequired):
            store.approve(second["draft_id"], sensitive=True, now=created)
        approved = store.approve(
            second["draft_id"], edited_text="人工确认版",
            sensitive=True, second_confirmation=True, now=created,
        )
        self.assertEqual(approved["status"], APPROVED)
        store.begin_send(second["draft_id"], now=created)
        result = store.record_send_state(
            second["draft_id"], DELIVERY_UNCONFIRMED, now=created,
        )
        self.assertEqual(result["status"], DELIVERY_UNCONFIRMED_STATE)
        self.assertIsNone(result["sent_at"])

    def test_expired_draft_cannot_be_approved(self):
        store = DraftStore()
        self.addCleanup(store.close)
        created = datetime(2026, 9, 9, 12, tzinfo=UTC)
        draft = store.create_draft(
            "chat", "sender", "batch-1", ["m1"], "m1", "ctx-1", "回复",
            created_at=created, expires_in=timedelta(minutes=30),
        )
        with self.assertRaises(DraftExpiredError):
            store.approve(
                draft["draft_id"], now=created + timedelta(minutes=30),
            )
        self.assertEqual(store.get(draft["draft_id"])["status"], "EXPIRED")

    def test_needs_user_input_can_be_resolved_by_explicit_manual_text(self):
        store = DraftStore()
        self.addCleanup(store.close)
        created = datetime(2026, 9, 9, 12, tzinfo=UTC)
        draft = store.create_draft(
            "chat", "sender", "batch-1", ["m1"], "m1", "ctx-1", None,
            needs_user_input=True, risk_flags=("MISSING_STANCE",), created_at=created,
        )
        with self.assertRaises(DraftConflictError):
            store.approve(draft["draft_id"], now=created)
        approved = store.approve(
            draft["draft_id"], edited_text="我先确认后再回复。", now=created,
        )
        self.assertEqual(approved["status"], APPROVED)
        self.assertEqual(approved["final_text"], "我先确认后再回复。")
        self.assertEqual(approved["needs_user_input"], 0)

    def test_high_risk_flag_requires_second_confirmation_even_without_sensitive_parameter(self):
        store = DraftStore()
        self.addCleanup(store.close)
        created = datetime(2026, 9, 9, 12, tzinfo=UTC)
        draft = store.create_draft(
            "chat", "sender", "batch-risk", ["m-risk"], "m-risk", "ctx-risk", "草稿",
            risk_flags=("RISK_HIGH", "MONEY"), created_at=created,
        )
        with self.assertRaises(SensitiveConfirmationRequired):
            store.approve(draft["draft_id"], now=created)
        approved = store.approve(
            draft["draft_id"], second_confirmation=True, now=created,
        )
        self.assertEqual(approved["status"], APPROVED)


if __name__ == "__main__":
    unittest.main()
