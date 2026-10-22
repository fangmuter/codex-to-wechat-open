import json
import unittest
from urllib.error import URLError

from chat_agent.ollama_provider import (
    INVALID_MODEL_OUTPUT,
    MODEL_UNAVAILABLE,
    OllamaProvider,
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
        return self.payload


class OllamaProviderTests(unittest.TestCase):
    def test_structured_local_chat_request_and_success(self):
        calls = []

        def opener(request, *, timeout):
            calls.append((request, timeout))
            return _Response(
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"draft": "收到，我晚点回复你。", "needs_user_input": False}
                            ),
                        }
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )

        provider = OllamaProvider("local-model", opener=opener)
        result = provider.generate(
            [
                {"role": "system", "content": "只输出 JSON"},
                {"role": "user", "content": "在吗"},
            ],
            timeout_seconds=3.5,
        )
        self.assertEqual(result.draft, "收到，我晚点回复你。")
        self.assertFalse(result.needs_user_input)
        request, timeout = calls[0]
        self.assertEqual(timeout, 3.5)
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "local-model")
        self.assertFalse(body["stream"])
        self.assertIsInstance(body["format"], dict)
        self.assertNotIn("tools", body)

    def test_invalid_structured_content_is_no_send(self):
        provider = OllamaProvider(
            "local-model",
            opener=lambda *_args, **_kwargs: _Response(
                b'{"message":{"content":"not-json"}}'
            ),
        )
        result = provider.generate(
            [{"role": "user", "content": "你好"}], timeout_seconds=1
        )
        self.assertTrue(result.needs_user_input)
        self.assertIsNone(result.draft)
        self.assertEqual(result.reason, INVALID_MODEL_OUTPUT)

    def test_network_failure_is_explicitly_unavailable(self):
        def opener(*_args, **_kwargs):
            raise URLError("Ollama is not running")

        result = OllamaProvider("local-model", opener=opener).generate(
            [{"role": "user", "content": "你好"}], timeout_seconds=1
        )
        self.assertTrue(result.needs_user_input)
        self.assertEqual(result.reason, MODEL_UNAVAILABLE)

    def test_remote_and_empty_requests_are_rejected(self):
        with self.assertRaises(ValueError):
            OllamaProvider("local-model", endpoint="https://ollama.com/api/chat")
        result = OllamaProvider(
            "local-model",
            opener=lambda *_args, **_kwargs: self.fail("opener must not be called"),
        ).generate([], timeout_seconds=1)
        self.assertTrue(result.needs_user_input)


if __name__ == "__main__":
    unittest.main()
