from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from ilink_receive_probe.probe.models import InboundEvent
from ilink_receive_probe.probe.proactive_send import IlinkResponseDiagnostics
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.session_health import (
    OUTBOUND_BLOCKED,
    SessionHealthManager,
    SessionHealthStore,
)
from ilink_receive_probe.probe.single_shot_v1 import (
    ContextBoundAuthorization,
    SingleShotV1ProbeFlow,
    StaticPreflight,
    build_synthetic_v0_v1_fixture,
    load_reply_context_record,
    snapshot_context_refs,
)


UTC = timezone.utc
PLAN_ID = "daily-plan-test-001"
PAYLOAD_HASH = "a" * 64
TARGET_DATE = "2026-09-16"
SENDER = "a" * 12
BOT = "b" * 12


def _event(message_id, received_at, *, sender=SENDER, bot=BOT,
           conversation_type="one_to_one", context_token="NEW"):
    return InboundEvent(
        "default", message_id, sender, received_at.isoformat(), "probe", bot,
        conversation_type, context_token, "recipient", False,
    )


class SingleShotV1ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = [datetime(2026, 9, 16, 10, 0, tzinfo=UTC)]
        self.health_path = self.root / "session-health.json"
        self.health_store = SessionHealthStore(self.health_path)
        self.manager = SessionHealthManager(
            self.health_store,
            account_fingerprint=SENDER,
            bot_fingerprint=BOT,
            credential_fingerprint="c" * 12,
            credential_loader=lambda: "TEST_CREDENTIAL",
            binding_loader=lambda: "TEST_BINDING",
            probe=None,
            clock=lambda: self.clock[0],
        )
        self.manager.observe_send_result(
            SimpleNamespace(
                state="FAILED", http_status=200, ret=-2, errcode=None,
                errmsg="prepare failed", response_keys=("errmsg", "ret"),
            ),
            self.clock[0],
        )
        self.assertEqual(self.health_store.load().state, OUTBOUND_BLOCKED)
        self.contexts = {
            "old-ref": {
                "context_ref": "old-ref", "account_scope": "default",
                "message_id": "old", "sender_fingerprint": SENDER,
                "bot_fingerprint": BOT, "context_token": "OLD",
                "created_at": self.clock[0] - timedelta(seconds=1),
                "expires_at": self.clock[0] + timedelta(seconds=299),
            },
            "new-ref": {
                "context_ref": "new-ref", "account_scope": "default",
                "message_id": "new", "sender_fingerprint": SENDER,
                "bot_fingerprint": BOT, "context_token": "NEW",
                "created_at": self.clock[0] + timedelta(seconds=1),
                "expires_at": self.clock[0] + timedelta(seconds=301),
            },
        }
        self.static = StaticPreflight(
            PLAN_ID, PAYLOAD_HASH, TARGET_DATE,
            exact_plan_valid=True, ledger_eligible=True,
            credential_valid=True, binding_valid=True,
            identity_match=True, receive_health_acceptable=True,
            request_fixture_validated=True, operator_authorized=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def flow(self, *, existing=()):
        flow = SingleShotV1ProbeFlow(
            static_preflight=self.static,
            health_manager=self.manager,
            runner_started_at=self.clock[0],
            expected_sender_fingerprint=SENDER,
            expected_bot_fingerprint=BOT,
            context_loader=lambda ref: self.contexts[ref],
            existing_context_refs=frozenset(existing),
            now=lambda: self.clock[0],
        )
        self.assertEqual(flow.prepare_static(), "PASS")
        return flow

    def accept_new(self, flow, *, age=None, event_id="new"):
        if age is not None:
            created = self.clock[0] - timedelta(seconds=age)
            self.contexts["new-ref"] = {
                **self.contexts["new-ref"],
                "message_id": event_id,
                "created_at": created,
                "expires_at": created + timedelta(seconds=299),
            }
        event = _event(event_id, self.clock[0] + timedelta(seconds=2))
        return flow.accept_inbound(
            event, "new-ref", received_at=self.clock[0] + timedelta(seconds=2),
        )

    def test_old_context_present_before_runner_is_ignored(self):
        flow = self.flow(existing={"old-ref"})
        result = flow.accept_inbound(
            _event("old", self.clock[0] + timedelta(seconds=2)), "old-ref",
            received_at=self.clock[0] + timedelta(seconds=2),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "OLD_CONTEXT_IGNORED")
        self.assertEqual(flow.state, "WAITING")

    def test_new_inbound_after_waiting_binds_exact_context_ref(self):
        flow = self.flow(existing={"old-ref"})
        result = self.accept_new(flow)
        self.assertTrue(result.accepted)
        self.assertEqual(result.context_ref, "new-ref")
        self.assertEqual(flow.authorization.context_ref, "new-ref")
        self.assertEqual(flow.authorization.event_id, "new")

    def test_unrelated_sender_is_rejected_without_binding(self):
        flow = self.flow()
        result = flow.accept_inbound(
            _event("other", self.clock[0] + timedelta(seconds=2), sender="other"),
            "new-ref", received_at=self.clock[0] + timedelta(seconds=2),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "SENDER_MISMATCH")
        self.assertEqual(flow.state, "WAITING")

    def test_group_or_forwarded_event_is_rejected(self):
        flow = self.flow()
        result = flow.accept_inbound(
            _event("group", self.clock[0] + timedelta(seconds=2), conversation_type="group"),
            "new-ref", received_at=self.clock[0] + timedelta(seconds=2),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "DIRECT_MESSAGE_REQUIRED")
        self.assertEqual(flow.state, "WAITING")

    def test_context_age_under_30_is_eligible(self):
        flow = self.flow()
        result = self.accept_new(flow, age=10)
        self.assertTrue(result.accepted)
        self.assertLess(result.age_seconds, 30)

    def test_context_age_30_or_more_cannot_send(self):
        flow = self.flow()
        result = self.accept_new(flow, age=30)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "CONTEXT_TOO_OLD")
        self.assertEqual(flow.network_send_count, 0)

    def test_v0_v1_fixture_has_only_context_difference(self):
        fixture = build_synthetic_v0_v1_fixture()
        self.assertTrue(fixture.passed)
        self.assertEqual(fixture.only_diff, "context_token")
        self.assertEqual(fixture.other_diffs, ())

    def test_one_network_call_maximum_and_second_send_is_noop(self):
        flow = self.flow()
        self.assertTrue(self.accept_new(flow).accepted)
        pair = flow.build_v1_request(
            token="TEST_TOKEN", recipient="TEST_RECIPIENT", text="test",
            idempotency_key="probe", bot_agent="ilink_receive_probe/phase16c",
            x_wechat_uin="SYNTHETIC_UIN",
        )
        calls = []
        ack = SimpleNamespace(state="ACKED_API", http_status=200, ret=0,
                              errcode=None, errmsg=None, response_keys=("ret",))
        self.assertTrue(flow.send_once(lambda request: calls.append(request) or ack).submitted)
        replay = flow.send_once(lambda request: calls.append(request) or ack)
        self.assertFalse(replay.submitted)
        self.assertTrue(replay.duplicate)
        self.assertEqual(len(calls), 1)
        self.assertEqual(flow.network_send_count, 1)
        self.assertEqual(pair.only_diff, "context_token")

    def test_second_inbound_cannot_cause_second_send(self):
        flow = self.flow()
        self.assertTrue(self.accept_new(flow).accepted)
        second = flow.accept_inbound(
            _event("second", self.clock[0] + timedelta(seconds=3)), "new-ref",
            received_at=self.clock[0] + timedelta(seconds=3),
        )
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "INBOUND_ALREADY_BOUND")

    def test_failure_stays_outbound_blocked(self):
        flow = self.flow()
        self.assertTrue(self.accept_new(flow).accepted)
        flow.build_v1_request(
            token="TEST_TOKEN", recipient="TEST_RECIPIENT", text="test",
            idempotency_key="probe", bot_agent="ilink_receive_probe/phase16c",
            x_wechat_uin="SYNTHETIC_UIN",
        )
        failed = SimpleNamespace(state="FAILED", http_status=200, ret=-2,
                                 errcode=None, errmsg="prepare failed",
                                 response_keys=("errmsg", "ret"))
        outcome = flow.send_once(lambda request: failed)
        self.assertTrue(outcome.submitted)
        self.assertEqual(outcome.state, "FAILED")
        self.assertEqual(self.health_store.load().state, OUTBOUND_BLOCKED)

    def test_matching_ack_clears_only_controlled_probe_state(self):
        flow = self.flow()
        self.assertTrue(self.accept_new(flow).accepted)
        flow.build_v1_request(
            token="TEST_TOKEN", recipient="TEST_RECIPIENT", text="test",
            idempotency_key="probe", bot_agent="ilink_receive_probe/phase16c",
            x_wechat_uin="SYNTHETIC_UIN",
        )
        ack = SimpleNamespace(state="ACKED_API", http_status=200, ret=0,
                              errcode=None, errmsg=None, response_keys=("ret",))
        outcome = flow.send_once(lambda request: ack)
        self.assertEqual(outcome.state, "ACKED")
        self.assertEqual(outcome.health_state, "HEALTHY")
        self.assertEqual(self.health_store.load().state, "HEALTHY")

    def test_context_bound_loader_helpers_use_ref_without_logging(self):
        # Exercise the exact-ref shape used by the real DPAPI store with a
        # deterministic in-memory protector; no token is written in plaintext.
        context_root = self.root / "reply-context"
        calls = []

        def protect(data, unprotect=False):
            return data if unprotect else b"P:" + data

        store = ReplyContextStore(self.root, ttl_seconds=300, now=lambda: 0,
                                  protect=protect,
                                  unprotect=lambda data: data[2:])
        event = _event("stored", datetime.fromtimestamp(1, UTC), context_token="CTX")
        ref = store.store(event)
        refs = snapshot_context_refs(store)
        self.assertIn(ref, refs)
        record = load_reply_context_record(store, ref)
        self.assertEqual(record["context_ref"], ref)
        self.assertEqual(record["message_id"], "stored")
        self.assertEqual(record["context_token"], "CTX")
        self.assertTrue(context_root.exists())


if __name__ == "__main__":
    unittest.main()
