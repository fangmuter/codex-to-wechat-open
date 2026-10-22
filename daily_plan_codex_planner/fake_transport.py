import json


class FakePlannerTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def plan(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.responses.pop(0) if self.responses else json.dumps({
            "intent": "extract_plan_fields", "must_do": [], "fixed_events": [],
            "carryover": [], "available_minutes": None, "energy": None,
            "missing_fields": ["must_do"], "overload": False,
            "suggested_blocks": [], "clarifying_question": "请补充必须完成的事项。",
        }, ensure_ascii=False)
