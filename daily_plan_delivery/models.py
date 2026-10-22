from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeliveryItem:
    item_id: str
    source: str
    title: str
    minutes: int
    time_hint: str | None = None
    reason: str | None = None
    roadmap_task_id: str | None = None
    roadmap_stage: str | None = None
    roadmap_milestone: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "item_id": self.item_id,
            "source": self.source,
            "title": self.title,
            "minutes": self.minutes,
            "time_hint": self.time_hint,
            "reason": self.reason,
        }
        if self.roadmap_task_id is not None:
            value["roadmap_task_id"] = self.roadmap_task_id
        if self.roadmap_stage is not None:
            value["roadmap_stage"] = self.roadmap_stage
        if self.roadmap_milestone is not None:
            value["roadmap_milestone"] = self.roadmap_milestone
        return value


@dataclass(frozen=True)
class DeliveryConstraint:
    title: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "detail": self.detail}


@dataclass(frozen=True)
class DeliveryPlan:
    schema_version: int
    plan_id: str
    target_date: str
    source: str
    status: str
    created_at: str
    items: tuple[DeliveryItem, ...]
    fixed_constraints: tuple[DeliveryConstraint, ...]
    execution_order: tuple[str, ...]
    buffer_minutes: int
    warnings: tuple[str, ...]

    def normalized_dict(self) -> dict[str, Any]:
        """Canonical payload used for hashing and deterministic comparisons."""
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "target_date": self.target_date,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "items": [item.to_dict() for item in sorted(self.items, key=lambda x: x.item_id)],
            "fixed_constraints": [
                item.to_dict() for item in sorted(
                    self.fixed_constraints, key=lambda x: (x.title, x.detail)
                )
            ],
            "execution_order": list(self.execution_order),
            "buffer_minutes": self.buffer_minutes,
            "warnings": list(self.warnings),
        }
