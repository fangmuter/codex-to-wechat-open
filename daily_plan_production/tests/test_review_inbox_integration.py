import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_plan_production.runtime import process_review_inbox
from daily_plan_roadmap.storage import RoadmapProgressStore
from daily_plan_roadmap.task_source import RoadmapTaskSource


class ProductionReviewInboxTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        self.store = RoadmapProgressStore(self.root / 'roadmap.db')
        self.addCleanup(self.store.close)

    def _write(self, name, payload):
        path = self.inbox / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def _payload(self, review_id, task_id='foundation_task_1', status='DONE'):
        return {
            'schema_version': 1,
            'review_id': review_id,
            'review_date': '2026-09-06',
            'source': 'chatgpt_weekly_review',
            'updates': [{'task_id': task_id, 'status': status}],
        }

    def test_empty_and_missing_inbox_are_noop(self):
        self.assertEqual(process_review_inbox(self.inbox, self.store)['status'], 'OK')
        result = process_review_inbox(self.root / 'missing', self.store)
        self.assertEqual(result['status'], 'NOOP')
        self.assertEqual(result['imported_count'], 0)

    def test_unreadable_inbox_is_noop(self):
        with patch('daily_plan_production.runtime.Path.iterdir', side_effect=OSError('unreadable')):
            result = process_review_inbox(self.inbox, self.store)
        self.assertEqual(result['status'], 'NOOP')
        self.assertEqual(result['reason'], 'inbox_unreadable')

    def test_valid_review_imports_before_task_source(self):
        self._write('ROADMAP_REVIEW_UPDATE_2026-09-06_valid.json', self._payload('valid-1'))
        result = process_review_inbox(self.inbox, self.store)
        self.assertEqual(result['imported_count'], 1)
        self.assertEqual(self.store.task_status('foundation_task_1').value, 'DONE')
        candidates = RoadmapTaskSource(self.store).candidates()
        self.assertFalse(any(c.task_id == 'foundation_task_1' for c in candidates))

    def test_malformed_review_rejected_and_valid_review_continues(self):
        self._write('ROADMAP_REVIEW_UPDATE_01_bad.json', dict(self._payload('bad'), schema_version=2))
        self._write('ROADMAP_REVIEW_UPDATE_02_good.json', self._payload('good', 'modeling_task_1', 'IN_PROGRESS'))
        result = process_review_inbox(self.inbox, self.store)
        self.assertEqual(result['files_seen'], 2)
        self.assertEqual(result['rejected_count'], 1)
        self.assertEqual(result['imported_count'], 1)
        self.assertEqual(self.store.task_status('modeling_task_1').value, 'IN_PROGRESS')

    def test_unknown_task_rejected_and_production_state_continues(self):
        self._write('ROADMAP_REVIEW_UPDATE_01_unknown.json', self._payload('unknown', 'not_a_task'))
        result = process_review_inbox(self.inbox, self.store)
        self.assertEqual(result['rejected_count'], 1)
        self.assertEqual(self.store.task_status('foundation_task_1').value, 'NOT_STARTED')

    def test_duplicate_review_is_noop(self):
        path = self._write('ROADMAP_REVIEW_UPDATE_2026-09-06_dup.json', self._payload('dup'))
        self.assertEqual(process_review_inbox(self.inbox, self.store)['imported_count'], 1)
        self.assertEqual(process_review_inbox(self.inbox, self.store)['noop_count'], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM imported_reviews').fetchone()[0], 1)
        self.assertTrue(path.exists())

    def test_multiple_reviews_have_stable_filename_order(self):
        self._write('ROADMAP_REVIEW_UPDATE_02_b.json', self._payload('b', 'modeling_task_1', 'IN_PROGRESS'))
        self._write('ROADMAP_REVIEW_UPDATE_01_a.json', self._payload('a', 'foundation_task_1', 'IN_PROGRESS'))
        result = process_review_inbox(self.inbox, self.store)
        self.assertEqual([x['name'] for x in result['results']], [
            'ROADMAP_REVIEW_UPDATE_01_a.json', 'ROADMAP_REVIEW_UPDATE_02_b.json'])
        self.assertEqual(result['imported_count'], 2)

    def test_review_import_does_not_send_or_change_shared_state(self):
        self._write('ROADMAP_REVIEW_UPDATE_2026-09-06_local.json', self._payload('local'))
        result = process_review_inbox(self.inbox, self.store)
        self.assertEqual(result['imported_count'], 1)
        self.assertFalse((self.root / 'DAILY_PLAN_SHARED_STATE.json').exists())


if __name__ == '__main__':
    unittest.main()
