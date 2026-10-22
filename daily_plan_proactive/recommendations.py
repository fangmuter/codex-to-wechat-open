from typing import Protocol

class RecommendationProvider(Protocol):
    enabled: bool
    def recommend(self, context: dict) -> list[dict]: ...

class DisabledRecommendationProvider:
    enabled = False
    def recommend(self, context: dict) -> list[dict]:
        del context
        return []
