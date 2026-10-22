import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from daily_plan_delivery import DeliveryLedger
from daily_plan_delivery.service import DELIVERY_TZ
from daily_plan_production.delivery_runtime import run_delivery_production
from daily_plan_production.lock import ProductionLock
from daily_plan_production.paths import ProductionPaths


def payload(plan_id, target_date, *, status="CONFIRMED"):
    return {
        "schema_version": 1,
        "plan_id": plan_id,
        "target_date": target_date,
        "source": "chatgpt_web",
        "status": status,
        "created_at": "2026-09-09T12:00:00+08:00",
        "items": [{
            "item_id": "item-1", "source": "SYSTEM_RECOMMENDED",
            "title": "完成一项测试任务", "minutes": 30,
            "time_hint": None, "reason": "测试",
        }],
        "fixed_constraints": [],
        "execution_order": ["item-1"],
        "buffer_minutes": 15,
        "warnings": [],
    }


class FakeClock:
    def __init__(self, current, on_sleep=None):
        self.current = current
        self.on_sleep = on_sleep
        self.sleeps = []

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep(self)


class FakeTransport:
    def __init__(self, state="ACKED_API"):
        self.state = state
        self.calls = []

    def send(self, text, idempotency_key, now):
        self.calls.append((text, idempotency_key, now))
        return SimpleNamespace(state=self.state)


class BoundedWaitingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.inbox = root / "DailyPlanDeliveryInbox"
        self.inbox.mkdir()
        self.paths = ProductionPaths(root / "production" / "daily-plan",
                                     delivery_inbox_dir=self.inbox)
        self.start = datetime(2026, 9, 9, 22, 0, tzinfo=DELIVERY_TZ)

    def write(self, plan_id="plan-1", target_date="2026-09-10", *, status="CONFIRMED", name=None):
        name = name or f"DAILY_PLAN_DELIVERY_{plan_id}.json"
        path = self.inbox / name
        path.write_text(json.dumps(payload(plan_id, target_date, status=status),
                                    ensure_ascii=False), encoding="utf-8")
        return path

    def run_runtime(self, transport, *, start=None, clock=None, max_hours=1, poll_seconds=60):
        start = start or self.start
        clock = clock or FakeClock(start)
        return run_delivery_production(
            max_hours, poll_seconds=poll_seconds, paths=self.paths,
            transport=transport, token="test-token", now=start,
            clock=clock, sleep=clock.sleep,
        )

    def test_plan_present_at_start_sends_once(self):
        self.write()
        transport = FakeTransport()
        clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(transport, clock=clock), "DELIVERED")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_late_plan_appears_on_second_poll(self):
        def appear(clock):
            if len(clock.sleeps) == 1:
                self.write()

        transport = FakeTransport()
        clock = FakeClock(self.start, appear)
        self.assertEqual(self.run_runtime(transport, clock=clock), "DELIVERED")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(clock.sleeps), 1)

    def test_window_without_plan_exits_safely(self):
        transport = FakeTransport()
        clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(transport, clock=clock, max_hours=1), "NO_CONFIRMED_PLAN")
        self.assertEqual(transport.calls, [])
        self.assertEqual(clock.sleeps, [60.0] * 60)

    def test_plan_after_window_is_not_delivered(self):
        def appear(clock):
            if len(clock.sleeps) == 1:
                clock.current = self.start + timedelta(hours=1, seconds=1)
                self.write()

        transport = FakeTransport()
        clock = FakeClock(self.start, appear)
        self.assertEqual(self.run_runtime(transport, clock=clock, max_hours=1), "NO_CONFIRMED_PLAN")
        self.assertEqual(transport.calls, [])

    def test_invalid_plan_is_rejected_and_later_valid_plan_delivered(self):
        self.write(status="DRAFT", name="DAILY_PLAN_DELIVERY_bad.json")

        def appear(clock):
            if len(clock.sleeps) == 1:
                self.write(plan_id="valid", name="DAILY_PLAN_DELIVERY_valid.json")

        transport = FakeTransport()
        clock = FakeClock(self.start, appear)
        self.assertEqual(self.run_runtime(transport, clock=clock), "DELIVERED")
        self.assertEqual(len(transport.calls), 1)
        ledger = DeliveryLedger(self.paths.plan_delivery_ledger_db)
        self.addCleanup(ledger.close)
        self.assertIsNotNone(ledger.db.execute(
            "SELECT 1 FROM delivery_rejections WHERE source_filename=?",
            ("DAILY_PLAN_DELIVERY_bad.json",),
        ).fetchone())

    def test_conflicting_confirmed_plans_fail_closed(self):
        self.write(plan_id="a", name="DAILY_PLAN_DELIVERY_a.json")
        self.write(plan_id="b", name="DAILY_PLAN_DELIVERY_b.json")
        transport = FakeTransport()
        clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(transport, clock=clock), "CONFLICT_REJECT")
        self.assertEqual(transport.calls, [])
        self.assertEqual(clock.sleeps, [])

    def test_same_delivered_plan_reappearing_is_noop(self):
        self.write()
        first = FakeTransport()
        first_clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(first, clock=first_clock), "DELIVERED")
        second = FakeTransport()
        second_clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(second, clock=second_clock), "NOOP")
        self.assertEqual(second.calls, [])

    def test_target_date_remains_pinned_across_midnight(self):
        start = datetime(2026, 9, 9, 23, 59, 30, tzinfo=DELIVERY_TZ)

        def appear(clock):
            if len(clock.sleeps) == 1:
                self.write(target_date="2026-09-10")

        transport = FakeTransport()
        clock = FakeClock(start, appear)
        self.assertEqual(self.run_runtime(transport, start=start, clock=clock), "DELIVERED")
        self.assertGreater(clock.current.date(), start.date())
        self.assertEqual(len(transport.calls), 1)

    def test_definite_failed_transport_is_not_resent(self):
        self.write()
        transport = FakeTransport("FAILED")
        clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(transport, clock=clock), "FAILED")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_ambiguous_transport_is_not_resent(self):
        self.write()
        transport = FakeTransport("UNKNOWN")
        clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(transport, clock=clock), "UNKNOWN")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_lock_prevents_second_production_instance(self):
        holder = ProductionLock(self.paths.lock_file)
        holder.acquire()
        self.addCleanup(holder.release)
        transport = FakeTransport()
        with self.assertRaises(RuntimeError):
            self.run_runtime(transport)
        self.assertEqual(transport.calls, [])

    def test_runner_persists_bounded_lifecycle_log(self):
        self.write()
        transport = FakeTransport()
        clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(transport, clock=clock), "DELIVERED")
        log = (self.paths.logs_dir / "production.log").read_text(encoding="utf-8")
        self.assertIn("runner_started", log)
        self.assertIn("pinned_target_date=2026-09-10", log)
        self.assertIn("delivery_event", log)
        self.assertIn("outcome=DELIVERED", log)
        self.assertIn("runner_exit", log)
        self.assertIn("lock_released", log)

    def test_runner_logs_bounded_scan_diagnostics(self):
        self.write()
        transport = FakeTransport()
        clock = FakeClock(self.start)
        self.assertEqual(self.run_runtime(transport, clock=clock), "DELIVERED")
        log = (self.paths.logs_dir / "production.log").read_text(encoding="utf-8")
        self.assertIn("delivery_scan", log)
        self.assertIn("inbox_accessible=True", log)
        self.assertIn("json_file_count=1", log)
        self.assertIn("target_date_candidate_count=1", log)
        self.assertIn("valid_target_candidate_count=1", log)
        self.assertIn("reject_reason_category=NONE", log)
        self.assertNotIn("完成一项测试任务", log)


if __name__ == "__main__":
    unittest.main()
