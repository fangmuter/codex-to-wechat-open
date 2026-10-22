from dataclasses import dataclass, field
from enum import Enum
from daily_plan_recommendation.models import Energy, DomainProgress, ProjectSignal


class Source(str, Enum):
    USER_FIXED="USER_FIXED"
    USER_REQUIRED="USER_REQUIRED"
    CARRYOVER="CARRYOVER"
    USER_ARRANGED="USER_ARRANGED"
    SYSTEM_RECOMMENDED="SYSTEM_RECOMMENDED"


SOURCE_PRIORITY={Source.USER_FIXED:1,Source.USER_REQUIRED:2,Source.CARRYOVER:3,Source.USER_ARRANGED:4,Source.SYSTEM_RECOMMENDED:5}


@dataclass(frozen=True)
class FusionItemInput:
    title: str
    source: Source
    estimated_minutes: int
    capacity_key: str | None = None
    consumes_available_capacity: bool = True
    fixed_time: str | None = None
    preferred_period: str | None = None
    kind: str = "user_arranged"
    unavailable_until: str | None = None


@dataclass(frozen=True)
class FusionRequest:
    date: str
    planning_window: str
    available_minutes: int
    energy: Energy
    items: tuple[FusionItemInput, ...] = ()
    project_signals: tuple[ProjectSignal, ...] = ()
    progress: tuple[DomainProgress, ...] = ()
    recently_recommended_domains: tuple[str, ...] = ()
    roadmap_candidates: tuple = ()
    # Optional data-only source.  Phase 1.7 remains the sole scorer for
    # roadmap/static recommendations; reading is merged as a light item by
    # Fusion only when the already-remaining capacity can fit it.
    reading_source: object | None = None


@dataclass(frozen=True)
class PlanItem:
    title: str
    source: Source
    estimated_minutes: int
    priority: int
    capacity_key: str | None = None
    fixed_time: str | None = None
    preferred_period: str | None = None
    reason: str | None = None
    recommendation_origin: str | None = None
    roadmap_task_id: str | None = None
    roadmap_stage: str | None = None
    roadmap_milestone: str | None = None
    deferred: bool = False
    unavailable_until: str | None = None
    consumes_available_capacity: bool = True


@dataclass(frozen=True)
class PlanDraft:
    date: str
    planning_window: str
    available_minutes: int
    energy: Energy
    items: tuple[PlanItem, ...]
    total_committed_minutes: int
    total_recommended_minutes: int
    remaining_buffer_minutes: int
    overload: bool
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_breakdown: tuple[tuple[str,int], ...] = ()
    confirmation_required: bool = True
    state: str = "DRAFT_READY"
    deferred_items: tuple[PlanItem, ...] = ()


def plan_draft_to_dict(draft: PlanDraft) -> dict:
    """Stable JSON payload for the canonical fused snapshot."""
    def item(value: PlanItem) -> dict:
        return {
            "title": value.title,
            "source": value.source.value,
            "estimated_minutes": value.estimated_minutes,
            "priority": value.priority,
            "capacity_key": value.capacity_key,
            "fixed_time": value.fixed_time,
            "preferred_period": value.preferred_period,
            "reason": value.reason,
            "recommendation_origin": value.recommendation_origin,
            "roadmap_task_id": value.roadmap_task_id,
            "roadmap_stage": value.roadmap_stage,
            "roadmap_milestone": value.roadmap_milestone,
            "deferred": value.deferred,
            "unavailable_until": value.unavailable_until,
            "consumes_available_capacity": value.consumes_available_capacity,
        }
    return {
        "date": draft.date,
        "planning_window": draft.planning_window,
        "available_minutes": draft.available_minutes,
        "energy": draft.energy.value,
        "items": [item(value) for value in draft.items],
        "total_committed_minutes": draft.total_committed_minutes,
        "total_recommended_minutes": draft.total_recommended_minutes,
        "remaining_buffer_minutes": draft.remaining_buffer_minutes,
        "overload": draft.overload,
        "conflicts": list(draft.conflicts),
        "warnings": list(draft.warnings),
        "source_breakdown": [list(value) for value in draft.source_breakdown],
        "confirmation_required": draft.confirmation_required,
        "state": draft.state,
        "deferred_items": [item(value) for value in draft.deferred_items],
    }


def plan_draft_from_dict(payload: dict) -> PlanDraft:
    def item(value: dict) -> PlanItem:
        return PlanItem(
            value["title"], Source(value["source"]), int(value["estimated_minutes"]),
            int(value["priority"]), value.get("capacity_key"), value.get("fixed_time"),
            value.get("preferred_period"), value.get("reason"),
            value.get("recommendation_origin"), value.get("roadmap_task_id"),
            value.get("roadmap_stage"), value.get("roadmap_milestone"),
            bool(value.get("deferred", False)), value.get("unavailable_until"),
            bool(value.get("consumes_available_capacity", True)),
        )
    return PlanDraft(
        payload["date"], payload["planning_window"], int(payload["available_minutes"]),
        Energy(payload["energy"]), tuple(item(value) for value in payload.get("items", ())),
        int(payload["total_committed_minutes"]), int(payload["total_recommended_minutes"]),
        int(payload["remaining_buffer_minutes"]), bool(payload["overload"]),
        tuple(payload.get("conflicts", ())), tuple(payload.get("warnings", ())),
        tuple((str(value[0]), int(value[1])) for value in payload.get("source_breakdown", ())),
        bool(payload.get("confirmation_required", True)), payload.get("state", "DRAFT_READY"),
        tuple(item(value) for value in payload.get("deferred_items", ())),
    )
