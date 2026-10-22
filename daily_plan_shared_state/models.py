from dataclasses import dataclass, field

@dataclass(frozen=True)
class SharedStateDocument:
    schema_version: int
    generated_at: str
    latest_confirmed_plan: dict | None = None
    recent_plan_history: tuple[dict, ...] = ()
    latest_review_summary: dict | None = None
    roadmap_summary: dict = field(default_factory=dict)

    def to_dict(self):
        return {"schema_version": self.schema_version, "generated_at": self.generated_at,
                "latest_confirmed_plan": self.latest_confirmed_plan,
                "recent_plan_history": list(self.recent_plan_history),
                "latest_review_summary": self.latest_review_summary,
                "roadmap_summary": self.roadmap_summary}
