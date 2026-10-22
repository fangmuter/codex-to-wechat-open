import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ilink_receive_probe.probe.pc_wechat_experiment import (
    PC_WECHAT_NOT_RUNNING,
    PC_WECHAT_RUNNING,
    detect_pc_wechat_processes,
)
from ilink_receive_probe.probe.session_lifecycle import (
    SessionLifecycleLogger,
    _boot_snapshot,
    record_boot_observation,
)


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "session-lifecycle.jsonl"

    def test_logger_redacts_and_bounds_safe_fields(self):
        logger = SessionLifecycleLogger(self.path, max_events=5, max_bytes=4096)
        event = logger.record(
            "PROACTIVE_SMOKE_FAILED", bot_fingerprint="A" * 12,
            account_fingerprint="B" * 12, credential_fingerprint="C" * 12,
            pc_wechat_state=PC_WECHAT_RUNNING, http_status=200, ret=-2,
            errmsg="context_token=SECRET credential=SECRET",
            response_keys=("ret", "errmsg"),
        )
        self.assertEqual(event.ret, -2)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("context_token=SECRET", raw)
        self.assertNotIn("credential=SECRET", raw)
        self.assertIn('"event_type":"PROACTIVE_SMOKE_FAILED"', raw)

    def test_logger_accepts_required_event_set_and_timeline(self):
        logger = SessionLifecycleLogger(self.path)
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        logger.record("AUTH_STAGED", timestamp=now)
        logger.record("AUTH_PROMOTED", timestamp=now)
        logger.record("INBOUND_PROBE_OK", timestamp=now)
        logger.record("PROACTIVE_SMOKE_OK", timestamp=now)
        timeline = logger.timeline()
        self.assertEqual(timeline["auth_staged_at"], now.isoformat())
        self.assertEqual(timeline["last_inbound_success_at"], now.isoformat())
        self.assertEqual(timeline["last_proactive_success_at"], now.isoformat())
        self.assertEqual(timeline["event_count"], 4)

    def test_timeline_orders_events_and_normalizes_offsets(self):
        logger = SessionLifecycleLogger(self.path, dedupe_seconds=0)
        logger.record("PROACTIVE_SMOKE_OK", timestamp="2026-09-12T12:01:00+08:00")
        logger.record("AUTH_STAGED", timestamp="2026-09-12T04:00:00Z")
        rows = logger.timeline()["rows"]
        self.assertEqual([row["event"] for row in rows], ["AUTH_STAGED", "PROACTIVE_SMOKE_OK"])
        self.assertTrue(rows[0]["timestamp"].endswith("+00:00"))

    def test_missing_timestamp_is_reported_as_unknown(self):
        self.path.write_text(
            json.dumps({"event_type": "INBOUND_PROBE_OK", "session_health": "HEALTHY"}) + "\n",
            encoding="utf-8",
        )
        timeline = SessionLifecycleLogger(self.path).timeline()
        self.assertIsNone(timeline["last_inbound_success_at"])
        self.assertEqual(timeline["event_count"], 1)
        self.assertIsNone(timeline["rows"][0]["timestamp"])

    def test_boot_observation_is_bounded(self):
        logger = SessionLifecycleLogger(self.path)
        event = record_boot_observation(logger)
        if event is not None:
            payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(payload["event_type"], "WINDOWS_BOOT_OBSERVED")
            self.assertRegex(payload["boot_session"], r"^[0-9a-f]{12}$")

    def test_boot_identity_is_stable_for_one_boot(self):
        now = datetime(2026, 9, 12, 12, 0, 0, 123000, tzinfo=timezone.utc)
        first = _boot_snapshot(now, uptime_ms=123456)
        second = _boot_snapshot(now, uptime_ms=123456)
        self.assertEqual(first, second)

    def test_boot_identity_change_logs_reboot_event(self):
        logger = SessionLifecycleLogger(self.path, dedupe_seconds=0)
        first_at = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        second_at = first_at + timedelta(hours=1)
        record_boot_observation(logger, now=first_at, uptime_ms=100_000,
                                session_health="HEALTHY")
        record_boot_observation(logger, now=second_at, uptime_ms=100_000,
                                session_health="UNKNOWN")
        events = logger.list_events(limit=10)
        self.assertIn("WINDOWS_REBOOT_DETECTED", [item["event_type"] for item in events])
        reboot = next(item for item in events if item["event_type"] == "WINDOWS_REBOOT_DETECTED")
        self.assertEqual(reboot["last_known_session_health_before_boot"], "HEALTHY")
        self.assertEqual(reboot["first_observed_session_health"], "UNKNOWN")

    def test_prepare_failure_correlation_uses_receive_health_anchor(self):
        logger = SessionLifecycleLogger(self.path, dedupe_seconds=0)
        healthy_at = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        failed_at = healthy_at + timedelta(seconds=90)
        logger.record("HEALTH_CHECK_RESULT", timestamp=healthy_at,
                      inbound_result="OK", session_health="HEALTHY",
                      boot_session="a" * 12)
        logger.record("SESSION_PREPARE_FAILED", timestamp=failed_at,
                      error_class="SESSION_PREPARE_FAILED", boot_session="a" * 12)
        rows = logger.correlate_faults()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seconds_since_inbound"], 90.0)
        self.assertEqual(rows[0]["seconds_since_last_health_check"], 90.0)
        self.assertEqual(rows[0]["reboot_happened_since_last_healthy"], "NO")
        self.assertEqual(rows[0]["idle_seconds"], 90.0)

    def test_prepare_failure_correlation_marks_reboot_when_boot_changes(self):
        logger = SessionLifecycleLogger(self.path, dedupe_seconds=0)
        healthy_at = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        failed_at = healthy_at + timedelta(seconds=90)
        logger.record("HEALTH_CHECK_RESULT", timestamp=healthy_at,
                      inbound_result="OK", session_health="HEALTHY",
                      boot_session="a" * 12)
        logger.record("WINDOWS_REBOOT_DETECTED", timestamp=healthy_at + timedelta(seconds=30),
                      boot_session="b" * 12, previous_boot_session="a" * 12)
        logger.record("SESSION_PREPARE_FAILED", timestamp=failed_at,
                      error_class="SESSION_PREPARE_FAILED", boot_session="b" * 12)
        self.assertEqual(logger.correlate_faults()[0]["reboot_happened_since_last_healthy"], "YES")

class PcWeChatDetectorTests(unittest.TestCase):
    def test_detector_uses_exact_process_names_and_returns_metadata(self):
        snapshot = detect_pc_wechat_processes(process_provider=lambda: [
            {"Name": "Weixin.exe", "ProcessId": 42,
             "CreationDate": "2026-09-12T12:00:00", "ExecutablePath": "D:\\Weixin\\Weixin.exe"},
            {"Name": "notepad.exe", "ProcessId": 9},
        ])
        self.assertEqual(snapshot.state, PC_WECHAT_RUNNING)
        self.assertEqual(snapshot.process_count, 1)
        self.assertEqual(snapshot.processes[0].pid, 42)

    def test_detector_reports_not_running(self):
        snapshot = detect_pc_wechat_processes(process_provider=lambda: [])
        self.assertEqual(snapshot.state, PC_WECHAT_NOT_RUNNING)
        self.assertEqual(snapshot.process_count, 0)


if __name__ == "__main__":
    unittest.main()
