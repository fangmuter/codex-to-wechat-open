import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from daily_plan_delivery import DeliveryInbox, DeliveryLedger, DeliveryService
from daily_plan_delivery.service import DELIVERY_TZ, payload_hash
from daily_plan_delivery.tests.test_delivery import make_payload


class RetryTransport:
    def __init__(self, states):
        self.states = iter(states)
        self.calls = []

    def send(self, text, idempotency_key, now, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(state=next(self.states))


class AuthorizedRetryTests(unittest.TestCase):
    def test_legacy_ledgers_seed_attempt_history_before_retry(self):
        root = Path(tempfile.mkdtemp())
        send_path = root / "send.db"
        db = sqlite3.connect(send_path)
        db.execute("""CREATE TABLE proactive_delivery_attempts(
            idempotency_key TEXT PRIMARY KEY, recipient_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, submitted INTEGER NOT NULL, updated_at TEXT NOT NULL,
            errcode TEXT)""")
        db.execute("INSERT INTO proactive_delivery_attempts VALUES(?,?,?,?,?,?)",
                   ("key", "fp", "FAILED", 1, "2026-09-08T23:00:00+08:00", "-2"))
        db.commit(); db.close()
        from daily_plan_proactive.real_transport import DeliveryLedger as SendLedger
        send = SendLedger(send_path)
        self.addCleanup(send.close)
        now = datetime(2026, 9, 8, 23, 30, tzinfo=DELIVERY_TZ)
        self.assertTrue(send.begin("key", "fp", now, allow_failed_retry=True))
        self.assertEqual([row["attempt_no"] for row in send.attempts("key")], [1, 2])

        plan_path = root / "plan.db"
        db = sqlite3.connect(plan_path)
        db.execute("""CREATE TABLE delivery_plans(
            plan_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, delivered_at TEXT,
            delivery_result TEXT NOT NULL, source_filename TEXT NOT NULL,
            last_reason TEXT)""")
        db.execute("INSERT INTO delivery_plans VALUES(?,?,?,?,?,?,?)",
                   ("plan", "b" * 64, "2026-09-08T23:00:00+08:00", None,
                    "FAILED", "plan.json", "FAILED"))
        db.commit(); db.close()
        plan = DeliveryLedger(plan_path)
        self.addCleanup(plan.close)
        self.assertTrue(plan.begin("plan", "b" * 64, "plan.json", now,
                                   allow_failed_retry=True))
        self.assertEqual([row["attempt_no"] for row in plan.attempts("plan")], [1, 2])

    def test_plan_history_preserves_failed_attempt_and_allows_one_retry(self):
        root = Path(tempfile.mkdtemp())
        ledger = DeliveryLedger(root / "ledger.db")
        self.addCleanup(ledger.close)
        now = datetime(2026, 9, 7, 21, tzinfo=DELIVERY_TZ)
        ledger.begin("plan", "a" * 64, "plan.json", now)
        ledger.record("plan", "FAILED", now, reason="FAILED")
        self.assertFalse(ledger.begin("plan", "a" * 64, "plan.json", now))
        self.assertTrue(ledger.begin("plan", "a" * 64, "plan.json", now,
                                     allow_failed_retry=True))
        ledger.record("plan", "DELIVERED", now)
        self.assertEqual([row["state"] for row in ledger.attempts("plan")],
                         ["FAILED", "DELIVERED"])
        self.assertFalse(ledger.begin("plan", "a" * 64, "plan.json", now,
                                      allow_failed_retry=True))

    def test_service_retry_requires_exact_plan_and_hash(self):
        root = Path(tempfile.mkdtemp())
        inbox = root / "inbox"
        inbox.mkdir()
        payload = make_payload(plan_id="retry-plan")
        inbox.joinpath("DAILY_PLAN_DELIVERY_retry.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        ledger = DeliveryLedger(root / "ledger.db")
        self.addCleanup(ledger.close)
        first = RetryTransport(["FAILED"])
        service = DeliveryService(DeliveryInbox(inbox), ledger, first)
        now = datetime(2026, 9, 7, 21, tzinfo=DELIVERY_TZ)
        self.assertEqual(service.deliver_tomorrow(now).status, "FAILED")
        digest = payload_hash(__import__(
            "daily_plan_delivery.schema", fromlist=["validate_delivery_payload"]
        ).validate_delivery_payload(payload))
        second = RetryTransport(["ACKED_API"])
        retried = DeliveryService(DeliveryInbox(inbox), ledger, second)
        result = retried.deliver_tomorrow(
            now, authorized_retry_plan_id="retry-plan", authorized_retry_hash=digest,
            authorized_retry_target_date="2026-09-08",
        )
        self.assertEqual(result.status, "DELIVERED")
        self.assertEqual(second.calls, [{"retry_failed": True}])
        self.assertEqual(len(ledger.attempts("retry-plan")), 2)

    def test_exact_target_override_requires_authorization(self):
        root = Path(tempfile.mkdtemp())
        inbox = root / "inbox"; inbox.mkdir()
        ledger = DeliveryLedger(root / "ledger.db")
        self.addCleanup(ledger.close)
        with self.assertRaises(ValueError):
            DeliveryService(DeliveryInbox(inbox), ledger, RetryTransport([])).deliver_tomorrow(
                datetime(2026, 9, 9, 8, tzinfo=DELIVERY_TZ),
                authorized_retry_target_date="2026-09-09",
            )


if __name__ == "__main__":
    unittest.main()
