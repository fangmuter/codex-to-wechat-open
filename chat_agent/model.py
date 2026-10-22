"""Provider boundary for the local-model POC."""

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ModelResult:
    draft: str | None
    needs_user_input: bool = False
    reason: str | None = None
    provider: str = "unknown"
    latency_ms: int | None = None


class ModelProvider(Protocol):
    def generate(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        timeout_seconds: float,
    ) -> ModelResult:
        """Generate a draft or explicitly request user input."""
