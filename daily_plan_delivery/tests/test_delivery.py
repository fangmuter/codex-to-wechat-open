from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daily_plan_delivery import (
    DeliveryInbox,
    DeliveryReadError,
    DeliveryLedger,
    DeliverySchemaError,
    DeliveryService,
    format_delivery_plan,
    validate_delivery_payload,
)
from daily_plan_delivery.service import payload_hash
from daily_plan_production import runtime as production_runtime
from daily_plan_production.delivery_runtime import run_delivery_production
from daily_plan_production.paths import ProductionPaths
from daily_plan_delivery.service import DELIVERY_TZ


def make_payload(*, plan_id="delivery-001", target_date="2026-09-08",
                 created_at="2026-09-07T12:00:00+08:00", status="CONFIRMED"):
    return {
        "schema_version": 1,
        "plan_id": plan_id,
        "target_date": target_date,
        "source": "chatgpt_web",
        "status": status,
        "created_at": created_at,
        "items": [
            {"item_id": "required-1", "source": "USER_REQUIRED",
             "title": "完成示例作业", "minutes": 60,
             "time_hint": "晚上", "reason": None},
            {"item_id": "arranged-circuit", "source": "USER_ARRANGED",
             "title": "复习示例方法", "minutes": 45,
             "time_hint": None, "reason": None},
            {"item_id": "arranged-cpp", "source": "USER_ARRANGED",
             "title": "练习 Programming 指针", "minutes": 45,
             "time_hint": None, "reason": None},
            {"item_id": "carryover-1", "source": "CARRYOVER",
             "title": "完成顺延的示例课程题", "minutes": 30,
             "time_hint": None, "reason": "昨日顺延"},
            {"item_id": "roadmap-logic", "source": "ROADMAP",
             "title": "练习组合逻辑化简", "minutes": 45,
             "time_hint": None, "reason": "路线阶段 A / milestone A1 的下一可执行任务",
             "roadmap_task_id": "foundation_task_1",
             "roadmap_stage": "A", "roadmap_milestone": "A1"},
            {"item_id": "system-1", "source": "SYSTEM_RECOMMENDED",
             "title": "完成一轮课程重点复习", "minutes": 30,
             "time_hint": None, "reason": "长期优先级第1"},
            {"item_id": "reading-1", "source": "READING",
             "title": "继续阅读《示例书籍》", "minutes": 25,
             "time_hint": None, "reason": "当前阅读"},
        ],
        "fixed_constraints": [
            {"title": "示例课程", "detail": "下午固定课程，不安排深度学习"},
        ],
        "execution_order": [
            "required-1", "arranged-circuit", "arranged-cpp", "carryover-1",
            "roadmap-logic", "system-1", "reading-1",
        ],
        "buffer_minutes": 30,
        "warnings": [],
    }


class FakeTransport:
    def __init__(self, state="ACKED_API"):
        self.state = state
        self.calls = []

    def send(self, text, idempotency_key, now):
        self.calls.append((text, idempotency_key, now))
        return SimpleNamespace(state=self.state)


