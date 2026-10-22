"""Narrow deterministic adapter from an existing Phase 1.4 draft to fusion input."""
import json

from daily_plan_mock.planner.models import State
from .models import FusionItemInput, FusionRequest, Source


def _clock_range(value):
    import re
    match=re.fullmatch(r"(\d\d):(\d\d)\s*[-–]\s*(\d\d):(\d\d)", value or "")
    if not match: return None
    return int(match[1])*60+int(match[2]), int(match[3])*60+int(match[4])


def _capacity_consuming(value, source, planning_window):
    explicit=value.get("consumes_available_capacity") if isinstance(value,dict) else None
    if explicit is not None: return bool(explicit)
    if source == Source.USER_FIXED and isinstance(value,dict):
        fixed=_clock_range(value.get("fixed_time")); window=_clock_range(planning_window)
        if fixed and window and (fixed[1] <= window[0] or fixed[0] >= window[1]):
            return False
        if value.get("unavailable_until"): return False
    return True


def _item(value, source, default_kind, planning_window=None):
    if isinstance(value, str):
        return FusionItemInput(value,source,0,consumes_available_capacity=False,kind=default_kind)
    return FusionItemInput(
        value["title"], source, int(value.get("estimated_minutes",0)),
        value.get("capacity_key"), _capacity_consuming(value,source,planning_window),
        value.get("fixed_time"), value.get("preferred_period"), value.get("kind",default_kind),
        value.get("unavailable_until"),
    )


def fusion_request_from_phase14(session, *, planning_window, available_minutes, energy, user_arranged=(), project_signals=(), progress=(), recently_recommended_domains=(), roadmap_candidates=(), reading_source=None):
    """Adapt only a DRAFT_READY session; it never changes planner state or storage."""
    if session is None or session["state"] != State.DRAFT_READY.value or not session["draft_json"]:
        raise ValueError("Phase 1.4 session must be DRAFT_READY with a draft")
    draft=json.loads(session["draft_json"])
    items=[]
    items.extend(_item(value,Source.USER_REQUIRED,"must_do",planning_window) for value in draft.get("must_do",()))
    items.extend(_item(value,Source.USER_FIXED,"fixed_event",planning_window) for value in draft.get("fixed_events",()))
    items.extend(_item(value,Source.CARRYOVER,"carryover",planning_window) for value in draft.get("optional",()))
    items.extend(_item(value,Source.USER_ARRANGED,"self_arranged",planning_window) for value in user_arranged)
    return FusionRequest(session["target_date"],planning_window,int(available_minutes),energy,tuple(items),tuple(project_signals),tuple(progress),tuple(recently_recommended_domains),tuple(roadmap_candidates),reading_source)
