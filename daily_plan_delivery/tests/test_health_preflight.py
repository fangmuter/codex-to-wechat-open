import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daily_plan_delivery import DeliveryInbox, DeliveryLedger, DeliveryService
from daily_plan_delivery.schema import validate_delivery_payload
from daily_plan_delivery.service import DELIVERY_TZ
from daily_plan_production.delivery_runtime import run_delivery_production
from daily_plan_production.paths import ProductionPaths
from ilink_receive_probe.probe.session_health import (
    DEGRADED,
    HEALTHY,
    OUTBOUND_BLOCKED,
    OUTBOUND_PREPARE_FAILED,
    REAUTH_REQUIRED,
    HealthPreflightResult,
    SessionHealthManager,
    SessionHealthStore,
)
from ilink_receive_probe.probe.proactive_send import IlinkResponseDiagnostics


def payload(plan_id="health-plan", target_date="2026-09-13"):
    return {
        "schema_version": 1,
        "plan_id": plan_id,
        "target_date": target_date,
        "source": "chatgpt_web",
        "status": "CONFIRMED",
        "created_at": "2026-09-12T10:00:00+08:00",
        "items": [{
            "item_id": "item-1", "source": "SYSTEM_RECOMMENDED",
            "title": "健康门控测试", "minutes": 30,
            "time_hint": None, "reason": "测试",
        }],
        "fixed_constraints": [],
        "execution_order": ["item-1"],
        "buffer_minutes": 15,
        "warnings": [],
    }


class FakeTransport:
    def __init__(self, state="ACKED_API"):
        self.state = state
        self.calls = []

    def send(self, text, key, now):
        self.calls.append((text, key, now))
        return SimpleNamespace(state=self.state)


class FakeHealth:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.observed = []
        self.last_observation = None
        self.last_transition = None

    def preflight(self, now):
        self.calls.append(now)
        result = self.outcomes.pop(0) if self.outcomes else self.outcomes[-1]
        self.last_observation = SimpleNamespace(
            state=result.state, error_class=result.error_class,
            checked_at=result.checked_at, age_seconds=result.age_seconds,
            bot_fingerprint=None, account_fingerprint=None,
            credential_fingerprint=None, http_status=result.http_status,
            ret=result.ret, errcode=result.errcode, errmsg=result.errmsg,
            response_keys=result.response_keys,
        )
        self.last_transition = "TEST"
        return result

    def observe_send_result(self, result, now):
        self.observed.append((result, now))
        return result


class HealthPreflightDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.inbox = root / "DailyPlanDeliveryInbox"
        self.inbox.mkdir()
        self.paths = ProductionPaths(
            root / "production" / "daily-plan", delivery_inbox_dir=self.inbox,
        )
        self.start = datetime(2026, 9, 12, 22, 0, tzinfo=DELIVERY_TZ)
        (self.inbox / "DAILY_PLAN_DELIVERY_health-plan.json").write_text(
            json.dumps(payload(), ensure_ascii=False), encoding="utf-8",
        )

    @staticmethod
    def healthy():
        return HealthPreflightResult(True, HEALTHY, None, "2026-09-12T22:00:00+08:00")

    @staticmethod
    def blocked(state, error):
        return HealthPreflightResult(False, state, error, "2026-09-12T22:00:00+08:00")

    def test_reauth_required_blocks_before_plan_ledger_begin(self):
        health = FakeHealth([self.blocked(REAUTH_REQUIRED, "SESSION_PREPARE_FAILED")])
        transport = FakeTransport()
        outcome = run_delivery_production(
            1, paths=self.paths, transport=transport, token="test-token",
            health_manager=health, now=self.start,
        )
        self.assertEqual(outcome, "WECHAT_REAUTH_REQUIRED")
        self.assertEqual(transport.calls, [])
        ledger = DeliveryLedger(self.paths.plan_delivery_ledger_db)
        self.addCleanup(ledger.close)
        self.assertIsNone(ledger.get("health-plan"))
        self.assertFalse(self.paths.lock_file.exists())
        self.assertFalse(self.paths.lock_file.exists())

    def test_network_degraded_waits_then_sends_without_consuming_attempt(self):
        health = FakeHealth([
            self.blocked(DEGRADED, "NETWORK_TRANSIENT"),
            self.healthy(),
        ])
        transport = FakeTransport()
        current = [self.start]

        def clock():
            return current[0]

        def sleep(seconds):
            current[0] += timedelta(seconds=seconds)

        outcome = run_delivery_production(
            1, poll_seconds=60, paths=self.paths, transport=transport,
            token="test-token", health_manager=health, now=self.start,
            clock=clock, sleep=sleep,
        )
        self.assertEqual(outcome, "DELIVERED")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(health.calls), 2)

    def test_already_delivered_noop_does_not_need_health_probe(self):
        first_health = FakeHealth([self.healthy()])
        first_transport = FakeTransport()
        self.assertEqual(run_delivery_production(
            1, paths=self.paths, transport=first_transport, token="test-token",
            health_manager=first_health, now=self.start,
        ), "DELIVERED")

        class MustNotProbe(FakeHealth):
            def preflight(self, now):
                raise AssertionError("NOOP should not run health preflight")

        second = MustNotProbe([])
        second_transport = FakeTransport()
        self.assertEqual(run_delivery_production(
            1, paths=self.paths, transport=second_transport, token="test-token",
            health_manager=second, now=self.start,
        ), "NOOP")
        self.assertEqual(second_transport.calls, [])

    def test_service_hook_preserves_deterministic_plan_validation(self):
        ledger = DeliveryLedger(Path(self.temp.name) / "service-ledger.db")
        self.addCleanup(ledger.close)
        calls = []
        service = DeliveryService(
            DeliveryInbox(self.inbox), ledger, FakeTransport(),
            preflight=lambda now, plan, digest: (
                calls.append((plan.plan_id, digest)) or self.healthy()
            ),
        )
        result = service.deliver_tomorrow(self.start)
        self.assertEqual(result.status, "DELIVERED")
        self.assertEqual(calls[0][0], "health-plan")
        self.assertEqual(calls[0][1], result.payload_hash)

    def test_authorized_failed_retry_is_health_gated_before_attempt_begin(self):
        first = FakeTransport("FAILED")
        self.assertEqual(run_delivery_production(
            1, paths=self.paths, transport=first, token="test-token",
            health_manager=FakeHealth([self.healthy()]), now=self.start,
        ), "FAILED")
        ledger = DeliveryLedger(self.paths.plan_delivery_ledger_db)
        self.addCleanup(ledger.close)
        before = len(ledger.attempts("health-plan"))
        blocked = FakeHealth([self.blocked(REAUTH_REQUIRED, "SESSION_PREPARE_FAILED")])
        second = FakeTransport()
        digest = ledger.get("health-plan")["payload_hash"]
        outcome = run_delivery_production(
            1, paths=self.paths, transport=second, token="test-token",
            health_manager=blocked, now=self.start,
            authorized_retry_plan_id="health-plan",
            authorized_retry_hash=digest,
            authorized_retry_target_date="2026-09-13",
        )
        self.assertEqual(outcome, "WECHAT_REAUTH_REQUIRED")
        self.assertEqual(second.calls, [])
        self.assertEqual(len(ledger.attempts("health-plan")), before)
        self.assertEqual(ledger.get("health-plan")["delivery_result"], "FAILED")

    def test_prepare_failed_stays_failed_and_does_not_auto_retry_or_reauth(self):
        class PrepareFailedTransport(FakeTransport):
            def send(self, text, key, now, **_kwargs):
                self.calls.append((text, key, now))
                return SimpleNamespace(
                    state="FAILED", http_status=200, ret=-2, errcode=None,
                    errmsg="prepare failed", response_keys=("errmsg", "ret"),
                )

        health = SessionHealthManager(
            SessionHealthStore(self.paths.session_health_file),
            account_fingerprint="a" * 12,
            bot_fingerprint="b" * 12,
            credential_fingerprint="c" * 12,
            credential_loader=lambda: "test-token",
            binding_loader=lambda: "binding-only-in-memory",
            probe=lambda: IlinkResponseDiagnostics(
                http_status=200, response_keys=("msgs", "get_updates_buf"),
            ),
        )
        first_transport = PrepareFailedTransport()
        first = run_delivery_production(
            1, paths=self.paths, transport=first_transport,
            token="test-token", health_manager=health, now=self.start,
        )
        self.assertEqual(first, "FAILED")
        self.assertEqual(len(first_transport.calls), 1)

        plan_ledger = DeliveryLedger(self.paths.plan_delivery_ledger_db)
        self.addCleanup(plan_ledger.close)
        row = plan_ledger.get("health-plan")
        self.assertEqual(row["delivery_result"], "FAILED")
        self.assertEqual(len(plan_ledger.attempts("health-plan")), 1)
        self.assertEqual(health.last_observation.state, OUTBOUND_BLOCKED)
        self.assertEqual(health.last_observation.error_class, OUTBOUND_PREPARE_FAILED)
        self.assertNotEqual(health.last_observation.state, REAUTH_REQUIRED)

        replay_transport = PrepareFailedTransport()
        replay = run_delivery_production(
            1, paths=self.paths, transport=replay_transport,
            token="test-token", health_manager=health, now=self.start,
            authorized_retry_plan_id="health-plan",
            authorized_retry_hash=row["payload_hash"],
            authorized_retry_target_date="2026-09-13",
        )
        self.assertEqual(replay, "OUTBOUND_PREPARE_FAILED")
        self.assertEqual(replay_transport.calls, [])
        self.assertEqual(len(plan_ledger.attempts("health-plan")), 1)
        self.assertEqual(health.last_observation.state, OUTBOUND_BLOCKED)
        self.assertEqual(health.last_observation.error_class, OUTBOUND_PREPARE_FAILED)

    def test_ambiguous_transport_is_observed_without_opening_retry(self):
        class RaisingTransport(FakeTransport):
            def send(self, text, key, now):
                self.calls.append((text, key, now))
                raise TimeoutError("network boundary")

        health = FakeHealth([self.healthy()])
        result = run_delivery_production(
            1, paths=self.paths, transport=RaisingTransport(), token="test-token",
            health_manager=health, now=self.start,
        )
        self.assertEqual(result, "UNKNOWN")
        self.assertEqual(len(health.observed), 1)
        self.assertEqual(health.observed[0][0].state, "UNKNOWN")

    def test_missing_real_credential_returns_reauth_without_ledger_attempt(self):
        # This exercises the installed (non-injected transport) construction
        # without contacting the network: a missing DPAPI load is handled by
        # the health gate before the bound sender can submit.
        isolated_state = Path(self.temp.name) / "isolated-ilink-state"
        with patch.dict(os.environ, {"ILINK_STATE_DIR": str(isolated_state)}):
            with patch(
                "daily_plan_production.delivery_runtime.CredentialStore.load",
                side_effect=RuntimeError("credential unavailable"),
            ):
                outcome = run_delivery_production(
                    1, paths=self.paths, now=self.start,
                )
        self.assertEqual(outcome, "WECHAT_REAUTH_REQUIRED")
        ledger = DeliveryLedger(self.paths.plan_delivery_ledger_db)
        self.addCleanup(ledger.close)
        self.assertIsNone(ledger.get("health-plan"))


if __name__ == "__main__":
    unittest.main()
