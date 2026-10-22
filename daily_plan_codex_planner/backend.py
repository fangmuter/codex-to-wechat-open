"""Backend-neutral planning seam.  The production default is intentionally off."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PlannerAIBackend(Protocol):
    """Optional future planner backend; implementations are never auto-enabled."""

    def plan(self, request: dict) -> str: ...


class DisabledPlannerAIBackend:
    """Fail closed so callers retain the deterministic structured planner."""

    def plan(self, request: dict) -> str:
        del request
        raise RuntimeError("backend_disabled")


DEFAULT_PLANNER_AI_BACKEND: PlannerAIBackend = DisabledPlannerAIBackend()
