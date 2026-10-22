import json
import tempfile
import threading
import unittest
from pathlib import Path

from daily_plan_production.lock import ProductionLock
from daily_plan_production.logging import build_logger


def metadata(*, pid=999999, run_id="old-run"):
    return {
        "schema_version": 1,
        "runner": "daily_plan_production",
        "pid": pid,
        "run_id": run_id,
        "created_at": "2026-09-09T12:00:00+00:00",
        "executable": "C:/Python314/pythonw.exe",
        "executable_fingerprint": "old-fingerprint",
    }


class Probe:
    def __init__(self, alive=False, valid=False):
        self.alive = alive
        self.valid = valid
        self.calls = []

    def __call__(self, pid):
        self.calls.append(pid)
        return {"alive": self.alive, "valid": self.valid}


class ProductionLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runner.lock"

    def test_no_lock_acquires_and_writes_bounded_metadata(self):
        lock = ProductionLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["runner"], "daily_plan_production")
        self.assertEqual(value["pid"], __import__("os").getpid())
        self.assertTrue(value["run_id"])
        self.assertTrue(value["executable_fingerprint"])

    def test_active_owner_rejects_second_runner(self):
        first = ProductionLock(self.path, process_probe=Probe(True, True))
        first.acquire()
        self.addCleanup(first.release)
        second = ProductionLock(self.path, process_probe=Probe(True, True))
        with self.assertRaisesRegex(RuntimeError, "already active"):
            second.acquire()
        self.assertTrue(self.path.exists())

    def test_dead_pid_json_lock_is_recovered_once(self):
        self.path.write_text(json.dumps(metadata()), encoding="utf-8")
        probe = Probe(False, False)
        recovered = []
        lock = ProductionLock(
            self.path, process_probe=probe,
            on_stale_recovered=lambda: recovered.append(True),
        )
        lock.acquire()
        self.addCleanup(lock.release)
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotEqual(value["run_id"], "old-run")
        self.assertEqual(recovered, [True])
        self.assertEqual(probe.calls, [999999])

    def test_legacy_plain_pid_stale_lock_is_recovered(self):
        self.path.write_text("999999", encoding="utf-8")
        lock = ProductionLock(self.path, process_probe=Probe(False, False))
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith("{"))

    def test_active_legacy_pid_is_rejected(self):
        self.path.write_text("12345", encoding="utf-8")
        lock = ProductionLock(self.path, process_probe=Probe(True, True))
        with self.assertRaisesRegex(RuntimeError, "already active"):
            lock.acquire()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "12345")

    def test_corrupt_lock_fails_closed(self):
        self.path.write_text("not-json", encoding="utf-8")
        probe = Probe(False, False)
        lock = ProductionLock(self.path, process_probe=probe)
        with self.assertRaisesRegex(RuntimeError, "corrupt"):
            lock.acquire()
        self.assertEqual(probe.calls, [])
        self.assertTrue(self.path.exists())

    def test_release_removes_own_lock(self):
        lock = ProductionLock(self.path)
        lock.acquire()
        self.assertTrue(lock.release())
        self.assertFalse(self.path.exists())

    def test_release_cannot_delete_another_run_id(self):
        lock = ProductionLock(self.path)
        lock.acquire()
        replacement = metadata(pid=__import__("os").getpid(), run_id="other-run")
        self.path.write_text(json.dumps(replacement), encoding="utf-8")
        self.assertFalse(lock.release())
        self.assertTrue(self.path.exists())

    def test_abrupt_termination_shape_is_recovered_and_old_owner_cannot_release(self):
        first = ProductionLock(self.path, run_id="crashed-run")
        first.acquire()
        second = ProductionLock(self.path, process_probe=Probe(False, False))
        second.acquire()
        self.assertNotEqual(second.run_id, first.run_id)
        second.release()
        self.assertFalse(self.path.exists())
        self.assertFalse(first.release())

    def test_concurrent_acquire_has_exactly_one_owner(self):
        results = []
        barrier = threading.Barrier(2)
        locks = []

        def attempt():
            lock = ProductionLock(self.path, process_probe=Probe(True, True))
            try:
                barrier.wait()
                lock.acquire()
                locks.append(lock)
                results.append("acquired")
            except RuntimeError:
                results.append("rejected")

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for lock in locks:
            lock.release()
        self.assertEqual(sorted(results), ["acquired", "rejected"])
        self.assertFalse(self.path.exists())

    def test_alive_owner_that_cannot_be_verified_fails_closed(self):
        self.path.write_text("12345", encoding="utf-8")
        lock = ProductionLock(self.path, process_probe=Probe(True, False))
        with self.assertRaisesRegex(RuntimeError, "could not be verified"):
            lock.acquire()
        self.assertTrue(self.path.exists())

    def test_runner_log_has_outcome_and_redacts_exception_values(self):
        logs = Path(self.temp.name) / "logs"
        logger = build_logger(logs)
        logger.event("runner_started", pid=123, run_id="run-1", pinned_target_date="2026-09-10")
        logger.event(
            "runner_exception", outcome="FAILED", exception_type="ValueError",
            exception_message="recipient=SECRET token=SECRET context_token=SECRET",
        )
        logger.event("lock_released", lock_released="YES", outcome="RELEASED")
        logger.close()
        text = (logs / "production.log").read_text(encoding="utf-8")
        self.assertIn("runner_started", text)
        self.assertIn("pid=123", text)
        self.assertIn("pinned_target_date=2026-09-10", text)
        self.assertIn("exception_type=ValueError", text)
        self.assertIn("lock_released", text)
        self.assertNotIn("SECRET", text)


if __name__ == "__main__":
    unittest.main()
