import json
import unittest
from unittest.mock import patch
from urllib.error import URLError

from ilink_receive_probe.probe.proactive_send import (
    AmbiguousProactiveSendError,
    DefiniteProactiveSendError,
    IlinkResponseDiagnostics,
    ProactiveIlinkTransport,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class ResponseObservabilityTests(unittest.TestCase):
    def make(self):
        return ProactiveIlinkTransport("token", base_url="https://example.invalid")

    def test_ret_zero_preserves_http_ret_errcode_and_keys(self):
        with patch("ilink_receive_probe.probe.proactive_send.urlopen",
                   return_value=FakeResponse({"ret": 0, "trace": "ok"})):
            result = self.make().send_text("recipient", "text", "key", "agent")
        self.assertIsInstance(result, IlinkResponseDiagnostics)
        self.assertEqual((result.http_status, result.ret, result.errcode), (200, 0, None))
        self.assertEqual(result.response_keys, ("ret", "trace"))

    def test_ret_minus_two_is_definite_and_keeps_separate_fields(self):
        with patch("ilink_receive_probe.probe.proactive_send.urlopen",
                   return_value=FakeResponse({"ret": -2, "errcode": 17, "message": "bad"})):
            with self.assertRaises(DefiniteProactiveSendError) as captured:
                self.make().send_text("recipient", "text", "key", "agent")
        error = captured.exception
        self.assertEqual(error.errcode, 17)
        self.assertEqual(error.diagnostics.http_status, 200)
        self.assertEqual(error.diagnostics.ret, -2)
        self.assertEqual(error.diagnostics.errcode, 17)
        self.assertEqual(error.diagnostics.response_keys, ("errcode", "message", "ret"))

    def test_errcode_minus_two_is_observed_without_changing_ret_driven_success(self):
        with patch("ilink_receive_probe.probe.proactive_send.urlopen",
                   return_value=FakeResponse({"ret": 0, "errcode": -2})):
            result = self.make().send_text("recipient", "text", "key", "agent")
        self.assertEqual(result.ret, 0)
        self.assertEqual(result.errcode, -2)

    def test_ret_minus_two_errmsg_is_bounded_and_sanitized(self):
        long_message = "authorization=secret\n" + ("x" * 400)
        with patch("ilink_receive_probe.probe.proactive_send.urlopen",
                   return_value=FakeResponse({"ret": -2, "errmsg": long_message})):
            with self.assertRaises(DefiniteProactiveSendError) as captured:
                self.make().send_text("recipient", "text", "key", "agent")
        errmsg = captured.exception.diagnostics.errmsg
        self.assertLessEqual(len(errmsg), 200)
        self.assertNotIn("secret", errmsg)
        self.assertNotIn("\n", errmsg)

    def test_network_exception_is_unknown_with_safe_metadata(self):
        with patch("ilink_receive_probe.probe.proactive_send.urlopen",
                   side_effect=URLError("offline")):
            with self.assertRaises(AmbiguousProactiveSendError) as captured:
                self.make().send_text("recipient", "text", "key", "agent")
        self.assertIsNone(captured.exception.diagnostics.http_status)
        self.assertEqual(captured.exception.diagnostics.response_keys, ())
        self.assertEqual(captured.exception.diagnostics.errmsg, "URLError")


if __name__ == "__main__":
    unittest.main()
