import sqlite3
import json
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_plan_production.lock import ProductionLock
from daily_plan_production.logging import build_logger
from daily_plan_production.paths import ProductionPaths
from daily_plan_roadmap.storage import RoadmapProgressStore
from daily_plan_roadmap.task_source import RoadmapTaskSource
from daily_plan_orchestration.service import Phase19Orchestrator
from daily_plan_orchestration.session_meta import SessionMetaStore


class ProductionPreparationTests(unittest.TestCase):
    def test_live_paths_require_explicit_inboxes(self):
        values = {
            "DAILY_PLAN_STATE_DIR": str(Path(tempfile.mkdtemp()) / "private-state"),
            "DAILY_PLAN_REVIEW_INBOX": str(Path(tempfile.mkdtemp()) / "review-inbox"),
            "DAILY_PLAN_DELIVERY_INBOX": str(Path(tempfile.mkdtemp()) / "delivery-inbox"),
        }
        with patch.dict(os.environ, values, clear=True):
            paths = ProductionPaths.current_user()
        self.assertEqual(paths.root, Path(values["DAILY_PLAN_STATE_DIR"]))
        self.assertEqual(paths.review_inbox_dir, Path(values["DAILY_PLAN_REVIEW_INBOX"]))
        self.assertEqual(paths.delivery_inbox_dir, Path(values["DAILY_PLAN_DELIVERY_INBOX"]))

        with patch.dict(os.environ, {"DAILY_PLAN_STATE_DIR": values["DAILY_PLAN_STATE_DIR"]}, clear=True):
            with self.assertRaises(RuntimeError):
                ProductionPaths.current_user()

    def test_paths_are_separate_and_non_pilot(self):
        paths=ProductionPaths(Path(tempfile.mkdtemp())/'production'/'daily-plan')
        self.assertNotIn('phase19_real_pilot',str(paths.root)); self.assertNotEqual(paths.scheduler_db,paths.delivery_ledger_db); self.assertNotEqual(paths.planner_db,paths.metadata_db)
        self.assertNotEqual(paths.review_inbox_dir, paths.delivery_inbox_dir)
        self.assertNotEqual(paths.plan_delivery_ledger_db, paths.delivery_ledger_db)
        self.assertNotEqual(paths.plan_delivery_ledger_db, paths.delivery_send_ledger_db)
    def test_double_launch_lock_fails_closed_and_releases(self):
        root=Path(tempfile.mkdtemp()); first=ProductionLock(root/'runner.lock'); second=ProductionLock(root/'runner.lock'); first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(RuntimeError): second.acquire()
        first.release(); second.acquire(); second.release(); self.assertFalse((root/'runner.lock').exists())
    def test_log_wrapper_drops_non_allowlisted_fields(self):
        root=Path(tempfile.mkdtemp()); logger=build_logger(root); logger.event('send',state='ACKED_API',idempotency_key='phase16:test',raw_recipient='SECRET',context_token='SECRET',planner_state='DRAFT_READY')
        text=(root/'production.log').read_text(encoding='utf-8'); self.assertIn('ACKED_API',text); self.assertIn('phase16:test',text); self.assertNotIn('SECRET',text); self.assertNotIn('raw_recipient',text)
    def test_expected_schema_files_are_sqlite_paths(self):
        root=Path(tempfile.mkdtemp())/'production'/'daily-plan'; paths=ProductionPaths(root); root.mkdir(parents=True)
        for path in (paths.scheduler_db,paths.delivery_ledger_db,paths.plan_delivery_ledger_db,
                     paths.delivery_send_ledger_db,paths.inbound_db,paths.planner_db,
                     paths.exchange_db,paths.metadata_db):
            connection=sqlite3.connect(path); connection.execute('select 1'); connection.close()
        self.assertTrue(all(path.exists() for path in (paths.scheduler_db,paths.delivery_ledger_db,
                                                       paths.plan_delivery_ledger_db,
                                                       paths.delivery_send_ledger_db,
                                                       paths.inbound_db,paths.planner_db,
                                                       paths.exchange_db,paths.metadata_db)))

    def test_production_wiring_injects_real_roadmap_source_and_reads_persisted_progress(self):
        root=Path(tempfile.mkdtemp()); roadmap=RoadmapProgressStore(root/'roadmap.db'); roadmap.update_status('task','modeling_task_1','DONE',when=__import__('datetime').date(2026,9,5)); roadmap.close()
        reopened=RoadmapProgressStore(root/'roadmap.db'); orchestrator=Phase19Orchestrator(SessionMetaStore(root/'meta.db'),roadmap_source=RoadmapTaskSource(reopened)); self.addCleanup(orchestrator.meta.close); self.addCleanup(reopened.close)
        self.assertEqual(reopened.task_status('modeling_task_1').value,'DONE'); self.assertIsInstance(orchestrator.roadmap_source,RoadmapTaskSource)

    def test_canonical_roadmap_path_is_distinct_from_session_databases(self):
        paths=ProductionPaths(Path(tempfile.mkdtemp())/'production'/'daily-plan'); self.assertNotEqual(paths.roadmap_db,paths.planner_db); self.assertNotIn('phase17r_roadmap_pilot',str(paths.roadmap_db))

    def test_hidden_pythonw_context_scans_absolute_inbox(self):
        """The scheduler's windowed interpreter must see an explicit inbox."""
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            self.skipTest("pythonw.exe is not available")
        root = Path(tempfile.mkdtemp())
        inbox = root / "DailyPlanDeliveryInbox"
        inbox.mkdir()
        payload = {
            "schema_version": 1, "plan_id": "hidden-context-plan",
            "target_date": "2026-09-10", "source": "chatgpt_web",
            "status": "CONFIRMED", "created_at": "2026-09-09T12:00:00+08:00",
            "items": [{"item_id": "item-1", "source": "SYSTEM_RECOMMENDED",
                       "title": "测试任务", "minutes": 30,
                       "time_hint": None, "reason": "测试"}],
            "fixed_constraints": [], "execution_order": ["item-1"],
            "buffer_minutes": 15, "warnings": [],
        }
        (inbox / "DAILY_PLAN_DELIVERY_hidden-context-plan.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        report = root / "probe.json"
        script = root / "probe.py"
        project = Path(__file__).resolve().parents[2]
        script.write_text(
            "import json, pathlib, sys\n"
            f"sys.path.insert(0, {str(project)!r})\n"
            "from daily_plan_delivery import DeliveryInbox\n"
            "root = pathlib.Path(sys.argv[1]).resolve()\n"
            "entries = DeliveryInbox(root).scan()\n"
            "pathlib.Path(sys.argv[2]).write_text(json.dumps({\n"
            "  'executable': sys.executable, 'entries': len(entries),\n"
            "  'valid': any(e.plan is not None for e in entries)\n"
            "}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [str(pythonw), str(script), str(inbox), str(report)],
            cwd=str(Path(__file__).resolve().parents[2]),
            timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 0)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(result["executable"].lower().endswith("pythonw.exe"))
        self.assertEqual(result["entries"], 1)
        self.assertTrue(result["valid"])

    def test_scheduler_delivery_path_has_no_outbound_latch_clear_hook(self):
        # The scheduler invokes normal preflight only.  Recovery of an
        # OUTBOUND_BLOCKED latch is intentionally an explicit, one-shot
        # SessionHealthManager operation and is not reachable from this path.
        from daily_plan_production import delivery_runtime

        source = inspect.getsource(delivery_runtime.run_delivery_production)
        self.assertIn("deliver_tomorrow", source)
        self.assertNotIn("mark_staged_validation", source)
        self.assertNotIn("mark_outbound_staged_validation", source)
        self.assertNotIn("mark_healthy", source)

    def test_lifecycle_events_are_bounded_and_safe(self):
        from ilink_receive_probe.probe.session_lifecycle import SessionLifecycleLogger

        source = inspect.getsource(SessionLifecycleLogger)
        self.assertIn("sanitize_errmsg", source)
        self.assertIn("max_events", source)
        self.assertIn("max_bytes", source)
        self.assertNotIn("raw_recipient", source)
        self.assertNotIn("context_token", source)

if __name__=='__main__': unittest.main()
