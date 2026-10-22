import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chat_agent.drafts import APPROVED, DraftStore, NEEDS_USER_INPUT, REJECTED
from chat_agent.contacts import ContactPairingStore
from chat_agent.review_server import LocalReviewServer
from chat_agent.send_coordinator import SendOutcome


UTC = timezone.utc


class _Sender:
    def __init__(self):
        self.calls = []

    def send_approved(self, draft_id, *, confirmed=False):
        self.calls.append((draft_id, confirmed))
        return SendOutcome(draft_id, "SENT_CONFIRMED", "API_ACCEPTED_WITH_RECEIPT", True, False, "receipt")


class ReviewServerTests(unittest.TestCase):
    def setUp(self):
        self.store = DraftStore()
        created = datetime.now(UTC).replace(microsecond=0)
        self.created = created
        self.first = self.store.create_draft(
            "account", "account\x1fsender\x1fbot", "batch-1", ["m1"],
            "m1", "ctx-ref", "初稿", created_at=created,
        )
        self.server = LocalReviewServer(self.store).start()
        self.addCleanup(self.server.stop)
        self.addCleanup(self.store.close)

    def request(self, path, method="GET", payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.server.address.rstrip("/") + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            error.read()
            error.close()
            raise

    def test_loopback_page_lists_and_approves_without_configured_send(self):
        with urlopen(self.server.address, timeout=2) as response:
            self.assertIn(b"Chat Agent REVIEW", response.read())
        status, payload = self.request("/api/drafts")
        self.assertEqual(status, 200)
        self.assertEqual(payload["drafts"][0]["draft_text"], "初稿")
        status, payload = self.request(
            f"/api/drafts/{self.first['draft_id']}/approve",
            method="POST",
            payload={"edited_text": "人工确认版"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["draft"]["status"], APPROVED)
        self.assertEqual(self.store.get(self.first["draft_id"])["final_text"], "人工确认版")
        with self.assertRaises(HTTPError) as error:
            self.request(f"/api/drafts/{self.first['draft_id']}/send", method="POST")
        self.assertEqual(error.exception.code, 409)

    def test_configured_send_route_requires_confirmation(self):
        self.server.stop()
        sender = _Sender()
        self.server = LocalReviewServer(self.store, send_coordinator=sender).start()
        self.request(
            f"/api/drafts/{self.first['draft_id']}/approve",
            method="POST", payload={},
        )
        with self.assertRaises(HTTPError) as error:
            self.request(
                f"/api/drafts/{self.first['draft_id']}/send",
                method="POST", payload={},
            )
        self.assertEqual(error.exception.code, 409)
        status, payload = self.request(
            f"/api/drafts/{self.first['draft_id']}/send",
            method="POST", payload={"confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["send"]["receipt_id"], "receipt")
        self.assertEqual(sender.calls, [(self.first["draft_id"], True)])

    def test_sensitive_approval_requires_second_confirmation_and_reject_works(self):
        with self.assertRaises(HTTPError) as error:
            self.request(
                f"/api/drafts/{self.first['draft_id']}/approve",
                method="POST", payload={"sensitive": True},
            )
        self.assertEqual(error.exception.code, 409)
        status, payload = self.request(
            f"/api/drafts/{self.first['draft_id']}/approve",
            method="POST",
            payload={"sensitive": True, "second_confirmation": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["draft"]["status"], APPROVED)

        second = self.store.create_draft(
            "account", "account\x1fsender\x1fbot", "batch-2", ["m2"],
            "m2", "ctx-ref-2", "第二版", created_at=self.created + timedelta(minutes=1),
        )
        status, payload = self.request(
            f"/api/drafts/{second['draft_id']}/reject", method="POST", payload={}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["draft"]["status"], REJECTED)

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaises(ValueError):
            LocalReviewServer(self.store, host="0.0.0.0")

    def test_pairing_api_lists_candidate_and_rejects_auto(self):
        contacts = ContactPairingStore()
        self.addCleanup(contacts.close)
        observed = datetime.now(UTC)
        contacts.observe_candidate(
            "account", "sender-fp", "bot-fp", "candidate-1",
            observed_at=observed, preview_text="候选消息",
        )
        self.server.stop()
        self.server = LocalReviewServer(self.store, contact_store=contacts).start()
        status, payload = self.request("/api/pairing/candidates")
        self.assertEqual(status, 200)
        self.assertEqual(payload["candidates"][0]["preview_text"], "候选消息")
        with self.assertRaises(HTTPError) as error:
            self.request(
                "/api/pairing/confirm", method="POST",
                payload={
                    "account_scope": "account", "sender_fingerprint": "sender-fp",
                    "bot_fingerprint": "bot-fp", "alias": "小号", "mode": "auto",
                },
            )
        self.assertEqual(error.exception.code, 409)
        status, payload = self.request(
            "/api/pairing/confirm", method="POST",
            payload={
                "account_scope": "account", "sender_fingerprint": "sender-fp",
                "bot_fingerprint": "bot-fp", "alias": "小号", "mode": "review",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["contact"]["mode"], "review")

    def test_needs_user_input_can_be_completed_from_review_api(self):
        draft = self.store.create_draft(
            "account", "account\x1fsender\x1fbot", "batch-input", ["m-input"],
            "m-input", "ctx-ref", None, needs_user_input=True,
            created_at=self.created + timedelta(minutes=1),
        )
        status, payload = self.request(
            f"/api/drafts/{draft['draft_id']}/approve", method="POST",
            payload={"edited_text": "人工补充后的明确回复"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["draft"]["status"], APPROVED)

    def test_draft_api_exposes_contact_alias_for_review_ui(self):
        contacts = ContactPairingStore()
        self.addCleanup(contacts.close)
        created = self.created
        contacts.observe_candidate(
            "account", "sender", "bot", "alias-source", observed_at=created,
        )
        contacts.confirm_candidate("account", "sender", "bot", alias="小号", now=created)
        self.server.stop()
        self.server = LocalReviewServer(self.store, contact_store=contacts).start()
        status, payload = self.request("/api/drafts")
        self.assertEqual(status, 200)
        self.assertEqual(payload["drafts"][0]["contact_alias"], "小号")

    def test_high_risk_review_api_requires_second_confirmation(self):
        draft = self.store.create_draft(
            "account", "account\x1fsender\x1fbot", "batch-high", ["m-high"],
            "m-high", "ctx-high", "高风险草稿", risk_flags=("RISK_HIGH", "MONEY"),
            created_at=self.created + timedelta(minutes=2),
        )
        with self.assertRaises(HTTPError) as error:
            self.request(
                f"/api/drafts/{draft['draft_id']}/approve", method="POST",
                payload={},
            )
        self.assertEqual(error.exception.code, 409)
        status, payload = self.request(
            f"/api/drafts/{draft['draft_id']}/approve", method="POST",
            payload={"second_confirmation": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["draft"]["status"], APPROVED)


if __name__ == "__main__":
    unittest.main()
