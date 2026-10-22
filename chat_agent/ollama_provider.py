"""Local-only Ollama provider for REVIEW draft generation.

The provider deliberately has no process launcher, cloud fallback, tool
calling, or remote endpoint support.  A model outage becomes an explicit
``needs_user_input`` result so the caller cannot accidentally send anything.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .model import ModelResult


MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"
INVALID_MODEL_REQUEST = "INVALID_MODEL_REQUEST"
USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"


# Ollama supports a JSON schema in the chat endpoint's ``format`` field.  The
# model is still instructed in the prompt because this keeps the contract
# understandable for models that only partially honour the schema.
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "draft": {"type": ["string", "null"]},
        "needs_user_input": {"type": "boolean"},
        "reason": {"type": ["string", "null"]},
    },
    "required": ["draft", "needs_user_input"],
    "additionalProperties": False,
}


def _latency_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


class OllamaProvider:
    """Call an already-running Ollama server on the local machine only."""

    def __init__(
        self,
        model: str,
        *,
        endpoint: str = "http://127.0.0.1:11434/api/chat",
        opener=None,
    ):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("an Ollama model name is required")
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path != "/api/chat"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Ollama endpoint must be http://127.0.0.1:<port>/api/chat")
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            raise ValueError("Ollama endpoint port is invalid")
        self.model = model.strip()
        self.endpoint = endpoint
        # Resolve at construction time so tests and callers can inject a
        # deterministic opener without monkey-patching global urllib state.
        self._opener = opener or urlopen

    @staticmethod
    def _normalise_messages(messages: Sequence[Mapping[str, str]]):
        normalised = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise ValueError("model messages must be mappings")
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"}:
                raise ValueError("unsupported model message role")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("model message content must be non-empty text")
            normalised.append({"role": role, "content": content})
        if not normalised:
            raise ValueError("at least one model message is required")
        return normalised

    @staticmethod
    def _result(reason, started, *, provider="ollama"):
        return ModelResult(
            draft=None,
            needs_user_input=True,
            reason=reason,
            provider=provider,
            latency_ms=_latency_ms(started),
        )

    def _decode(self, payload, started):
        if not isinstance(payload, Mapping):
            return self._result(INVALID_MODEL_OUTPUT, started)
        if payload.get("error"):
            return self._result(MODEL_UNAVAILABLE, started)
        message = payload.get("message")
        if not isinstance(message, Mapping):
            return self._result(INVALID_MODEL_OUTPUT, started)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return self._result(INVALID_MODEL_OUTPUT, started)
        try:
            structured = json.loads(content)
        except (TypeError, ValueError):
            return self._result(INVALID_MODEL_OUTPUT, started)
        if not isinstance(structured, Mapping):
            return self._result(INVALID_MODEL_OUTPUT, started)
        needs_user_input = structured.get("needs_user_input")
        if not isinstance(needs_user_input, bool):
            return self._result(INVALID_MODEL_OUTPUT, started)
        reason = structured.get("reason")
        if reason is not None and not isinstance(reason, str):
            return self._result(INVALID_MODEL_OUTPUT, started)
        if needs_user_input:
            return ModelResult(
                draft=None,
                needs_user_input=True,
                reason=(reason.strip() if isinstance(reason, str) and reason.strip() else USER_INPUT_REQUIRED),
                provider="ollama",
                latency_ms=_latency_ms(started),
            )
        draft = structured.get("draft")
        if not isinstance(draft, str) or not draft.strip():
            return self._result(INVALID_MODEL_OUTPUT, started)
        return ModelResult(
            draft=draft.strip(),
            needs_user_input=False,
            reason=(reason.strip() if isinstance(reason, str) and reason.strip() else None),
            provider="ollama",
            latency_ms=_latency_ms(started),
        )

    def generate(self, messages, *, timeout_seconds: float) -> ModelResult:
        started = time.monotonic()
        try:
            if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be positive")
            normalised = self._normalise_messages(messages)
        except ValueError:
            return self._result(INVALID_MODEL_REQUEST, started)

        body = json.dumps(
            {
                "model": self.model,
                "messages": normalised,
                "stream": False,
                "format": REPLY_SCHEMA,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = self._opener(request, timeout=float(timeout_seconds))
            with response:
                status = getattr(response, "status", 200)
                raw = response.read()
            if status is not None and int(status) >= 400:
                return self._result(MODEL_UNAVAILABLE, started)
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError):
            return self._result(MODEL_UNAVAILABLE, started)
        return self._decode(payload, started)
