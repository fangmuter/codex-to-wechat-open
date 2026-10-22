import unittest

from chat_agent.benchmark import BenchmarkCase, run_benchmark
from chat_agent.model import ModelResult


class _Provider:
    def __init__(self):
        self.calls = []

    def generate(self, messages, *, timeout_seconds):
        self.calls.append((messages, timeout_seconds))
        if messages[-1]["content"] == "需要补充":
            return ModelResult(None, needs_user_input=True, reason="MISSING_FACT", latency_ms=12)
        return ModelResult("安全草稿", latency_ms=8)


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_only_calls_provider_and_classifies_results(self):
        provider = _Provider()
        report = run_benchmark(
            provider,
            (
                BenchmarkCase("ok", ({"role": "user", "content": "你好"},)),
                BenchmarkCase("needs_input", ({"role": "user", "content": "需要补充"},)),
            ),
            timeout_seconds=3,
        )
        self.assertEqual(report.total_cases, 2)
        self.assertEqual(report.successful_drafts, 1)
        self.assertEqual(report.needs_user_input, 1)
        self.assertEqual(report.failures, 0)
        self.assertEqual(report.draft_success_rate, 0.5)
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(all(call[1] == 3 for call in provider.calls))

    def test_provider_error_is_failure_but_does_not_escape(self):
        class Broken:
            def generate(self, *_args, **_kwargs):
                raise RuntimeError("offline")

        report = run_benchmark(
            Broken(),
            (BenchmarkCase("broken", ({"role": "user", "content": "你好"},)),),
        )
        self.assertEqual(report.failures, 1)
        self.assertEqual(report.results[0].status, "PROVIDER_ERROR")


if __name__ == "__main__":
    unittest.main()
