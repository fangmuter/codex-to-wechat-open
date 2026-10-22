from dataclasses import dataclass, field
from enum import Enum


class Energy(str, Enum):
    LOW="LOW"
    NORMAL="NORMAL"
    HIGH="HIGH"


SYSTEM_RECOMMENDED="SYSTEM_RECOMMENDED"


@dataclass(frozen=True)
class Commitment:
    title: str
    estimated_minutes: int
    kind: str  # fixed_event | deadline | must_do | carryover | self_arranged
    consumes_available_capacity: bool = True
    capacity_key: str | None = None  # reuse for one item represented by multiple tags


@dataclass(frozen=True)
class ProjectSignal:
    domain: str
    urgency: int = 0  # 0..3; current project goal proximity
    course_related: bool = False


@dataclass(frozen=True)
class DomainProgress:
    domain: str
    days_since_progress: int = 0


@dataclass(frozen=True)
class RecommendationRequest:
    target_date: str
    available_minutes: int  # net minutes inside the user's stated planning window
    energy: Energy
    commitments: tuple[Commitment, ...] = ()
    project_signals: tuple[ProjectSignal, ...] = ()
    progress: tuple[DomainProgress, ...] = ()
    recently_recommended_domains: tuple[str, ...] = ()
    roadmap_candidates: tuple = ()


@dataclass(frozen=True)
class Recommendation:
    title: str
    domain: str
    estimated_minutes: int
    priority: int
    reason: str
    source: str = SYSTEM_RECOMMENDED
    intensity: str = "LIGHT"
    recommendation_origin: str = "STATIC"
    roadmap_task_id: str | None = None
    roadmap_stage: str | None = None
    roadmap_milestone: str | None = None


@dataclass(frozen=True)
class RecommendationResult:
    reserved_minutes: int
    remaining_minutes: int
    recommendations: tuple[Recommendation, ...] = ()
