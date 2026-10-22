import unittest
from datetime import datetime, timezone, timedelta

from chat_agent.audit import ChatAuditStore, DRAFT_EVENT, ROUTE_EVENT


UTC = timezone.utc


class ChatAuditStoreTests(unittest.TestCase):
    def test_store_is_bounded_and_contains_no_message_or_context_columns(self):
        store = ChatAuditStore(max_events=2)
        self.addCleanup(store.close)
        at = datetime(2026, 9, 10, 12, tzinfo=UTC)
        store.record(
            ROUTE_EVENT,
            occurred_at=at,
            account_scope="account-fp",
            contact_key="account-fp\x1fsender-fp\x1fbot-fp",
            route="MATCHED_CONTACT",
            mode="REVIEW",
            detail="message body must not be persisted",
        )
        store.record(
            DRAFT_EVENT,
            occurred_at=at + timedelta(seconds=1),
            account_scope="account-fp",
            contact_key="account-fp\x1fsender-fp\x1fbot-fp",
            draft_id="chat-batch-2",
            risk_flags=("RISK_HIGH",),
            source_count=1,
            draft_generated=False,
            detail="CONTEXT_UNAVAILABLE",
        )
        store.record(ROUTE_EVENT, occurred_at=at + timedelta(seconds=2), detail="third")

        events = store.list_events(limit=10)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].detail, "third")
        self.assertNotIn("message body", repr(events))
        columns = {
            row[1]
            for row in store.db.execute("PRAGMA table_info(audit_events)").fetchall()
        }
        self.assertNotIn("text", columns)
        self.assertNotIn("context_ref", columns)
        self.assertNotIn("context_token", columns)
        self.assertNotIn("recipient", columns)
        self.assertNotIn("prompt", columns)

    def test_detail_is_a_stable_code_and_risk_flags_are_structured(self):
        store = ChatAuditStore()
        self.addCleanup(store.close)
        store.record(
            DRAFT_EVENT,
            detail="raw secret\nvalue!",
            risk_flags=("RISK_HIGH", "RISK_HIGH", "PROMPT_INJECTION"),
        )
        event = store.list_events()[0]
        self.assertEqual(event.detail, "raw_secret_value_")
        self.assertEqual(event.risk_flags, ("RISK_HIGH", "PROMPT_INJECTION"))
        self.assertNotIn("\n", event.detail)
        self.assertNotIn("!", event.detail)


if __name__ == "__main__":
    unittest.main()
