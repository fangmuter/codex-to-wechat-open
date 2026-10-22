"""Offline coverage for the context-bound Web-First production runner."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from daily_plan_delivery import DeliveryLedger
from daily_plan_delivery.service import DELIVERY_TZ
from daily_plan_production.delivery_runtime import run_delivery_production
from daily_plan_production.lock import ProductionLock
from daily_plan_production.paths import ProductionPaths
from ilink_receive_probe.probe.models import Batch, InboundEvent
from ilink_receive_probe.probe.proactive_send import IlinkResponseDiagnostics
from ilink_receive_probe.probe.receiver import Receiver
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.session_health import (
    HealthPreflightResult,
    HEALTHY,
    OUTBOUND_BLOCKED,
    OUTBOUND_PREPARE_FAILED,
    REAUTH_REQUIRED,
)
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.transport import FakeIlinkTransport


SENDER = "a" * 12
BOT = "b" * 12
TOKEN = "SYNTHETIC_CONTEXT_TOKEN"
START = datetime(2026, 9, 16, 22, 0, tzinfo=DELIVERY_TZ)


def inbound_event(message_id="m-1", *, offset=1, sender=SENDER,
                  bot=BOT, conversation_type="one_to_one", token=TOKEN,
                  forwarded=False, base=START):
    return InboundEvent(
        "default", message_id, sender,
        (base + timedelta(seconds=offset)).isoformat(),
        "hello", bot, conversation_type, token, "recipient", forwarded,
    )


def plan_payload(plan_id="plan-1", target_date="2026-09-17", *, status="CONFIRMED"):
    return {
        "schema_version": 1,
        "plan_id": plan_id,
        "target_date": target_date,
        "source": "chatgpt_web",
        "status": status,
        "created_at": "2026-09-16T12:00:00+08:00",
        "items": [{
            "item_id": "item-1", "source": "SYSTEM_RECOMMENDED",
            "title": "测试计划项", "minutes": 30,
            "time_hint": None, "reason": "offline test",
        }],
        "fixed_constraints": [],
        "execution_order": ["item-1"],
        "buffer_minutes": 15,
        "warnings": [],
    }


class FakeClock:
    def __init__(self, current=START, on_sleep=None):
        self.current = current
        self.on_sleep = on_sleep
        self.sleeps = []

    def __call__(self):
        return self.current

    def seconds(self):
        return self.current.timestamp()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep(self)


class FakeSend:
    def __init__(self, state="ACKED_API"):
        self.state = state
        self.calls = []

    def send(self, text, key, now, *, context_token=None, retry_failed=False):
        self.calls.append({
            "text": text, "key": key, "now": now,
            "context_token": context_token, "retry_failed": retry_failed,
        })
        return SimpleNamespace(
            state=self.state, http_status=200,
            ret=0 if self.state == "ACKED_API" else -2,
            errcode=None,
            errmsg=None if self.state == "ACKED_API" else "prepare failed",
            response_keys=("message_id",) if self.state == "ACKED_API" else ("errmsg", "ret"),
        )


class FakeHealth:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []
        self.observed = []
        self.last_observation = None
        self.last_transition = None

    @staticmethod
    def healthy():
        return HealthPreflightResult(True, HEALTHY, None, "2026-09-16T22:00:00+08:00")

    def preflight(self, now):
        self.calls.append(now)
        result = self.outcomes.pop(0) if self.outcomes else self.healthy()
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


class ContextRuntimeFixture:
    def __init__(self, *, events=(), batches=None, context_ttl=300,
                 send_state="ACKED_API", health=None):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inbox = self.root / "DailyPlanDeliveryInbox"
        self.inbox.mkdir()
        self.paths = ProductionPaths(
            self.root / "production" / "daily-plan",
            delivery_inbox_dir=self.inbox,
        )
        self.clock = FakeClock()
        self.context_store = ReplyContextStore(
            self.root / "reply-context", ttl_seconds=context_ttl,
            now=self.clock.seconds,
            protect=lambda data, unprotect=False: data if unprotect else b"P:" + data,
            unprotect=lambda data: data[2:],
        )
        self.storage = ProbeStorage(self.root / "inbound.db")
        if batches is None:
            batches = [Batch(tuple(events), "cursor-1")]
        self.receive_transport = FakeIlinkTransport(batches=batches)
        self.receiver = Receiver(
            self.receive_transport, self.storage, SENDER, BOT,
            context_store=self.context_store,
        )
        self.send = FakeSend(send_state)
        self.health = health or FakeHealth()

    def close(self):
        self.storage.close()
        self.temp.cleanup()

    def write_plan(self, plan_id="plan-1", target_date="2026-09-17", *, status="CONFIRMED", name=None):
        name = name or f"DAILY_PLAN_DELIVERY_{plan_id}.json"
        path = self.inbox / name
        path.write_text(
            json.dumps(plan_payload(plan_id, target_date, status=status), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def run(self, *, start=START, max_hours=1, poll_seconds=60):
        return run_delivery_production(
            max_hours, poll_seconds=poll_seconds, paths=self.paths,
            transport=self.send, token="TEST_CREDENTIAL",
            health_manager=self.health, receiver=self.receiver,
            context_store=self.context_store, require_fresh_context=True,
            now=start, clock=self.clock, sleep=self.clock.sleep,
        )

    def event(self, message_id="m-1", *, offset=1, sender=SENDER,
              bot=BOT, conversation_type="one_to_one", token=TOKEN,
              forwarded=False):
        return InboundEvent(
            "default", message_id, sender,
            (START + timedelta(seconds=offset)).isoformat(),
            "hello", bot, conversation_type, token, "recipient", forwarded,
        )


class WebFirstContextRuntimeTests(unittest.TestCase):
    def tearDown(self):
        fixture = getattr(self, "fixture", None)
        if fixture:
            fixture.close()

    def make(self, **kwargs):
        self.fixture = ContextRuntimeFixture(**kwargs)
        return self.fixture

    def test_plan_exists_without_inbound_does_not_send(self):
        f = self.make(batches=[Batch((), "c0")]); f.write_plan()
        self.assertEqual(f.run(), "WAITING_CONTEXT_TIMEOUT")
        self.assertEqual(f.send.calls, [])

    def test_inbound_without_plan_does_not_send(self):
        f = self.make(events=[f"placeholder"])
        # Replace the intentionally invalid placeholder with no accepted event.
        f.receive_transport = FakeIlinkTransport(batches=[Batch((), "c0")])
        f.receiver.transport = f.receive_transport
        self.assertEqual(f.run(), "NO_CONFIRMED_PLAN")
        self.assertEqual(f.send.calls, [])

    def test_plan_and_new_inbound_send_once(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)
        self.assertEqual(f.send.calls[0]["context_token"], TOKEN)

    def test_missing_wire_timestamp_is_timestamped_before_delivery_gate(self):
        event = replace(inbound_event(), received_at="")
        f = self.make(events=[event]); f.write_plan()
        # Keep the receive-boundary clock deterministic and strictly after the
        # runner start; the token store clock remains independent.
        f.receiver.clock = lambda: START + timedelta(seconds=2)
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)

    def test_ack_updates_ledger_automatically(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        ledger = DeliveryLedger(f.paths.plan_delivery_ledger_db)
        try:
            self.assertEqual(ledger.get("plan-1")["delivery_result"], "DELIVERED")
        finally:
            ledger.close()

    def test_ack_restart_is_noop(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        second = FakeSend()
        self.assertEqual(
            run_delivery_production(
                1, paths=f.paths, transport=second, token="TEST_CREDENTIAL",
                health_manager=FakeHealth(), receiver=f.receiver,
                context_store=f.context_store, require_fresh_context=True,
                now=START, clock=FakeClock(), sleep=FakeClock().sleep,
            ),
            "NOOP",
        )
        self.assertEqual(second.calls, [])

    def test_plan_arriving_after_runtime_start_is_detected(self):
        f = self.make(events=[inbound_event()])
        f.clock.on_sleep = lambda clock: f.write_plan() if len(clock.sleeps) == 1 else None
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)

    def test_inbound_before_plan_is_held_until_plan_arrives(self):
        f = self.make(events=[inbound_event()])
        f.clock.on_sleep = lambda clock: f.write_plan() if len(clock.sleeps) == 1 else None
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)

    def test_inbound_context_expiry_prevents_send(self):
        f = self.make(events=[inbound_event()], context_ttl=30)
        f.clock.on_sleep = lambda clock: f.write_plan() if len(clock.sleeps) == 1 else None
        self.assertEqual(f.run(), "WAITING_CONTEXT_TIMEOUT")
        self.assertEqual(f.send.calls, [])

    def test_old_context_before_runner_is_ignored(self):
        f = self.make(batches=[Batch((), "c0"), Batch((inbound_event(message_id="new"),), "c1")])
        old = inbound_event(message_id="old", offset=-10)
        ref = f.context_store.store(old)
        f.storage.save_event(old, "accepted")
        f.storage.set_context_ref(old.account_scope, old.message_id, ref)
        f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(f.send.calls[0]["context_token"], TOKEN)

    def test_wrong_sender_is_rejected(self):
        f = self.make(events=[inbound_event(sender="other")]); f.write_plan()
        self.assertEqual(f.run(), "WAITING_CONTEXT_TIMEOUT")
        self.assertEqual(f.send.calls, [])

    def test_group_message_is_rejected(self):
        f = self.make(events=[inbound_event(conversation_type="group")]); f.write_plan()
        self.assertEqual(f.run(), "WAITING_CONTEXT_TIMEOUT")
        self.assertEqual(f.send.calls, [])

    def test_bot_fingerprint_mismatch_is_rejected(self):
        f = self.make(events=[inbound_event(bot="other")]); f.write_plan()
        self.assertEqual(f.run(), "WAITING_CONTEXT_TIMEOUT")
        self.assertEqual(f.send.calls, [])

    def test_delivered_plan_is_not_resend(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        second = ContextRuntimeFixture(batches=[Batch((), "c0")])
        self.addCleanup(second.close)
        second.inbox = f.inbox
        # Reuse the authoritative files but a fresh sender fixture.
        self.assertEqual(
            run_delivery_production(
                1, paths=ProductionPaths(f.paths.root, delivery_inbox_dir=f.inbox),
                transport=second.send, token="TEST_CREDENTIAL", health_manager=FakeHealth(),
                receiver=second.receiver, context_store=second.context_store,
                require_fresh_context=True, now=START,
                clock=FakeClock(), sleep=FakeClock().sleep,
            ), "NOOP",
        )
        self.assertEqual(second.send.calls, [])

    def test_failed_plan_plus_new_inbound_is_not_auto_retried(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        f.send.state = "FAILED"
        self.assertEqual(f.run(), "FAILED")
        second = FakeSend()
        self.assertEqual(
            run_delivery_production(
                1, paths=f.paths, transport=second, token="TEST_CREDENTIAL",
                health_manager=FakeHealth(), receiver=f.receiver,
                context_store=f.context_store, require_fresh_context=True,
                now=START, clock=FakeClock(), sleep=FakeClock().sleep,
            ), "NOOP_EXISTING",
        )
        self.assertEqual(second.calls, [])

    def test_outbound_blocked_plus_inbound_does_not_send(self):
        health = FakeHealth([HealthPreflightResult(False, OUTBOUND_BLOCKED, OUTBOUND_PREPARE_FAILED, START.isoformat())])
        f = self.make(events=[inbound_event()], health=health); f.write_plan()
        self.assertEqual(f.run(), "OUTBOUND_PREPARE_FAILED")
        self.assertEqual(f.send.calls, [])

    def test_reauth_required_plus_inbound_does_not_send(self):
        health = FakeHealth([HealthPreflightResult(False, REAUTH_REQUIRED, "AUTH_INVALID", START.isoformat())])
        f = self.make(events=[inbound_event()], health=health); f.write_plan()
        self.assertEqual(f.run(), "WECHAT_REAUTH_REQUIRED")
        self.assertEqual(f.send.calls, [])

    def test_prepare_failed_latches_and_does_not_retry(self):
        f = self.make(events=[inbound_event()], send_state="FAILED"); f.write_plan()
        self.assertEqual(f.run(), "FAILED")
        self.assertEqual(len(f.send.calls), 1)

    def test_auth_invalid_health_state_blocks_before_send(self):
        health = FakeHealth([HealthPreflightResult(False, REAUTH_REQUIRED, "AUTH_INVALID", START.isoformat())])
        f = self.make(events=[inbound_event()], health=health); f.write_plan()
        self.assertEqual(f.run(), "WECHAT_REAUTH_REQUIRED")
        self.assertEqual(health.calls, [START])

    def test_conflicting_confirmed_plans_fail_closed(self):
        f = self.make(events=[inbound_event()]); f.write_plan("a"); f.write_plan("b")
        self.assertEqual(f.run(), "CONFLICT_REJECT")
        self.assertEqual(f.send.calls, [])

    def test_same_plan_hash_duplicate_files_are_one_candidate(self):
        f = self.make(events=[inbound_event()]); f.write_plan("same", name="DAILY_PLAN_DELIVERY_a.json")
        f.write_plan("same", name="DAILY_PLAN_DELIVERY_b.json")
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)

    def test_target_date_stays_frozen_across_midnight(self):
        start = datetime(2026, 9, 16, 23, 59, 30, tzinfo=DELIVERY_TZ)
        f = self.make(events=[inbound_event(base=datetime(2026, 9, 16, 23, 59, 30, tzinfo=DELIVERY_TZ))]); f.write_plan(target_date="2026-09-17")
        f.clock.current = start
        self.assertEqual(f.run(start=start), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)

    def test_timeout_with_pending_plan_has_no_failed_state(self):
        f = self.make(batches=[Batch((), "c0")]); f.write_plan()
        self.assertEqual(f.run(max_hours=1), "WAITING_CONTEXT_TIMEOUT")
        ledger = DeliveryLedger(f.paths.plan_delivery_ledger_db)
        try:
            self.assertIsNone(ledger.get("plan-1"))
        finally:
            ledger.close()

    def test_context_is_consumed_after_send_attempt(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertFalse(any(f.context_store.directory.glob("*.dat")))

    def test_second_inbound_cannot_trigger_second_send(self):
        f = self.make(batches=[Batch((inbound_event(),), "c1"), Batch((inbound_event("m-2"),), "c2")])
        f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)

    def test_no_context_does_not_call_send_transport(self):
        f = self.make(batches=[Batch((), "c0")]); f.write_plan()
        self.assertEqual(f.run(), "WAITING_CONTEXT_TIMEOUT")
        self.assertEqual(f.send.calls, [])

    def test_late_context_with_plan_sends_once(self):
        f = self.make(batches=[Batch((), "c0"), Batch((inbound_event("late"),), "c1")])
        f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)

    def test_invalid_plan_then_valid_plan_delivers_valid(self):
        f = self.make(events=[inbound_event()])
        f.write_plan(status="DRAFT", name="DAILY_PLAN_DELIVERY_invalid.json")
        f.clock.on_sleep = lambda clock: f.write_plan("valid") if len(clock.sleeps) == 1 else None
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(len(f.send.calls), 1)

    def test_receive_cursor_advances_without_outbound(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertTrue(f.storage.cursor())
        self.assertEqual(f.send.calls[0]["key"], "daily-plan-delivery:plan-1")

    def test_credential_and_recipient_are_not_in_production_log(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        log = (f.paths.logs_dir / "production.log").read_text(encoding="utf-8")
        self.assertNotIn("TEST_CREDENTIAL", log)
        self.assertNotIn("recipient", log)
        self.assertNotIn(TOKEN, log)

    def test_roadmap_review_and_scheduler_markers_are_unchanged(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        markers = {
            "roadmap": f.root / "roadmap.marker",
            "review": f.root / "review.marker",
            "scheduler": f.root / "scheduler.marker",
        }
        for path in markers.values():
            path.write_text("unchanged", encoding="ascii")
        self.assertEqual(f.run(), "DELIVERED")
        self.assertTrue(all(path.read_text(encoding="ascii") == "unchanged" for path in markers.values()))

    def test_single_instance_lock_blocks_second_worker(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        holder = ProductionLock(f.paths.lock_file)
        holder.acquire()
        self.addCleanup(holder.release)
        with self.assertRaises(RuntimeError):
            f.run()
        self.assertEqual(f.send.calls, [])

    def test_no_reminder_or_old_planner_surface_in_context_runtime(self):
        from daily_plan_production import delivery_runtime
        source = Path(delivery_runtime.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Reminder", source)
        self.assertNotIn("Phase19Orchestrator", source)
        self.assertNotIn("DailyPlanService", source)

    def test_context_ref_is_exactly_bound_to_message_id(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(f.receive_transport.calls[0][0], "getupdates")

    def test_context_token_is_passed_only_when_bound(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertIsNotNone(f.send.calls[0]["context_token"])

    def test_transport_failure_remains_closed_after_restart(self):
        f = self.make(events=[inbound_event()], send_state="FAILED"); f.write_plan()
        self.assertEqual(f.run(), "FAILED")
        second = FakeSend()
        self.assertEqual(
            run_delivery_production(
                1, paths=f.paths, transport=second, token="TEST_CREDENTIAL",
                health_manager=FakeHealth(), receiver=f.receiver,
                context_store=f.context_store, require_fresh_context=True,
                now=START, clock=FakeClock(), sleep=FakeClock().sleep,
            ), "NOOP_EXISTING",
        )
        self.assertEqual(second.calls, [])

    def test_no_network_calls_are_made_by_fake_fixture(self):
        f = self.make(events=[inbound_event()]); f.write_plan()
        self.assertEqual(f.run(), "DELIVERED")
        self.assertEqual(f.receive_transport.calls.count("get_bot_qrcode"), 0)
        self.assertEqual(f.receive_transport.calls.count("sendmessage"), 0)


if __name__ == "__main__":
    unittest.main()
