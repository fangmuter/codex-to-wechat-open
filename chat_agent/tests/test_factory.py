import unittest
from tempfile import TemporaryDirectory

from chat_agent.factory import build_manual_pilot
from chat_agent.model import ModelResult
from ilink_receive_probe.probe.models import Batch


class _Transport:
    def __init__(self):
        self.calls = 0

    def getupdates(self, _cursor):
        self.calls += 1
        return Batch((), f"cursor-{self.calls}")


class _Provider:
    def generate(self, _messages, *, timeout_seconds):
        return ModelResult("草稿", provider="fake")


class FactoryTests(unittest.TestCase):
    def test_factory_requires_explicit_receive_and_model_boundaries(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                build_manual_pilot(object(), _Provider(), directory)
            with self.assertRaises(ValueError):
                build_manual_pilot(_Transport(), object(), directory)

    def test_factory_is_isolated_and_disabled_until_explicit_enable(self):
        transport = _Transport()
        with TemporaryDirectory() as directory:
            pilot = build_manual_pilot(transport, _Provider(), directory)
            self.assertIsNone(pilot.review_server.address)
            self.assertFalse(pilot.runtime_control.enabled)
            self.assertFalse(pilot.router.enabled)
            self.assertTrue(pilot.receiver.defer_cursor)
            self.assertTrue((pilot.audit.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'"
            ).fetchone()))
            self.assertEqual(pilot.runtime.run_once().state, "RUNTIME_DISABLED")
            self.assertEqual(transport.calls, 0)
            pilot.runtime.enable()
            self.assertTrue(pilot.router.enabled)
            self.assertEqual(pilot.runtime.run_once().state, "RUNTIME_POLLED")
            self.assertEqual(transport.calls, 1)
            pilot.close()

    def test_factory_review_server_is_loopback_only_and_not_started(self):
        with TemporaryDirectory() as directory:
            pilot = build_manual_pilot(_Transport(), _Provider(), directory)
            self.assertIsNone(pilot.review_server.address)
            self.assertEqual(pilot.review_server.host, "127.0.0.1")
            pilot.close()


if __name__ == "__main__":
    unittest.main()
