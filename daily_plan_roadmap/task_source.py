from .candidate_adapter import to_phase17_candidates


class RoadmapTaskSource:
    """Eligibility source; it never scores or force-inserts recommendations."""
    def __init__(self,store): self.store=store
    def candidates(self,today=None,limit=4): return to_phase17_candidates(self.store.next_tasks(today,limit))
