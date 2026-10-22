"""Narrow, data-only reading catalog contract for a future integration.

The production project deliberately ships no fabricated 20-book list.  A
caller must provide an explicit JSON catalog path; an absent path yields an
empty source and a deterministic data-required state.
"""
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


DEFAULT_DAILY_MINUTES = 25
MIN_DAILY_MINUTES = 20
MAX_DAILY_MINUTES = 30


class ReadingStatus(str, Enum):
    NOT_STARTED = 'NOT_STARTED'
    READING = 'READING'
    DONE = 'DONE'


@dataclass(frozen=True)
class ReadingBook:
    book_id: str
    title: str
    queue_order: int
    status: ReadingStatus = ReadingStatus.NOT_STARTED
    current_book: bool = False
    daily_minutes: int = 25
    last_read_at: str | None = None

    @property
    def source(self):
        """Stable source marker for downstream fusion metadata."""
        return 'READING'


class ReadingRecommendationSource:
    """Load a caller-supplied typed catalog; never infer or fabricate."""
    def __init__(self, catalog_path=None):
        self.catalog_path = Path(catalog_path) if catalog_path else None
        self._current_book_id = None
        self._default_daily_minutes = DEFAULT_DAILY_MINUTES
        self._books = self._load() if self.catalog_path else ()

    def _load(self):
        if not self.catalog_path.is_file():
            raise FileNotFoundError(str(self.catalog_path))
        payload = json.loads(self.catalog_path.read_text(encoding='utf-8'))
        if isinstance(payload, dict):
            if 'schema_version' in payload and payload['schema_version'] != 1:
                raise ValueError('reading catalog schema_version must be 1')
            rows = payload.get('books')
            current_book_id = payload.get('current_book_id')
            if current_book_id is not None and (
                not isinstance(current_book_id, str) or not current_book_id.strip()
            ):
                raise ValueError('current_book_id must be a non-empty string or null')
            default_minutes = payload.get('default_daily_minutes', DEFAULT_DAILY_MINUTES)
        else:
            # Keep the small legacy {"books": [...]} / list contract readable.
            rows = payload
            current_book_id = None
            default_minutes = DEFAULT_DAILY_MINUTES
        if not isinstance(rows, list):
            raise ValueError('reading catalog must contain a books list')
        default_minutes = self._validate_minutes(default_minutes, 'default_daily_minutes')
        books = []
        seen = set()
        seen_orders = set()
        explicit_current_ids = []
        for row in rows:
            if not isinstance(row, dict) or not {'book_id', 'title', 'queue_order'} <= row.keys():
                raise ValueError('reading catalog row is malformed')
            book_id = row['book_id']
            title = row['title']
            queue_order = row['queue_order']
            if not isinstance(book_id, str) or not book_id.strip() or book_id in seen:
                raise ValueError('reading book identity must be unique')
            if not isinstance(title, str) or not title.strip():
                raise ValueError('reading title must be a non-empty string')
            if isinstance(queue_order, bool) or not isinstance(queue_order, int) or queue_order < 1:
                raise ValueError('reading queue_order must be a positive integer')
            if queue_order in seen_orders:
                raise ValueError('reading queue_order must be unique')
            try:
                status = ReadingStatus(row.get('status', ReadingStatus.NOT_STARTED.value))
            except (TypeError, ValueError) as exc:
                raise ValueError('reading status is invalid') from exc
            minutes = self._validate_minutes(
                row.get('daily_minutes', default_minutes), 'reading daily_minutes'
            )
            current_flag = row.get('current_book', False)
            if not isinstance(current_flag, bool):
                raise ValueError('current_book must be boolean')
            if current_flag:
                explicit_current_ids.append(book_id)
            seen.add(book_id)
            seen_orders.add(queue_order)
            books.append(ReadingBook(book_id, title, queue_order, status,
                                     current_flag, minutes,
                                     row.get('last_read_at')))
        if len(explicit_current_ids) > 1:
            raise ValueError('reading catalog may have only one current book')
        if current_book_id is not None:
            if current_book_id not in seen:
                raise ValueError('current_book_id must reference a listed book')
            if explicit_current_ids and explicit_current_ids[0] != current_book_id:
                raise ValueError('current_book_id conflicts with current_book')
            current = next(book for book in books if book.book_id == current_book_id)
            if current.status != ReadingStatus.READING:
                raise ValueError('current book must have READING status')
        elif explicit_current_ids:
            current_book_id = explicit_current_ids[0]
        self._current_book_id = current_book_id
        self._default_daily_minutes = default_minutes
        if current_book_id is not None:
            books = [
                ReadingBook(book.book_id, book.title, book.queue_order, book.status,
                            book.book_id == current_book_id, book.daily_minutes,
                            book.last_read_at)
                for book in books
            ]
        return tuple(sorted(books, key=lambda book: (book.queue_order, book.book_id)))

    @staticmethod
    def _validate_minutes(value, field):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f'{field} must be an integer')
        if value < MIN_DAILY_MINUTES or value > MAX_DAILY_MINUTES:
            raise ValueError(f'{field} must be {MIN_DAILY_MINUTES}..{MAX_DAILY_MINUTES}')
        return value

    @property
    def data_required(self):
        return self.catalog_path is None

    @property
    def current_book_id(self):
        return self._current_book_id

    @property
    def default_daily_minutes(self):
        return self._default_daily_minutes

    def books(self):
        return self._books

    def candidates(self, available_minutes, limit=1):
        if available_minutes < 20:
            return ()
        eligible = [book for book in self._books
                    if book.status != ReadingStatus.DONE and book.daily_minutes <= available_minutes]
        if self._current_book_id is not None:
            eligible.sort(key=lambda book: (
                0 if book.book_id == self._current_book_id else 1,
                book.queue_order,
                book.book_id,
            ))
        return tuple(eligible)[:max(0, limit)]
