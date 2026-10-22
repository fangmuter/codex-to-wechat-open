import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from daily_plan_roadmap.storage import RoadmapProgressStore
from daily_plan_shared_state import export_shared_state
from daily_plan_shared_state.exporter import roadmap_summary_from_store
from daily_plan_shared_state.scripts.refresh_local import refresh_projection


class ProjectionProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.out = self.root / 'shared'
        self.out.mkdir()
        self.plan = {
            'plan_date': '2026-09-07', 'revision': 'rev-1', 'available_minutes': 240,
            'energy': 'NORMAL', 'user_fixed': [], 'user_required': [], 'carryover': [],
            'user_arranged': [], 'system_recommendations': [],
            'roadmap_recommendations': [{'title': '完成一项示例建模练习', 'minutes': 45}],
        }

    def _seed_shared(self):
        export_shared_state(self.out, confirmed_plan=self.plan,
                            roadmap_summary={'recently_completed_tasks': ['完成一项示例建模练习']},
                            generated_at='fixed')

    def test_canonical_not_started_excludes_pilot_done_projection(self):
        canonical = RoadmapProgressStore(self.root / 'canonical.db')
        pilot = RoadmapProgressStore(self.root / 'pilot.db')
        pilot.update_status('task', 'modeling_task_1', 'DONE', when=date(2026, 9, 5), source_of_update='WEB_REVIEW')
        pilot.close()
        self._seed_shared()
        refresh_projection(self.out, canonical_path := self.root / 'canonical.db')
        data = json.loads((self.out / 'DAILY_PLAN_SHARED_STATE.json').read_text(encoding='utf-8'))
        self.assertNotIn('完成一项示例建模练习', data['roadmap_summary']['recently_completed_tasks'])
        canonical.close()

    def test_canonical_done_is_visible(self):
        canonical = RoadmapProgressStore(self.root / 'canonical.db')
        canonical.update_status('task', 'modeling_task_1', 'DONE', when=date(2026, 9, 5), source_of_update='WEB_REVIEW')
        self._seed_shared()
        refresh_projection(self.out, self.root / 'canonical.db')
        data = json.loads((self.out / 'DAILY_PLAN_SHARED_STATE.json').read_text(encoding='utf-8'))
        self.assertIn('完成一项示例建模练习', data['roadmap_summary']['recently_completed_tasks'])
        canonical.close()

    def test_recommendation_or_confirmed_plan_does_not_mark_task_done(self):
        canonical = RoadmapProgressStore(self.root / 'canonical.db')
        self._seed_shared()
        refresh_projection(self.out, self.root / 'canonical.db')
        data = json.loads((self.out / 'DAILY_PLAN_SHARED_STATE.json').read_text(encoding='utf-8'))
        self.assertNotIn('完成一项示例建模练习', data['roadmap_summary']['recently_completed_tasks'])
        canonical.close()

    def test_refresh_is_deterministic_on_restart(self):
        canonical = RoadmapProgressStore(self.root / 'canonical.db')
        self._seed_shared()
        refresh_projection(self.out, self.root / 'canonical.db')
        first = (self.out / 'DAILY_PLAN_SHARED_STATE.json').read_text(encoding='utf-8')
        refresh_projection(self.out, self.root / 'canonical.db')
        second = (self.out / 'DAILY_PLAN_SHARED_STATE.json').read_text(encoding='utf-8')
        self.assertEqual(first, second)
        canonical.close()

    def test_pilot_projection_requires_explicit_injected_store(self):
        canonical = RoadmapProgressStore(self.root / 'canonical.db')
        pilot = RoadmapProgressStore(self.root / 'pilot.db')
        pilot.update_status('task', 'modeling_task_1', 'DONE', when=date(2026, 9, 5), source_of_update='WEB_REVIEW')
        self.assertIn('完成一项示例建模练习', roadmap_summary_from_store(pilot)['recently_completed_tasks'])
        self._seed_shared(); refresh_projection(self.out, self.root / 'canonical.db')
        data = json.loads((self.out / 'DAILY_PLAN_SHARED_STATE.json').read_text(encoding='utf-8'))
        self.assertNotIn('完成一项示例建模练习', data['roadmap_summary']['recently_completed_tasks'])
        pilot.close(); canonical.close()


if __name__ == '__main__':
    unittest.main()
