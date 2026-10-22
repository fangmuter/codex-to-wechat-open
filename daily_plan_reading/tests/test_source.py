import json
import tempfile
import unittest
from pathlib import Path

from daily_plan_reading.source import ReadingRecommendationSource, ReadingStatus


class ReadingSourceTests(unittest.TestCase):
    def sample_catalog(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'reading-catalog.json'
        path.write_text(json.dumps({
            'schema_version': 1,
            'books': [
                {'book_id': 'sample-current', 'title': 'Example reading item',
                 'queue_order': 1, 'status': 'READING', 'daily_minutes': 25},
                {'book_id': 'sample-next', 'title': 'Second example item',
                 'queue_order': 2, 'status': 'NOT_STARTED'},
            ],
            'current_book_id': 'sample-current',
            'default_daily_minutes': 25,
        }, ensure_ascii=False), encoding='utf-8')
        return path

    def test_missing_catalog_is_empty_and_never_fabricates_books(self):
        source = ReadingRecommendationSource()
        self.assertTrue(source.data_required)
        self.assertEqual(source.books(), ())
        self.assertEqual(source.candidates(240), ())

    def test_explicit_catalog_is_typed_and_done_is_not_recommended(self):
        path = Path(tempfile.mktemp('.json'))
        path.write_text(json.dumps({'books': [
            {'book_id': 'b1', 'title': '用户书目', 'queue_order': 1, 'status': 'READING', 'daily_minutes': 25},
            {'book_id': 'b2', 'title': '已读', 'queue_order': 2, 'status': 'DONE'},
        ]}, ensure_ascii=False), encoding='utf-8')
        source = ReadingRecommendationSource(path)
        self.assertEqual(source.candidates(30)[0].status, ReadingStatus.READING)
        self.assertEqual([book.book_id for book in source.candidates(30)], ['b1'])

    def test_user_catalog_has_one_current_book_and_preserves_queue(self):
        catalog = self.sample_catalog()
        source = ReadingRecommendationSource(catalog)
        books = source.books()
        self.assertEqual(len(books), 2)
        self.assertEqual(source.current_book_id, 'sample-current')
        self.assertEqual(source.default_daily_minutes, 25)
        self.assertEqual([book.queue_order for book in books], [1, 2])
        self.assertEqual(sum(book.status == ReadingStatus.READING for book in books), 1)
        self.assertEqual(sum(book.status == ReadingStatus.NOT_STARTED for book in books), 1)
        current = books[0]
        self.assertEqual(current.title, 'Example reading item')
        self.assertEqual(current.status, ReadingStatus.READING)
        self.assertTrue(current.current_book)

    def test_current_book_is_first_recommendation_when_capacity_allows(self):
        catalog = self.sample_catalog()
        source = ReadingRecommendationSource(catalog)
        candidate = source.candidates(25, limit=1)[0]
        self.assertEqual(candidate.title, 'Example reading item')
        self.assertEqual(candidate.daily_minutes, 25)
        self.assertEqual(candidate.status, ReadingStatus.READING)
        self.assertNotEqual(candidate.status, ReadingStatus.DONE)


if __name__ == '__main__':
    unittest.main()
