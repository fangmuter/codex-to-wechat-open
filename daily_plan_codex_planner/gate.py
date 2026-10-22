"""Local AI call gate and deterministic fallback.  No WeChat transport is imported."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .backend import DEFAULT_PLANNER_AI_BACKEND, PlannerAIBackend
from .policy import (ALLOWED_MODEL, LOCAL_COMMANDS, MAX_CURRENT_USER_MESSAGE_CHARS,
                     MAX_MODEL_CALLS_PER_SESSION, MAX_MODEL_INPUT_CHARS,
                     MAX_MODEL_OUTPUT_CHARS, REASONING_EFFORT)
from .schema import PlannerData, SchemaError, validate

FALLBACK_TEXT = "AI 辅助暂时不可用，已切换到结构化计划模式。"


PlannerTransport = PlannerAIBackend


@dataclass
class Decision:
    mode: str
    reason: str
    data: PlannerData | None = None
    calls_used: int = 0
    fallback_text: str | None = None


@dataclass
class SessionGate:
    allowed_model: str | None = ALLOWED_MODEL
    reasoning_effort: str = REASONING_EFFORT
    calls_used: int = 0
    _commands: tuple[str, ...] = field(default=LOCAL_COMMANDS)

    def needs_ai(self, text: str) -> bool:
        return not any(text.strip().startswith(command) for command in self._commands)

    def _fallback(self, reason: str) -> Decision:
        return Decision("fallback", reason, calls_used=self.calls_used, fallback_text=FALLBACK_TEXT)

    def request(self, transport: PlannerTransport | None, model: str | None, reasoning_effort: str | None,
                target_date: str, current_state: str, current_user_message: str,
                context: dict | None = None) -> Decision:
        if not self.needs_ai(current_user_message):
            return Decision("local", "structured_or_control_command", calls_used=self.calls_used)
        if len(current_user_message) > MAX_CURRENT_USER_MESSAGE_CHARS:
            return self._fallback("current_user_message_too_long")
        if model != self.allowed_model or model is None:
            return self._fallback("model_not_allowlisted")
        if reasoning_effort != self.reasoning_effort:
            return self._fallback("reasoning_not_allowlisted")
        if self.calls_used >= MAX_MODEL_CALLS_PER_SESSION:
            return self._fallback("AI_BUDGET_EXHAUSTED")
        payload = {"target_date": target_date, "current_state": current_state,
                   "current_user_message": current_user_message, **(context or {})}
        if len(str(payload)) > MAX_MODEL_INPUT_CHARS:
            return self._fallback("model_input_too_long")
        self.calls_used += 1
        try:
            raw = (transport or DEFAULT_PLANNER_AI_BACKEND).plan({"model": model, "reasoning_effort": reasoning_effort,
                                  "tools": [], "payload": payload})
        except TimeoutError:
            return self._fallback("codex_timeout")
        except PermissionError:
            return self._fallback("auth_unavailable")
        except RuntimeError as exc:
            return self._fallback(str(exc) if str(exc) in {"model_unavailable", "quota_unavailable"} else "codex_unavailable")
        if len(raw) > MAX_MODEL_OUTPUT_CHARS:
            return self._fallback("model_output_too_long")
        try:
            return Decision("ai", "validated", validate(__import__("json").loads(raw)), self.calls_used)
        except (__import__("json").JSONDecodeError, SchemaError):
            return self._fallback("invalid_model_schema")
