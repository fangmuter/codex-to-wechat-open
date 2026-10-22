import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from ilink_receive_probe.probe.proactive_send import IlinkResponseDiagnostics
from ilink_receive_probe.probe.real_transport import RealIlinkTransport
from ilink_receive_probe.probe.session_health import (
    DEGRADED,
    HEALTHY,
    OUTBOUND_BLOCKED,
    OUTBOUND_PREPARE_FAILED,
    REAUTH_REQUIRED,
    HealthPreflightResult,
    OutboundProbeAuthorization,
    ReceiveHealthResult,
    SessionErrorClass,
    SessionHealthManager,
    SessionHealthStore,
    SessionHealthState,
    classify_response,
)


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class SessionHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

    def manager(self, *, probe=None, freshness=300, loader=lambda: "secret"):
        now = [self.now]
        manager = SessionHealthManager(
            SessionHealthStore(Path(self.temp.name) / "session-health.json"),
            account_fingerprint="a" * 12,
            bot_fingerprint="b" * 12,
            credential_fingerprint="c" * 12,
            credential_loader=loader,
            binding_loader=lambda: "raw-recipient",
            probe=probe,
            freshness_seconds=freshness,
            clock=lambda: now[0],
        )
        return manager, now

    def test_prepare_failed_is_outbound_failure_not_reauth(self):
        diagnostics = IlinkResponseDiagnostics(
            http_status=200, ret=-2, errmsg="prepare failed",
            response_keys=("errmsg", "ret"),
        )
        self.assertEqual(classify_response(diagnostics), OUTBOUND_PREPARE_FAILED)
        manager, _ = self.manager(probe=lambda: diagnostics)
        result = manager.preflight(self.now)
        self.assertFalse(result.allowed)
        self.assertEqual(result.state, OUTBOUND_BLOCKED)
        self.assertEqual(result.error_class, OUTBOUND_PREPARE_FAILED)
        self.assertEqual(result.outcome, "BLOCKED_OUTBOUND_PREPARE_FAILED")
        self.assertNotEqual(result.state, REAUTH_REQUIRED)

    def test_auth_invalid_still_requires_reauth(self):
        diagnostics = IlinkResponseDiagnostics(
            http_status=200, ret=-14, response_keys=("ret",),
        )
        self.assertEqual(
            classify_response(diagnostics), SessionErrorClass.AUTH_INVALID.value,
        )
        manager, _ = self.manager(probe=lambda: diagnostics)
        result = manager.preflight(self.now)
        self.assertFalse(result.allowed)
        self.assertEqual(result.state, REAUTH_REQUIRED)
        self.assertEqual(result.error_class, SessionErrorClass.AUTH_INVALID.value)

    def test_response_classifier_keeps_ret_and_errcode_semantics_separate(self):
        self.assertEqual(classify_response(IlinkResponseDiagnostics(
            http_status=200, ret=0, response_keys=("ret",))), "NONE")
        self.assertEqual(classify_response(IlinkResponseDiagnostics(
            http_status=200, ret=-2, response_keys=("ret",))),
                         SessionErrorClass.REMOTE_REJECT.value)
        self.assertEqual(classify_response(IlinkResponseDiagnostics(
            http_status=200, errcode=-2, response_keys=("errcode",))),
                         SessionErrorClass.REMOTE_REJECT.value)
        self.assertEqual(classify_response(IlinkResponseDiagnostics(
            http_status=200, ret=-2, errmsg="prepare failed",
            response_keys=("errmsg", "ret"))),
                         OUTBOUND_PREPARE_FAILED)
        self.assertEqual(classify_response(
            exception=TimeoutError("long poll")),
            SessionErrorClass.NETWORK_TRANSIENT.value)

    def test_getupdates_success_without_ret_is_healthy(self):
        diagnostics = IlinkResponseDiagnostics(
            http_status=200, response_keys=("get_updates_buf", "msgs", "sync_buf"),
        )
        manager, _ = self.manager(
            probe=lambda: ReceiveHealthResult(diagnostics, message_count=0, cursor_present=True),
        )
        result = manager.preflight(self.now)
        self.assertTrue(result.allowed)
        self.assertEqual(result.state, HEALTHY)
        payload = json.loads((Path(self.temp.name) / "session-health.json").read_text())
        self.assertEqual(payload["state"], HEALTHY)
        self.assertNotIn("secret", json.dumps(payload))
        self.assertNotIn("raw-recipient", json.dumps(payload))

    def test_receive_success_persists_last_inbound_timestamp(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        self.assertTrue(manager.preflight(self.now).allowed)
        observation = manager.last_observation
        self.assertIsNotNone(observation.last_inbound_success_at)
        self.assertEqual(observation.last_inbound_success_at, observation.checked_at)

    def test_proactive_ack_persists_last_proactive_without_fake_inbound(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        result = SimpleNamespace(state="ACKED_API")
        manager.observe_send_result(result, self.now)
        observation = manager.last_observation
        self.assertEqual(observation.last_proactive_success_at, observation.checked_at)
        self.assertIsNone(observation.last_inbound_success_at)

    def test_prepare_failure_diagnostic_survives_store_reopen_without_reauth(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        manager.observe_send_result(SimpleNamespace(
            state="FAILED", http_status=200, ret=-2,
            errmsg="prepare failed", response_keys=("ret", "errmsg"),
        ), self.now)
        reopened = SessionHealthStore(Path(self.temp.name) / "session-health.json").load()
        self.assertEqual(reopened.state, OUTBOUND_BLOCKED)
        self.assertEqual(reopened.error_class, OUTBOUND_PREPARE_FAILED)
        self.assertEqual(reopened.first_prepare_failed_at, reopened.checked_at)
        calls = []
        restarted = SessionHealthManager(
            SessionHealthStore(Path(self.temp.name) / "session-health.json"),
            account_fingerprint="a" * 12,
            bot_fingerprint="b" * 12,
            credential_fingerprint="c" * 12,
            credential_loader=lambda: "secret",
            binding_loader=lambda: "raw-recipient",
            probe=lambda: calls.append(True),
            clock=lambda: self.now,
        )
        blocked = restarted.preflight(self.now)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.state, OUTBOUND_BLOCKED)
        self.assertEqual(blocked.error_class, OUTBOUND_PREPARE_FAILED)
        self.assertEqual(blocked.reason, "outbound_prepare_failed_latched")
        self.assertEqual(calls, [])

    def test_legacy_prepare_failure_latch_is_normalized_on_restart(self):
        path = Path(self.temp.name) / "session-health.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "state": REAUTH_REQUIRED,
            "error_class": SessionErrorClass.SESSION_PREPARE_FAILED.value,
            "checked_at": self.now.isoformat(),
            "http_status": 200,
            "ret": -2,
            "errcode": None,
            "errmsg": "prepare failed",
            "response_keys": ["errmsg", "ret"],
            "reason": "proactive_send_failed",
            "account_fingerprint": "a" * 12,
            "bot_fingerprint": "b" * 12,
            "credential_fingerprint": "c" * 12,
        }), encoding="utf-8")
        observation = SessionHealthStore(path).load()
        self.assertEqual(observation.state, OUTBOUND_BLOCKED)
        self.assertEqual(observation.error_class, OUTBOUND_PREPARE_FAILED)
        # Loading performs an in-memory compatibility migration only; it does
        # not rewrite the production state file as a side effect.
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], REAUTH_REQUIRED)

    def test_health_store_redacts_context_and_credentials_from_errmsg(self):
        store = SessionHealthStore(Path(self.temp.name) / "session-health.json")
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, ret=-2,
            errmsg="context_token=SECRET credential=SECRET",
            response_keys=("errmsg", "ret"),
        ))
        result = manager.preflight(self.now)
        self.assertFalse(result.allowed)
        raw = (Path(self.temp.name) / "session-health.json").read_text()
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("context_token=SECRET", raw)
        self.assertNotIn("credential=SECRET", raw)

    def test_network_probe_is_degraded_and_not_reauth(self):
        manager, _ = self.manager(probe=lambda: (_ for _ in ()).throw(URLError("offline")))
        result = manager.preflight(self.now)
        self.assertFalse(result.allowed)
        self.assertEqual(result.state, DEGRADED)
        self.assertEqual(result.error_class, SessionErrorClass.NETWORK_TRANSIENT.value)

    def test_identity_failure_blocks_without_probe(self):
        called = []
        manager, _ = self.manager(probe=lambda: called.append(True))
        manager.bot_fingerprint = None
        result = manager.preflight(self.now)
        self.assertFalse(result.allowed)
        self.assertEqual(result.state, REAUTH_REQUIRED)
        self.assertEqual(called, [])

    def test_stale_healthy_state_runs_probe_again(self):
        store = SessionHealthStore(Path(self.temp.name) / "session-health.json")
        first_probe = lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        )
        manager, clock = self.manager(probe=first_probe, freshness=60)
        self.assertTrue(manager.preflight(self.now).allowed)
        clock[0] = self.now + timedelta(seconds=61)
        calls = []
        manager.probe = lambda: (calls.append(True) or IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        self.assertTrue(manager.preflight(clock[0]).allowed)
        self.assertEqual(calls, [True])
        self.assertIsNotNone(store.load())

    def test_fresh_healthy_state_skips_remote_probe(self):
        calls = []
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        self.assertTrue(manager.preflight(self.now).allowed)
        manager.probe = lambda: calls.append(True)
        self.assertTrue(manager.preflight(self.now + timedelta(seconds=1)).allowed)
        self.assertEqual(calls, [])

    def test_fresh_health_from_old_identity_is_not_reused(self):
        manager, clock = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        self.assertTrue(manager.preflight(self.now).allowed)
        manager.bot_fingerprint = "d" * 12
        calls = []
        manager.probe = lambda: calls.append(True) or IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        )
        self.assertTrue(manager.preflight(self.now + timedelta(seconds=1)).allowed)
        self.assertEqual(calls, [True])

    def test_send_prepare_failure_transitions_to_outbound_block(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        result = SimpleNamespace(
            state="FAILED", http_status=200, ret=-2, errcode=None,
            errmsg="prepare failed", response_keys=("errmsg", "ret"),
        )
        observed = manager.observe_send_result(result, self.now)
        self.assertEqual(observed.state, OUTBOUND_BLOCKED)
        self.assertEqual(manager.last_observation.error_class,
                         OUTBOUND_PREPARE_FAILED)
        self.assertNotEqual(observed.state, REAUTH_REQUIRED)

    def test_outbound_latch_cannot_be_cleared_by_receive_probe(self):
        calls = []
        manager, _ = self.manager(probe=lambda: calls.append(True) or IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        failed = SimpleNamespace(
            state="FAILED", http_status=200, ret=-2, errcode=None,
            errmsg="prepare failed", response_keys=("errmsg", "ret"),
        )
        manager.observe_send_result(failed, self.now)
        blocked = manager.preflight(self.now)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.state, OUTBOUND_BLOCKED)
        self.assertEqual(blocked.error_class, OUTBOUND_PREPARE_FAILED)
        self.assertEqual(blocked.reason, "outbound_prepare_failed_latched")
        self.assertEqual(blocked.outcome, "BLOCKED_OUTBOUND_PREPARE_FAILED")
        self.assertEqual(calls, [])
        # Generic staged validation (the path used by receive-only re-auth)
        # cannot clear an outbound latch.
        staged = manager.mark_staged_validation(self.now)
        self.assertFalse(staged.allowed)
        self.assertEqual(staged.reason, "outbound_recovery_required")
        self.assertFalse(manager.preflight(self.now).allowed)
        self.assertEqual(calls, [])

    def test_direct_mark_healthy_cannot_clear_outbound_latch(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        failed = SimpleNamespace(
            state="FAILED", http_status=200, ret=-2, errcode=None,
            errmsg="prepare failed", response_keys=("errmsg", "ret"),
        )
        manager.observe_send_result(failed, self.now)
        direct = manager.mark_healthy(self.now, reason="operator_called_me")
        self.assertFalse(direct.allowed)
        self.assertEqual(direct.state, OUTBOUND_BLOCKED)
        self.assertEqual(direct.reason, "outbound_recovery_required")
        self.assertEqual(manager.last_observation.state, OUTBOUND_BLOCKED)

    def test_outbound_ack_requires_exact_inflight_authorization(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        armed = self._arm_outbound_recovery(manager)
        authorization = armed.authorization
        allowed = manager.preflight_controlled_probe(
            self.now,
            plan_id="recovery-plan",
            payload_hash="a" * 64,
            authorization=authorization,
        )
        self.assertTrue(allowed.allowed)
        missing = manager.observe_send_result(
            SimpleNamespace(state="ACKED_API"), self.now,
        )
        self.assertFalse(missing.allowed)
        self.assertEqual(missing.state, OUTBOUND_BLOCKED)
        self.assertEqual(missing.reason, "outbound_controlled_probe_required")
        # The exact in-flight authorization is required to record the ACK and
        # clear the latch; a replay after that transition cannot clear again.
        acknowledged = manager.observe_send_result(
            SimpleNamespace(state="ACKED_API"), self.now,
            authorization=authorization,
        )
        self.assertTrue(acknowledged.allowed)
        self.assertEqual(manager.last_observation.state, HEALTHY)
        replay = manager.observe_send_result(
            SimpleNamespace(state="ACKED_API"), self.now,
            authorization=authorization,
        )
        self.assertTrue(replay.allowed)

    def test_restart_cannot_use_inflight_outbound_authorization(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        armed = self._arm_outbound_recovery(manager)
        authorization = armed.authorization
        self.assertTrue(manager.preflight_controlled_probe(
            self.now,
            plan_id="recovery-plan",
            payload_hash="a" * 64,
            authorization=authorization,
        ).allowed)
        restarted = SessionHealthManager(
            SessionHealthStore(Path(self.temp.name) / "session-health.json"),
            account_fingerprint="a" * 12,
            bot_fingerprint="b" * 12,
            credential_fingerprint="c" * 12,
            credential_loader=lambda: "secret",
            binding_loader=lambda: "raw-recipient",
            probe=lambda: (_ for _ in ()).throw(AssertionError("no receive probe")),
            clock=lambda: self.now,
        )
        blocked = restarted.observe_send_result(
            SimpleNamespace(state="ACKED_API"), self.now,
            authorization=authorization,
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.state, OUTBOUND_BLOCKED)
        self.assertEqual(blocked.reason, "outbound_controlled_probe_required")

    def _arm_outbound_recovery(self, manager):
        failed = SimpleNamespace(
            state="FAILED", http_status=200, ret=-2, errcode=None,
            errmsg="prepare failed", response_keys=("errmsg", "ret"),
        )
        manager.observe_send_result(failed, self.now)
        return manager.mark_outbound_staged_validation(
            self.now,
            plan_id="recovery-plan",
            payload_hash="a" * 64,
            request_fixture_validated=True,
            credential_readable=True,
            binding_valid=True,
            identity_match=True,
            receive_health_acceptable=True,
            operator_authorized=True,
        )

    def test_outbound_staged_recovery_requires_all_operator_evidence(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        failed = SimpleNamespace(
            state="FAILED", http_status=200, ret=-2, errcode=None,
            errmsg="prepare failed", response_keys=("errmsg", "ret"),
        )
        manager.observe_send_result(failed, self.now)
        blocked = manager.mark_outbound_staged_validation(
            self.now,
            plan_id="recovery-plan",
            payload_hash="a" * 64,
            request_fixture_validated=True,
            credential_readable=True,
            binding_valid=True,
            identity_match=True,
            receive_health_acceptable=True,
            operator_authorized=False,
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.state, OUTBOUND_BLOCKED)
        self.assertEqual(blocked.reason, "outbound_staged_validation_requirements_missing")
        self.assertIsNone(blocked.authorization)

    def test_outbound_staged_recovery_arms_one_exact_controlled_probe(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        armed = self._arm_outbound_recovery(manager)
        self.assertFalse(armed.allowed)
        self.assertEqual(armed.state, OUTBOUND_BLOCKED)
        self.assertIsInstance(armed.authorization, OutboundProbeAuthorization)
        authorization = armed.authorization
        # Normal receive preflight and the generic finalize hook remain blocked.
        self.assertFalse(manager.preflight(self.now).allowed)
        self.assertFalse(manager.mark_staged_validation(self.now).allowed)
        allowed = manager.preflight_controlled_probe(
            self.now,
            plan_id="recovery-plan",
            payload_hash="a" * 64,
            authorization=authorization,
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.reason, "outbound_controlled_probe_authorized")
        self.assertEqual(allowed.authorization, authorization)
        # The in-memory arm is one-use; a second call is fail-closed.
        replay = manager.preflight_controlled_probe(
            self.now,
            plan_id="recovery-plan",
            payload_hash="a" * 64,
            authorization=authorization,
        )
        self.assertFalse(replay.allowed)
        self.assertEqual(replay.state, OUTBOUND_BLOCKED)
        self.assertEqual(replay.reason, "outbound_probe_authorization_missing")

    def test_outbound_staged_recovery_is_exact_plan_and_hash_bound(self):
        manager, _ = self.manager(probe=lambda: IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        ))
        armed = self._arm_outbound_recovery(manager)
        authorization = armed.authorization
        wrong_plan = manager.preflight_controlled_probe(
            self.now,
            plan_id="other-plan",
            payload_hash="a" * 64,
            authorization=authorization,
        )
        self.assertFalse(wrong_plan.allowed)
        self.assertEqual(wrong_plan.state, OUTBOUND_BLOCKED)
        right = manager.preflight_controlled_probe(
            self.now,
            plan_id="recovery-plan",
            payload_hash="a" * 64,
            authorization=authorization,
        )
        self.assertFalse(right.allowed)
        # A mismatched attempt consumes and invalidates the arm; no fallback.
        self.assertEqual(right.reason, "outbound_probe_authorization_missing")

    def test_unknown_remote_health_is_distinct_from_network_degraded(self):
        diagnostics = IlinkResponseDiagnostics(http_status=200, ret=-7,
                                                response_keys=("ret",))
        manager, _ = self.manager(probe=lambda: diagnostics)
        result = manager.preflight(self.now)
        self.assertFalse(result.allowed)
        self.assertEqual(result.state, SessionHealthState.UNKNOWN.value)
        self.assertEqual(result.outcome, "BLOCKED_SESSION_UNKNOWN")

    def test_receive_probe_identity_mismatch_requires_reauth(self):
        diagnostics = IlinkResponseDiagnostics(
            http_status=200, response_keys=("msgs", "get_updates_buf"),
        )
        manager, _ = self.manager(probe=lambda: ReceiveHealthResult(
            diagnostics, message_count=1, cursor_present=True,
            account_fingerprint="different", bot_fingerprint="b" * 12,
        ))
        result = manager.preflight(self.now)
        self.assertFalse(result.allowed)
        self.assertEqual(result.state, REAUTH_REQUIRED)
        self.assertEqual(result.reason, "receive_probe_identity_mismatch")

    def test_real_transport_diagnostic_probe_is_bounded(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return _Response({"msgs": [], "get_updates_buf": "cursor", "sync_buf": "sync"})

        transport = RealIlinkTransport(
            token="secret", base_url="https://example.invalid", opener=opener,
        )
        result = transport.getupdates_with_diagnostics("old")
        self.assertEqual(result.message_count, 0)
        self.assertTrue(result.cursor_present)
        self.assertEqual(result.diagnostics.http_status, 200)
        self.assertEqual(result.diagnostics.response_keys,
                         ("get_updates_buf", "msgs", "sync_buf"))
        self.assertEqual(calls[0][1], 45.0)

    def test_real_transport_network_exception_is_not_reported_as_success(self):
        def opener(_request, timeout):
            raise URLError("offline")

        transport = RealIlinkTransport(
            token="secret", base_url="https://example.invalid", opener=opener,
        )
        result = transport.getupdates_with_diagnostics("old")
        self.assertIsInstance(result.exception, URLError)
        self.assertEqual(result.diagnostics.errmsg, "URLError")
        self.assertEqual(result.message_count, 0)


class HealthPreflightContractTests(unittest.TestCase):
    def test_result_contract_maps_state_to_safe_outcome(self):
        now = datetime(2026, 9, 12, tzinfo=timezone.utc).isoformat()
        self.assertEqual(
            HealthPreflightResult(False, REAUTH_REQUIRED, "AUTH_INVALID", now).outcome,
            "BLOCKED_REAUTH_REQUIRED",
        )
        self.assertEqual(
            HealthPreflightResult(False, DEGRADED, "NETWORK_TRANSIENT", now).outcome,
            "BLOCKED_SESSION_DEGRADED",
        )
        self.assertEqual(
            HealthPreflightResult(True, HEALTHY, None, now).outcome,
            "HEALTHY",
        )


if __name__ == "__main__":
    unittest.main()
