import json
import tempfile
import unittest
from pathlib import Path

from ilink_receive_probe.probe.proactive_send import (
    AmbiguousProactiveSendError, PILOT_KEY,
    PILOT_TEXT, ProactiveIlinkTransport, ProactiveSendStorage, SingleProactivePilot,
    build_proactive_request_spec,
)
from ilink_receive_probe.probe.recipient_binding import RecipientBindingUnavailable

TEST_RECIPIENT_FINGERPRINT = "abcdef012345"


class FakeBinding:
    def __init__(self, value="recipient"): self.value=value; self.calls=0
    def load_for_proactive_send(self, expected): self.calls+=1; assert expected == TEST_RECIPIENT_FINGERPRINT; return self.value


class FakeTransport:
    def __init__(self, outcome="ok"): self.outcome=outcome; self.calls=[]
    def send_pilot_text(self, recipient, key):
        self.calls.append((recipient,key))
        if self.outcome == "ambiguous": raise AmbiguousProactiveSendError()
        return {"ret":0}


class Phase16BTests(unittest.TestCase):
    def make(self, outcome="ok"):
        storage=ProactiveSendStorage(Path(tempfile.mkdtemp()) / "proactive.db"); self.addCleanup(storage.close)
        transport=FakeTransport(outcome); return storage, transport, SingleProactivePilot(FakeBinding(),storage,transport,TEST_RECIPIENT_FINGERPRINT)

    def test_one_successful_pilot_and_no_duplicate(self):
        storage, transport, pilot=self.make(); first=pilot.send_once(); second=pilot.send_once()
        self.assertEqual(first["state"],"ACKED_API"); self.assertTrue(first["success"]); self.assertTrue(second["duplicate"]); self.assertEqual(len(transport.calls),1)
        row=storage.get(PILOT_KEY); self.assertEqual(row["recipient_fingerprint"],TEST_RECIPIENT_FINGERPRINT); self.assertNotIn("recipient", " ".join(str(v) for v in row))

    def test_ambiguous_send_never_retries(self):
        _, transport, pilot=self.make("ambiguous"); self.assertEqual(pilot.send_once()["state"],"UNKNOWN"); self.assertTrue(pilot.send_once()["duplicate"]); self.assertEqual(len(transport.calls),1)

    def test_invalid_binding_fails_before_submit(self):
        storage=ProactiveSendStorage(Path(tempfile.mkdtemp()) / "proactive.db"); self.addCleanup(storage.close)
        class MissingBinding:
            def load_for_proactive_send(self, expected): raise RecipientBindingUnavailable()
        transport=FakeTransport()
        with self.assertRaises(RecipientBindingUnavailable): SingleProactivePilot(MissingBinding(),storage,transport,TEST_RECIPIENT_FINGERPRINT).send_once()
        self.assertIsNone(storage.get(PILOT_KEY)); self.assertEqual(transport.calls,[])

    def test_transport_body_has_fixed_text_and_no_context_token(self):
        self.assertEqual(PILOT_TEXT,"开始准备明日计划了吗？")
        # The production defaults remain context-free; the extracted builder
        # makes that contract directly testable without network I/O.
        spec = build_proactive_request_spec(
            token="TEST_TOKEN", recipient="TEST_RECIPIENT", text="test",
            idempotency_key="fixture-client-001",
            bot_agent="ilink_receive_probe/phase16b",
            x_wechat_uin="MTIzNDU2",
        )
        self.assertNotIn("context_token", spec.body["msg"])


if __name__ == "__main__": unittest.main()
