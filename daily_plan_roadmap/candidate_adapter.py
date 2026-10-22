from .models import RoadmapCandidate


def to_phase17_candidates(candidates):
    """Typed bridge only; Phase 1.7 still owns scoring and selection."""
    return tuple(candidate for candidate in candidates if isinstance(candidate,RoadmapCandidate))


def dedupe_candidates(roadmap_candidates,existing_task_ids=()):
    seen=set(existing_task_ids); result=[]
    for candidate in to_phase17_candidates(roadmap_candidates):
        if candidate.task_id in seen: continue
        seen.add(candidate.task_id); result.append(candidate)
    return tuple(result)
