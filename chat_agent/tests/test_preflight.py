import json
import unittest
from urllib.error import URLError

from chat_agent.preflight import (
    OLLAMA_INVALID_REQUEST,
    OLLAMA_MODEL_NOT_INSTALLED,
    OLLAMA_READY,
    OLLAMA_SERVICE_UNAVAILABLE,
    check_ollama,
)


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class PreflightTests(unittest.TestCase):
    def test_preflight_only_reads_tags_and_reports_selected_model(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return _Response({"models": [{"name": "qwen3:8b"}, {"name": "llama3.2"}]})

        report = check_ollama("qwen3:8b", opener=opener)
        self.assertEqual(report.status, OLLAMA_READY)
        self.assertEqual(report.available_models, ("qwen3:8b", "llama3.2"))
        self.assertEqual(calls[0][0].full_url, "http://127.0.0.1:11434/api/tags")
        self.assertEqual(calls[0][1], 1.5)

    def test_preflight_does_not_call_generation_or_install_missing_model(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return _Response({"models": [{"name": "llama3.2"}]})

        report = check_ollama("qwen3:8b", opener=opener)
        self.assertEqual(report.status, OLLAMA_MODEL_NOT_INSTALLED)
        self.assertEqual(calls, ["http://127.0.0.1:11434/api/tags"])

    def test_preflight_is_local_only_and_fail_closed_when_service_is_down(self):
        self.assertEqual(
            check_ollama("qwen3:8b", endpoint="https://example.com/api/chat").status,
            OLLAMA_INVALID_REQUEST,
        )
        report = check_ollama("qwen3:8b", opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("down")))
        self.assertEqual(report.status, OLLAMA_SERVICE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
