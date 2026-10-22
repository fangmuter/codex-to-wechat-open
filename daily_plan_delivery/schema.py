"""Strict, non-executing validation for confirmed web delivery plans."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from .models import DeliveryConstraint, DeliveryItem, DeliveryPlan


class DeliverySchemaError(ValueError):
    pass


class DeliveryReadError(OSError):
    """The inbox file could not be read at this point in time.

    DriveFS can expose a directory entry before its contents are hydrated.
    Keeping this separate from :class:`DeliverySchemaError` lets the bounded
    production poller retry the read without treating a transient I/O failure
    as a permanently malformed plan.
    """

    pass


ALLOWED_SOURCES = {
    "USER_REQUIRED", "USER_ARRANGED", "CARRYOVER", "ROADMAP",
    "SYSTEM_RECOMMENDED", "READING",
}
TOP_LEVEL_KEYS = {
    "schema_version", "plan_id", "target_date", "source", "status",
    "created_at", "items", "fixed_constraints", "execution_order",
    "buffer_minutes", "warnings",
}
ITEM_KEYS = {
    "item_id", "source", "title", "minutes", "time_hint", "reason",
    "roadmap_task_id", "roadmap_stage", "roadmap_milestone",
}
CONSTRAINT_KEYS = {"title", "detail"}
MAX_TEXT = 500
MAX_ID = 160

_FORBIDDEN_KEY_PARTS = {
    "credential", "recipient", "cookie", "token", "context", "reply_context",
    "raw_message", "chat_history", "shell", "command", "executable", "path",
}


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
                raise DeliverySchemaError("forbidden delivery field")
            _reject_forbidden_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_keys(nested)


def _text(value: Any, name: str, *, required: bool = False, limit: int = MAX_TEXT) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DeliverySchemaError(f"invalid {name}")
    return value.strip()


def _date(value: Any) -> str:
    if not isinstance(value, str):
        raise DeliverySchemaError("invalid target_date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise DeliverySchemaError("invalid target_date") from exc


def _created_at(value: Any) -> str:
    text = _text(value, "created_at", required=True, limit=80)
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise DeliverySchemaError("invalid created_at") from exc
    return text


def _minutes(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 24 * 60:
        raise DeliverySchemaError(f"invalid {name}")
    return value


def validate_delivery_payload(payload: Any) -> DeliveryPlan:
    """Validate and normalize one JSON object; never writes or executes it."""
    if not isinstance(payload, dict):
        raise DeliverySchemaError("delivery payload must be an object")
    _reject_forbidden_keys(payload)
    if set(payload) - TOP_LEVEL_KEYS:
        raise DeliverySchemaError("unknown delivery field")
    if payload.get("schema_version") != 1:
        raise DeliverySchemaError("unsupported schema_version")
    plan_id = _text(payload.get("plan_id"), "plan_id", required=True, limit=MAX_ID)
    target_date = _date(payload.get("target_date"))
    if payload.get("source") != "chatgpt_web":
        raise DeliverySchemaError("unsupported delivery source")
    if payload.get("status") != "CONFIRMED":
        raise DeliverySchemaError("delivery plan is not CONFIRMED")
    created_at = _created_at(payload.get("created_at"))

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise DeliverySchemaError("items must be a list")
    items: list[DeliveryItem] = []
    seen_ids: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) - ITEM_KEYS:
            raise DeliverySchemaError("malformed delivery item")
        item_id = _text(raw.get("item_id"), "item_id", required=True, limit=MAX_ID)
        if item_id in seen_ids:
            raise DeliverySchemaError("duplicate item_id")
        source = raw.get("source")
        if source not in ALLOWED_SOURCES:
            raise DeliverySchemaError("invalid item source")
        title = _text(raw.get("title"), "title", required=True)
        minutes = _minutes(raw.get("minutes"), "minutes")
        time_hint = _text(raw.get("time_hint"), "time_hint")
        reason = _text(raw.get("reason"), "reason")
        roadmap_task_id = _text(raw.get("roadmap_task_id"), "roadmap_task_id", limit=MAX_ID)
        roadmap_stage = _text(raw.get("roadmap_stage"), "roadmap_stage", limit=80)
        roadmap_milestone = _text(raw.get("roadmap_milestone"), "roadmap_milestone", limit=80)
        seen_ids.add(item_id)
        items.append(DeliveryItem(
            item_id, source, title, minutes, time_hint, reason,
            roadmap_task_id, roadmap_stage, roadmap_milestone,
        ))

    raw_constraints = payload.get("fixed_constraints")
    if not isinstance(raw_constraints, list):
        raise DeliverySchemaError("fixed_constraints must be a list")
    constraints: list[DeliveryConstraint] = []
    for raw in raw_constraints:
        if not isinstance(raw, dict) or set(raw) - CONSTRAINT_KEYS:
            raise DeliverySchemaError("malformed fixed constraint")
        constraints.append(DeliveryConstraint(
            _text(raw.get("title"), "constraint title", required=True),
            _text(raw.get("detail"), "constraint detail", required=True),
        ))

    execution = payload.get("execution_order")
    if not isinstance(execution, list) or any(not isinstance(value, str) for value in execution):
        raise DeliverySchemaError("invalid execution_order")
    if len(set(execution)) != len(execution) or any(value not in seen_ids for value in execution):
        raise DeliverySchemaError("execution_order references unknown or duplicate item")

    raw_warnings = payload.get("warnings")
    if not isinstance(raw_warnings, list) or any(not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT for value in raw_warnings):
        raise DeliverySchemaError("invalid warnings")
    return DeliveryPlan(
        1, plan_id, target_date, "chatgpt_web", "CONFIRMED", created_at,
        tuple(items), tuple(constraints), tuple(execution),
        _minutes(payload.get("buffer_minutes"), "buffer_minutes"),
        tuple(value.strip() for value in raw_warnings),
    )


def load_delivery_json(path) -> DeliveryPlan:
    """Read one explicit path and validate it; content is never executed."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DeliverySchemaError("invalid delivery JSON") from exc
    except OSError as exc:
        raise DeliveryReadError("delivery file unreadable") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeliverySchemaError("invalid delivery JSON") from exc
    return validate_delivery_payload(payload)
