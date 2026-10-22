from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    NOT_STARTED='NOT_STARTED'; IN_PROGRESS='IN_PROGRESS'; BLOCKED='BLOCKED'; DONE='DONE'


@dataclass(frozen=True)
class RoadmapStage:
    id: str; title: str; order: int; prerequisites: tuple[str,...]=(); status: Status=Status.NOT_STARTED


@dataclass(frozen=True)
class RoadmapMilestone:
    id: str; stage_id: str; title: str; prerequisite_ids: tuple[str,...]=(); status: Status=Status.NOT_STARTED; recommended_task_ids: tuple[str,...]=()


@dataclass(frozen=True)
class RoadmapTask:
    id: str; milestone_id: str; domain: str; title: str; estimated_minutes: int; intensity: str; prerequisite_task_ids: tuple[str,...]=(); completion_criterion: str=''; tags: tuple[str,...]=(); background_prerequisites: tuple[str,...]=()


@dataclass(frozen=True)
class RoadmapCandidate:
    task_id: str; title: str; domain: str; estimated_minutes: int; intensity: str; roadmap_stage: str; roadmap_milestone: str; days_since_progress: int|None; recommendation_origin: str='ROADMAP'; canonical_task_id: str|None = None
    @property
    def candidate_key(self): return 'roadmap:'+self.task_id
