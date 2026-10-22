"""Optional deterministic reading source; no catalog means no books are invented."""

from .source import ReadingBook, ReadingRecommendationSource, ReadingStatus

__all__ = ['ReadingBook', 'ReadingRecommendationSource', 'ReadingStatus']
