"""Read-only preflight checks for the local C1 model gate.

This module never launches Ollama, changes permissions, downloads a model, or
calls the chat generation endpoint.  It only queries the already-running
localhost tags endpoint when a caller explicitly invokes the check.
"""

from dataclasses import dataclass
import json
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


OLLAMA_READY = "READY"
OLLAMA_SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
OLLAMA_MODEL_NOT_INSTALLED = "MODEL_NOT_INSTALLED"
OLLAMA_INVALID_RESPONSE = "INVALID_RESPONSE"
OLLAMA_INVALID_REQUEST = "INVALID_REQUEST"


@dataclass(frozen=True)
class OllamaPreflightReport:
    status: str
    endpoint: str
    model: str
    available_models: tuple[str, ...] = ()
    detail: str | None = None


def _tags_endpoint(endpoint):
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is not None and not (1 <= parsed.port <= 65535)
    ):
        raise ValueError("Ollama endpoint must be an http://127.0.0.1 endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Ollama endpoint must not contain credentials or query data")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunparse(("http", f"127.0.0.1{port}", "/api/tags", "", "", ""))


def _model_names(payload):
    if not isinstance(payload, Mapping) or not isinstance(payload.get("models"), list):
        return None
    names = []
    for item in payload["models"]:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            return None
        name = item["name"].strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def check_ollama(model, *, endpoint="http://127.0.0.1:11434/api/chat", timeout_seconds=1.5, opener=None):
    """Inspect a running local Ollama service without generating text."""
    if not isinstance(model, str) or not model.strip():
        return OllamaPreflightReport(OLLAMA_INVALID_REQUEST, endpoint, "", detail="MODEL_REQUIRED")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return OllamaPreflightReport(OLLAMA_INVALID_REQUEST, endpoint, model.strip(), detail="TIMEOUT_INVALID")
    try:
        tags_endpoint = _tags_endpoint(endpoint)
    except ValueError as error:
        return OllamaPreflightReport(OLLAMA_INVALID_REQUEST, endpoint, model.strip(), detail=str(error))
    request = Request(tags_endpoint, headers={"Accept": "application/json"}, method="GET")
    open_fn = opener or urlopen
    try:
        response = open_fn(request, timeout=float(timeout_seconds))
        with response:
            status = getattr(response, "status", 200)
            if status is not None and int(status) >= 400:
                return OllamaPreflightReport(OLLAMA_SERVICE_UNAVAILABLE, tags_endpoint, model.strip(), detail=f"HTTP_{status}")
            raw = response.read()
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError):
        return OllamaPreflightReport(OLLAMA_SERVICE_UNAVAILABLE, tags_endpoint, model.strip(), detail="LOCAL_SERVICE_UNAVAILABLE")
    names = _model_names(payload)
    if names is None:
        return OllamaPreflightReport(OLLAMA_INVALID_RESPONSE, tags_endpoint, model.strip(), detail="TAGS_RESPONSE_INVALID")
    selected = model.strip()
    if selected not in names:
        return OllamaPreflightReport(OLLAMA_MODEL_NOT_INSTALLED, tags_endpoint, selected, names, "MODEL_NOT_INSTALLED")
    return OllamaPreflightReport(OLLAMA_READY, tags_endpoint, selected, names)
