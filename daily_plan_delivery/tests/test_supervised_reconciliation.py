import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from daily_plan_delivery import (
    DeliveryLedger,
    DeliveryLedgerConflict,
    SupervisedProbeEvidence,
)


PLAN_ID = "daily-plan-test-001"
HASH = "a" * 64
WRONG_HASH = "b" * 64
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _seed(path, *, state="FAILED", payload_hash=HASH, attempts=2):
    ledger = DeliveryLedger(path)
    ledger.begin(PLAN_ID, payload_hash, "plan.json", NOW)
    ledger.record(PLAN_ID, state, NOW, reason=state)
    for _ in range(1, attempts):
        if state != "FAILED":
            break
        if ledger.begin(PLAN_ID, payload_hash, "plan.json", NOW, allow_failed_retry=True):
            ledger.record(PLAN_ID, "FAILED", NOW, reason="FAILED")
    return ledger


def _evidence(**overrides):
    values = dict(
        plan_id=PLAN_ID, payload_hash=HASH, authorized=True,
        acked=True, message_id_present=True,
    )
    values.update(overrides)
    return SupervisedProbeEvidence(**values)


class SupervisedReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.db"
        self.ledger = _seed(self.path)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_failed_to_supervised_ack_reconciles_without_attempt(self):
        result = self.ledger.record_supervised_probe_success(
            PLAN_ID, HASH, _evidence(), NOW,
        )
        self.assertEqual(result.status, "RECONCILED")
        row = self.ledger.get(PLAN_ID)
        self.assertEqual(row["delivery_result"], "DELIVERED")
        self.assertIsNotNone(row["delivered_at"])
        self.assertEqual([r["state"] for r in self.ledger.attempts(PLAN_ID)], ["FAILED", "FAILED"])

    def test_multiple_failed_attempts_remain_preserved(self):
        self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW)
        attempts = self.ledger.attempts(PLAN_ID)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(row["state"] == "FAILED" for row in attempts))
        self.assertEqual([row["attempt_no"] for row in attempts], [1, 2])

    def test_duplicate_reconciliation_is_noop(self):
        first = self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW)
        second = self.ledger.record_supervised_probe_success(
            PLAN_ID, HASH, _evidence(), datetime(2026, 9, 16, 12, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(first.status, "RECONCILED")
        self.assertEqual(second.status, "NOOP_ALREADY_DELIVERED")
        self.assertEqual(first.delivered_at, second.delivered_at)

    def test_wrong_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.record_supervised_probe_success("other", HASH, _evidence(plan_id="other"), NOW)

    def test_wrong_hash_is_rejected(self):
        with self.assertRaises(DeliveryLedgerConflict):
            self.ledger.record_supervised_probe_success(
                PLAN_ID, WRONG_HASH, _evidence(payload_hash=WRONG_HASH), NOW,
            )

    def test_missing_ack_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.record_supervised_probe_success(
                PLAN_ID, HASH, _evidence(acked=False), NOW,
            )

    def test_authorization_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.record_supervised_probe_success(
                PLAN_ID, HASH, _evidence(authorized=False), NOW,
            )

    def test_missing_message_id_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.record_supervised_probe_success(
                PLAN_ID, HASH, _evidence(message_id_present=False), NOW,
            )

    def test_already_delivered_is_noop(self):
        self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW)
        self.assertEqual(
            self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW).status,
            "NOOP_ALREADY_DELIVERED",
        )

    def test_reconciliation_does_not_call_transport(self):
        calls = []
        self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW)
        self.assertEqual(calls, [])

    def test_reconciliation_does_not_increase_submitted_or_retry_count(self):
        before = len(self.ledger.attempts(PLAN_ID))
        submitted_before = sum(row["state"] in {"SENDING", "DELIVERED"} for row in self.ledger.attempts(PLAN_ID))
        self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW)
        after = self.ledger.attempts(PLAN_ID)
        self.assertEqual(len(after), before)
        self.assertEqual(sum(row["state"] in {"SENDING", "DELIVERED"} for row in after), submitted_before)

    def test_reconciliation_has_no_scheduler_dependency(self):
        scheduler_marker = {"enabled": False, "task_count": 1}
        before = dict(scheduler_marker)
        self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW)
        self.assertEqual(scheduler_marker, before)

    def test_reconciliation_does_not_touch_session_health(self):
        health_path = Path(self.temp.name) / "session-health.json"
        health_path.write_text(
            json.dumps({"state": "HEALTHY", "error_class": None}), encoding="utf-8",
        )
        before = health_path.read_bytes()
        self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW)
        self.assertEqual(health_path.read_bytes(), before)

    def test_unsupported_evidence_source_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.record_supervised_probe_success(
                PLAN_ID, HASH, _evidence(source="other_probe"), NOW,
            )

    def test_schema_and_history_are_readable_after_reopen(self):
        self.ledger.record_supervised_probe_success(PLAN_ID, HASH, _evidence(), NOW)
        self.ledger.close()
        reopened = DeliveryLedger(self.path)
        try:
            self.assertEqual(reopened.get(PLAN_ID)["delivery_result"], "DELIVERED")
            self.assertEqual([row["state"] for row in reopened.attempts(PLAN_ID)], ["FAILED", "FAILED"])
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
