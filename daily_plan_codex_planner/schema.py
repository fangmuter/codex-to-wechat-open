"""Strict, local validation for untrusted planner output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class SchemaError(ValueError):
    pass


FIELDS = {
    "intent", "must_do", "fixed_events", "carryover", "available_minutes",
    "energy", "missing_fields", "overload", "suggested_blocks",
    "clarifying_question",
}
INTENTS = {"extract_plan_fields", "assess_completeness", "suggest_blocks"}
ENERGIES = {None, "low", "medium", "high"}


@dataclass(frozen=True)
class PlannerData:
    intent: str
    must_do: list[dict[str, Any]]
    fixed_events: list[dict[str, Any]]
    carryover: list[str]
    available_minutes: int | None
    energy: str | None
    missing_fields: list[str]
    overload: bool
    suggested_blocks: list[dict[str, Any]]
    clarifying_question: str | None


def _text(value: Any, label: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise SchemaError(f"invalid {label}")
    return value.strip()


def _items(value: Any, label: str, max_count: int, duration: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > max_count:
        raise SchemaError(f"invalid {label}")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"title", "estimated_minutes", "start", "end"}:
            raise SchemaError(f"invalid {label} item")
        clean = {"title": _text(item.get("title"), f"{label}.title", 160)}
        if "estimated_minutes" in item:
            minutes = item["estimated_minutes"]
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
                raise SchemaError(f"invalid {label}.estimated_minutes")
            clean["estimated_minutes"] = minutes
        elif duration:
            raise SchemaError(f"missing {label}.estimated_minutes")
        for key in ("start", "end"):
            if key in item:
                clean[key] = _text(item[key], f"{label}.{key}", 20)
        result.append(clean)
    return result


def validate(value: Any) -> PlannerData:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise SchemaError("unknown, missing, or forbidden planner fields")
    intent = value["intent"]
    if intent not in INTENTS:
        raise SchemaError("invalid intent")
    must_do = _items(value["must_do"], "must_do", 3, duration=True)
    fixed_events = _items(value["fixed_events"], "fixed_events", 12)
    suggested_blocks = _items(value["suggested_blocks"], "suggested_blocks", 12)
    carryover = value["carryover"]
    if not isinstance(carryover, list) or len(carryover) > 12:
        raise SchemaError("invalid carryover")
    carryover = [_text(item, "carryover", 160) for item in carryover]
    available = value["available_minutes"]
    if available is not None and (isinstance(available, bool) or not isinstance(available, int) or not 1 <= available <= 1440):
        raise SchemaError("invalid available_minutes")
    if value["energy"] not in ENERGIES:
        raise SchemaError("invalid energy")
    missing = value["missing_fields"]
    if not isinstance(missing, list) or len(missing) > 8:
        raise SchemaError("invalid missing_fields")
    missing = [_text(item, "missing_fields", 80) for item in missing]
    if not isinstance(value["overload"], bool):
        raise SchemaError("invalid overload")
    question = value["clarifying_question"]
    if question is not None:
        question = _text(question, "clarifying_question")
    if sum(item["estimated_minutes"] for item in must_do) > 1440:
        raise SchemaError("impossible must_do duration")
    return PlannerData(intent, must_do, fixed_events, carryover, available, value["energy"], missing, value["overload"], suggested_blocks, question)
