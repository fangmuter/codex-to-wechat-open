"""Translate friendly parser output into existing Phase 1.4 operation payloads."""
from dataclasses import dataclass, field
from .models import FriendlyParseResult

@dataclass(frozen=True)
class FriendlyPlannerOperations:
    review: dict
    must: list
    fixed: list
    capacity: dict | None
    arranged: list = field(default_factory=list)

def to_phase14_operations(result: FriendlyParseResult) -> FriendlyPlannerOperations:
    """Pure, deterministic adapter; it does not mutate planner state."""
    review = {
        "completed": list(result.review_completed),
        "unfinished": list(result.review_unfinished),
        "carryover": [x["title"] for x in result.carryovers],
    }
    fixed = [dict(x) for x in result.fixed_events]
    must = [dict(x) for x in result.required_tasks]
    capacity = None
    if result.available_minutes is not None or result.energy is not None:
        energy_label = {"LOW": "低", "NORMAL": "中", "HIGH": "高"}
        capacity = {"available_minutes": result.available_minutes,
                    "energy": energy_label[result.energy.value] if result.energy else None,
                    "planning_window": result.planning_window}
    return FriendlyPlannerOperations(review, must, fixed, capacity,
                                     [dict(x) for x in result.user_arranged_tasks])
