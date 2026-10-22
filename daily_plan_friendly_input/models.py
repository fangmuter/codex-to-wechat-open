from dataclasses import dataclass
from daily_plan_recommendation.models import Energy

@dataclass(frozen=True)
class FriendlyParseResult:
    fixed_events: tuple = ()
    required_tasks: tuple = ()
    carryovers: tuple = ()
    available_minutes: int | None = None
    planning_window: str | None = None
    energy: Energy | None = None
    unresolved_fragments: tuple = ()
    ambiguity_flags: tuple = ()
    # Optional data-only translations; the friendly layer never owns state
    # transitions, confirmation, or roadmap progress.
    user_arranged_tasks: tuple = ()
    review_completed: tuple = ()
    review_unfinished: tuple = ()
