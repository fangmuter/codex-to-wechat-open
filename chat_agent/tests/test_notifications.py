import unittest

from chat_agent.notifications import (
    CallbackNotificationSink,
    NotificationEvent,
    RecoveryNotification,
    WindowsMessageNotificationSink,
)


class _Completed:
    returncode = 0


class NotificationTests(unittest.TestCase):
    def test_windows_sink_is_opt_in_and_does_not_send_draft_content(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return _Completed()

        sink = WindowsMessageNotificationSink(
            runner=runner, platform_name="Windows", timeout_seconds=2
        )
        event = NotificationEvent("draft-1234567890-secret", "PENDING")
        self.assertTrue(sink.notify(event))
        args, kwargs = calls[0]
        self.assertEqual(args[0][0:2], ["msg.exe", "*"])
        self.assertIn("draft-123456", args[0][2])
        self.assertNotIn("context_token", args[0][2])
        self.assertNotIn("actual draft body", args[0][2])
        self.assertEqual(kwargs["timeout"], 2)

    def test_callback_sink_receives_event(self):
        events = []
        sink = CallbackNotificationSink(events.append)
        event = NotificationEvent("draft-1", "NEEDS_USER_INPUT", "MISSING_FACT")
        self.assertTrue(sink.notify(event))
        self.assertEqual(events, [event])

    def test_windows_sink_uses_count_only_for_recovery(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append(args[0][2])
            return _Completed()

        sink = WindowsMessageNotificationSink(runner=runner, platform_name="Windows")
        self.assertTrue(sink.notify(RecoveryNotification(7)))
        self.assertIn("7", calls[0])
        self.assertNotIn("draft", calls[0].lower())


if __name__ == "__main__":
    unittest.main()
