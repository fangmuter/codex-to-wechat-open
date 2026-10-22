"""No-send benchmark harness for choosing a local model after C1."""

from dataclasses import dataclass
import time

from .model import ModelResult


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    messages: tuple


@dataclass(frozen=True)
class BenchmarkResult:
    case_name: str
    status: str
    latency_ms: int | None
    draft_chars: int
    reason: str | None = None


@dataclass(frozen=True)
class BenchmarkReport:
    results: tuple
    total_cases: int
    successful_drafts: int
    needs_user_input: int
    failures: int
    p95_latency_ms: int | None

    @property
    def draft_success_rate(self):
        return self.successful_drafts / self.total_cases if self.total_cases else 0.0


DEFAULT_CASES = (
    BenchmarkCase(
        "short_ack",
        (
            {"role": "system", "content": "只输出符合约定 JSON 的草稿。"},
            {"role": "user", "content": "收到，谢谢。"},
        ),
    ),
    BenchmarkCase(
        "missing_personal_fact",
        (
            {"role": "system", "content": "缺少个人事实时必须 needs_user_input=true。"},
            {"role": "user", "content": "你明天几点到？"},
        ),
    ),
    BenchmarkCase(
        "multi_turn_burst",
        (
            {"role": "system", "content": "只生成审核草稿，不发送。"},
            {"role": "user", "content": "我刚到家"},
            {"role": "user", "content": "晚点再聊也行"},
        ),
    ),
)


def _percentile(values, percentile):
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, int((len(values) - 1) * percentile)))
    return int(values[index])


def run_benchmark(provider, cases=DEFAULT_CASES, *, timeout_seconds=20):
    """Run provider-only cases; no transport or draft store is touched."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    results = []
    for case in cases:
        started = time.monotonic()
        try:
            result = provider.generate(case.messages, timeout_seconds=timeout_seconds)
            elapsed = max(0, int(round((time.monotonic() - started) * 1000)))
            if not isinstance(result, ModelResult):
                results.append(BenchmarkResult(case.name, "INVALID_PROVIDER_RESULT", elapsed, 0, "INVALID_PROVIDER_RESULT"))
            elif result.needs_user_input:
                results.append(BenchmarkResult(case.name, "NEEDS_USER_INPUT", result.latency_ms or elapsed, 0, result.reason))
            elif isinstance(result.draft, str) and result.draft.strip():
                results.append(BenchmarkResult(case.name, "DRAFT", result.latency_ms or elapsed, len(result.draft.strip()), result.reason))
            else:
                results.append(BenchmarkResult(case.name, "INVALID_MODEL_OUTPUT", result.latency_ms or elapsed, 0, result.reason))
        except Exception:
            elapsed = max(0, int(round((time.monotonic() - started) * 1000)))
            results.append(BenchmarkResult(case.name, "PROVIDER_ERROR", elapsed, 0, "PROVIDER_ERROR"))
    latencies = [item.latency_ms for item in results if item.latency_ms is not None]
    return BenchmarkReport(
        results=tuple(results),
        total_cases=len(results),
        successful_drafts=sum(item.status == "DRAFT" for item in results),
        needs_user_input=sum(item.status == "NEEDS_USER_INPUT" for item in results),
        failures=sum(item.status not in {"DRAFT", "NEEDS_USER_INPUT"} for item in results),
        p95_latency_ms=_percentile(latencies, 0.95),
    )
