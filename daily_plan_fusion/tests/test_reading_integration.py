import json
import tempfile
import unittest
from pathlib import Path

from daily_plan_fusion.engine import FusionEngine
from daily_plan_fusion.models import FusionItemInput, FusionRequest, Source, plan_draft_to_dict
from daily_plan_reading.source import ReadingRecommendationSource, ReadingStatus
from daily_plan_recommendation.models import Energy


CATALOG = Path(__file__).resolve().parents[2] / 'examples' / 'reading_catalog.example.json'


class ReadingFusionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.reading = ReadingRecommendationSource(CATALOG)

    def test_current_reading_survives_fusion_as_light_reading_item(self):
        request = FusionRequest('2026-09-08', '19:00-23:00', 240, Energy.NORMAL,
                                reading_source=self.reading)
        draft = FusionEngine().fuse(request)
        items = [item for item in draft.items if item.recommendation_origin == 'READING']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, '继续阅读《示例书籍》')
        self.assertEqual(items[0].estimated_minutes, 25)
        self.assertEqual(items[0].source, Source.SYSTEM_RECOMMENDED)
        self.assertEqual(items[0].capacity_key, 'reading:example-book-001')
        self.assertLessEqual(draft.total_committed_minutes + draft.total_recommended_minutes,
                             draft.available_minutes)
        self.assertEqual(self.reading.books()[0].status, ReadingStatus.READING)

    def test_reading_never_displaces_user_capacity(self):
        request = FusionRequest(
            '2026-09-08', '19:00-23:00', 240, Energy.NORMAL,
            (FusionItemInput('必须完成的作业', Source.USER_REQUIRED, 240, kind='must_do'),),
            reading_source=self.reading,
        )
        draft = FusionEngine().fuse(request)
        self.assertFalse(any(item.recommendation_origin == 'READING' for item in draft.items))
        self.assertEqual(draft.total_recommended_minutes, 0)
        self.assertEqual(len(draft.items), 1)

    def test_reading_metadata_is_serialized_without_progress_mutation(self):
        before = [(book.book_id, book.status.value) for book in self.reading.books()]
        draft = FusionEngine().fuse(FusionRequest(
            '2026-09-08', '19:00-23:00', 240, Energy.NORMAL,
            reading_source=self.reading,
        ))
        payload = plan_draft_to_dict(draft)
        reading = next(item for item in payload['items'] if item['recommendation_origin'] == 'READING')
        self.assertEqual(reading['capacity_key'], 'reading:example-book-001')
        self.assertEqual(reading['title'], '继续阅读《示例书籍》')
        self.assertEqual(before, [(book.book_id, book.status.value) for book in self.reading.books()])


if __name__ == '__main__':
    unittest.main()