class RaisingTransport:
    def __init__(self):
        self.calls = []

    def send(self, text, idempotency_key, now):
        self.calls.append(idempotency_key)
        raise TimeoutError("unknown send result")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Register the directory first so later ledger cleanups run before it
        # is removed on Windows (SQLite keeps the database file open).
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "DailyPlanDeliveryInbox"
        self.inbox.mkdir()
        self.ledger_path = self.root / "delivery-ledger.db"
        self.now = datetime(2026, 9, 7, 21, 0, tzinfo=DELIVERY_TZ)

    def write_plan(self, payload, name="DAILY_PLAN_DELIVERY_001.json"):
        path = self.inbox / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def service(self, transport=None, ledger_path=None):
        service = DeliveryService(
            DeliveryInbox(self.inbox),
            DeliveryLedger(ledger_path or self.ledger_path),
            transport or FakeTransport(),
        )
        self.addCleanup(service.ledger.close)
        return service

    def test_valid_schema_and_gate11_payload(self):
        plan = validate_delivery_payload(make_payload())
        self.assertEqual(plan.status, "CONFIRMED")
        self.assertEqual(plan.target_date, "2026-09-08")
        self.assertEqual(len(plan.items), 7)
        self.assertEqual(plan.items[4].roadmap_task_id, "foundation_task_1")
        self.assertEqual(plan.items[-1].title, "继续阅读《示例书籍》")

    def test_schema_rejects_invalid_status_unsafe_field_and_bad_execution(self):
        invalid_status = make_payload(status="DRAFT")
        with self.assertRaises(DeliverySchemaError):
            validate_delivery_payload(invalid_status)
        unsafe = make_payload()
        unsafe["recipient"] = "must never be accepted"
        with self.assertRaises(DeliverySchemaError):
            validate_delivery_payload(unsafe)
        bad_order = make_payload()
        bad_order["execution_order"].append("not-an-item")
        with self.assertRaises(DeliverySchemaError):
            validate_delivery_payload(bad_order)
        duplicate_ids = make_payload()
        duplicate_ids["items"].append(dict(duplicate_ids["items"][0]))
        with self.assertRaises(DeliverySchemaError):
            validate_delivery_payload(duplicate_ids)

    def test_invalid_file_does_not_block_valid_file(self):
        self.write_plan(make_payload(status="DRAFT"), "DAILY_PLAN_DELIVERY_000.json")
        self.write_plan(make_payload(), "DAILY_PLAN_DELIVERY_001.json")
        transport = FakeTransport()
        service = self.service(transport)
        result = service.deliver_tomorrow(self.now)
        self.assertEqual(result.status, "DELIVERED")
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(len(transport.calls), 1)

    def test_unreadable_file_is_retryable_and_not_permanently_rejected(self):
        self.write_plan(make_payload(), "DAILY_PLAN_DELIVERY_target.json")
        ledger = DeliveryLedger(self.ledger_path)
        self.addCleanup(ledger.close)
        transport = FakeTransport()
        service = DeliveryService(DeliveryInbox(self.inbox), ledger, transport)
        with patch(
            "daily_plan_delivery.inbox.load_delivery_json",
            side_effect=DeliveryReadError("delivery file unreadable"),
        ):
            self.assertEqual(service.deliver_tomorrow(self.now).status,
                             "NO_CONFIRMED_PLAN")
        self.assertEqual(service.last_scan_diagnostics["reject_reason_category"],
                         "UNREADABLE:1")
        self.assertIsNone(ledger.db.execute(
            "SELECT 1 FROM delivery_rejections WHERE source_filename=?",
            ("DAILY_PLAN_DELIVERY_target.json",),
        ).fetchone())
        self.assertEqual(service.deliver_tomorrow(self.now).status, "DELIVERED")
        self.assertEqual(len(transport.calls), 1)

    def test_invalid_utf8_is_schema_rejection_not_runtime_exception(self):
        path = self.inbox / "DAILY_PLAN_DELIVERY_invalid-utf8.json"
        path.write_bytes(b"\xff\xfe")
        entry = DeliveryInbox(self.inbox).scan()[0]
        self.assertIsNone(entry.plan)
        self.assertEqual(entry.error_category, "INVALID_SCHEMA")

    def test_scan_diagnostics_report_wrong_date_and_target_candidates(self):
        self.write_plan(make_payload(target_date="2026-09-07"),
                        "DAILY_PLAN_DELIVERY_wrong-date.json")
        self.write_plan(make_payload(), "DAILY_PLAN_DELIVERY_target.json")
        service = self.service(FakeTransport())
        self.assertEqual(service.deliver_tomorrow(self.now).status, "DELIVERED")
        diagnostics = service.last_scan_diagnostics
        self.assertEqual(diagnostics["json_file_count"], 2)
        self.assertEqual(diagnostics["target_date_candidate_count"], 1)
        self.assertEqual(diagnostics["valid_target_candidate_count"], 1)
        self.assertEqual(diagnostics["reject_reason_category"], "WRONG_DATE:1")

    def test_same_plan_hash_noop_and_restart_does_not_resend(self):
        self.write_plan(make_payload())
        transport = FakeTransport()
        ledger = DeliveryLedger(self.ledger_path)
        service = DeliveryService(DeliveryInbox(self.inbox), ledger, transport)
        self.assertEqual(service.deliver_tomorrow(self.now).status, "DELIVERED")
        ledger.close()
        reopened = DeliveryLedger(self.ledger_path)
        service = DeliveryService(DeliveryInbox(self.inbox), reopened, transport)
        self.assertEqual(service.deliver_tomorrow(self.now).status, "NOOP")
        self.assertEqual(len(transport.calls), 1)
        reopened.close()

    def test_same_plan_id_different_hash_is_conflict(self):
        self.write_plan(make_payload(), "DAILY_PLAN_DELIVERY_001.json")
        changed = make_payload()
        changed["items"][0]["minutes"] = 90
        self.write_plan(changed, "DAILY_PLAN_DELIVERY_002.json")
        transport = FakeTransport()
        result = self.service(transport).deliver_tomorrow(self.now)
        self.assertEqual(result.status, "CONFLICT_REJECT")
        self.assertEqual(transport.calls, [])

    def test_unknown_send_is_closed_without_blind_retry(self):
        self.write_plan(make_payload())
        transport = RaisingTransport()
        ledger = DeliveryLedger(self.ledger_path)
        service = DeliveryService(DeliveryInbox(self.inbox), ledger, transport)
        self.assertEqual(service.deliver_tomorrow(self.now).status, "UNKNOWN")
        self.assertEqual(service.deliver_tomorrow(self.now).status, "NOOP_EXISTING")
        self.assertEqual(len(transport.calls), 1)
        ledger.close()

    def test_date_selection_and_conflict_rules(self):
        wrong = make_payload(target_date="2026-09-07")
        self.write_plan(wrong)
        self.assertEqual(self.service().deliver_tomorrow(self.now).status, "NO_CONFIRMED_PLAN")
        self.inbox.joinpath("DAILY_PLAN_DELIVERY_001.json").unlink()
        self.write_plan(make_payload(plan_id="delivery-a"), "DAILY_PLAN_DELIVERY_A.json")
        self.write_plan(make_payload(plan_id="delivery-b"), "DAILY_PLAN_DELIVERY_B.json")
        result = self.service().deliver_tomorrow(self.now)
        self.assertEqual(result.status, "CONFLICT_REJECT")

    def test_formatter_labels_empty_sections_order_and_determinism(self):
        payload = make_payload()
        payload["items"] = list(reversed(payload["items"]))
        plan = validate_delivery_payload(payload)
        rendered = format_delivery_plan(plan)
        self.assertIn("【固定 / 不可安排】", rendered)
        self.assertIn("【必须完成】", rendered)
        self.assertIn("【你安排的】", rendered)
        self.assertIn("【顺延】", rendered)
        self.assertIn("【路线推荐】", rendered)
        self.assertIn("【系统推荐】", rendered)
        self.assertIn("【阅读】", rendered)
        self.assertIn("继续阅读《示例书籍》 25min", rendered)
        self.assertIn("缓冲：30min", rendered)
        self.assertLess(rendered.index("【路线推荐】"), rendered.index("【系统推荐】"))
        self.assertEqual(rendered, format_delivery_plan(validate_delivery_payload(payload)))
        empty = make_payload()
        empty["items"] = [empty["items"][0]]
        empty["execution_order"] = ["required-1"]
        empty_rendered = format_delivery_plan(validate_delivery_payload(empty))
        self.assertNotIn("【路线推荐】", empty_rendered)
        self.assertNotIn("【阅读】", empty_rendered)

    def test_payload_hash_is_independent_of_item_input_order(self):
        first = validate_delivery_payload(make_payload())
        payload = make_payload()
        payload["items"] = list(reversed(payload["items"]))
        second = validate_delivery_payload(payload)
        self.assertEqual(payload_hash(first), payload_hash(second))

    def test_production_runtime_delegates_without_legacy_planner(self):
        with patch("daily_plan_production.runtime.build_production_parts",
                   side_effect=AssertionError("legacy planner path called")):
            with patch("daily_plan_production.delivery_runtime.run_delivery_production",
                       return_value="NO_CONFIRMED_PLAN"):
                self.assertEqual(production_runtime.run_production(4, 5), "NO_CONFIRMED_PLAN")
        with patch("daily_plan_production.delivery_runtime.run_delivery_production",
                   return_value="NO_CONFIRMED_PLAN") as delegated:
            self.assertEqual(production_runtime.run_production(4, 5), "NO_CONFIRMED_PLAN")
        delegated.assert_called_once_with(4, 5)
        source = inspect.getsource(__import__(
            "daily_plan_production.delivery_runtime",
            fromlist=["delivery_runtime"],
        ))
        for forbidden in ("Friendly", "Phase19Orchestrator", "DailyPlanService", ".tick("):
            self.assertNotIn(forbidden, source)

    def test_disabled_delivery_runtime_does_not_send(self):
        paths = ProductionPaths(self.root / "production" / "daily-plan",
                                 delivery_inbox_dir=self.inbox)
        paths.root.mkdir(parents=True)
        paths.disabled_sentinel.write_text("disabled\n", encoding="ascii")
        transport = FakeTransport()
        self.assertEqual(run_delivery_production(
            4, paths=paths, transport=transport, token="test-token",
            now=self.now,
        ), "DISABLED")
        self.assertEqual(transport.calls, [])
        self.assertFalse(paths.lock_file.exists())

    def test_delivery_modules_have_no_roadmap_or_reading_mutation_authority(self):
        import daily_plan_delivery.service as service_module
        import daily_plan_delivery.inbox as inbox_module
        for module in (service_module, inbox_module):
            source = inspect.getsource(module)
            self.assertNotIn("RoadmapProgressStore", source)
            self.assertNotIn("ReadingRecommendationSource", source)
            self.assertNotIn("subprocess", source)
            self.assertNotIn("daily_plan_proactive.scheduler", source)

    def test_inbox_event_is_read_only(self):
        path = self.write_plan(make_payload())
        before = path.read_bytes()
        transport = FakeTransport()
        service = self.service(transport)
        self.assertEqual(service.deliver_tomorrow(self.now).status, "DELIVERED")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
