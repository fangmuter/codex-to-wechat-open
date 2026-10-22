import base64
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

from ilink_receive_probe.probe.proactive_send import (
    ProactiveRequestSpec,
    build_proactive_request_spec,
    require_expected_recipient_fingerprint,
)


def fixture(**overrides):
    values = {
        "token": "TEST_TOKEN",
        "recipient": "TEST_RECIPIENT",
        "text": "test",
        "idempotency_key": "fixture-client-001",
        "bot_agent": "ilink_receive_probe/phase16c",
        "channel_version": "1.0.0",
        "app_client_version": "1.0.0",
        "random_source": lambda: 123456,
    }
    values.update(overrides)
    return build_proactive_request_spec(**values)


class ProactiveRequestFixtureTests(unittest.TestCase):
    def test_live_recipient_fingerprint_is_explicit_and_never_bundled(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                require_expected_recipient_fingerprint()
        with patch.dict(
            "os.environ", {"ILINK_EXPECTED_RECIPIENT_FINGERPRINT": "ABCDEF012345"},
            clear=True,
        ):
            self.assertEqual(require_expected_recipient_fingerprint(), "abcdef012345")

    def test_v0_fixture_is_deterministic_and_network_free(self):
        first = fixture()
        second = fixture()
        self.assertIsInstance(first, ProactiveRequestSpec)
        self.assertEqual(first, second)
        self.assertEqual(
            first.headers["X-WECHAT-UIN"],
            base64.b64encode(b"123456").decode(),
        )
        self.assertEqual(first.body["msg"]["client_id"], "fixture-client-001")

    def test_builder_requires_explicit_deterministic_uin_input(self):
        with self.assertRaises(ValueError):
            build_proactive_request_spec(
                token="TEST_TOKEN", recipient="TEST_RECIPIENT", text="test",
                idempotency_key="fixture-client-001",
                bot_agent="ilink_receive_probe/phase16c",
            )

    def test_v1_only_adds_context_token(self):
        v0 = fixture()
        v1 = fixture(context_token="TEST_CONTEXT")
        self.assertEqual(v0.url, v1.url)
        self.assertEqual(v0.headers, v1.headers)
        self.assertEqual(v0.body["base_info"], v1.body["base_info"])
        self.assertEqual(
            {key: value for key, value in v0.body["msg"].items()},
            {key: value for key, value in v1.body["msg"].items()
             if key != "context_token"},
        )
        self.assertEqual(v1.body["msg"]["context_token"], "TEST_CONTEXT")

    def test_v2_only_changes_version_contract_fields(self):
        v1 = fixture(context_token="TEST_CONTEXT")
        v2 = fixture(
            context_token="TEST_CONTEXT",
            channel_version="2.4.8",
            app_client_version="132104",
        )
        self.assertEqual(v1.headers["iLink-App-ClientVersion"], "1.0.0")
        self.assertEqual(v2.headers["iLink-App-ClientVersion"], "132104")
        self.assertEqual(v1.body["base_info"]["channel_version"], "1.0.0")
        self.assertEqual(v2.body["base_info"]["channel_version"], "2.4.8")
        self.assertEqual(v1.body["msg"], {
            **v2.body["msg"],
        })
        self.assertEqual(v1.body["base_info"]["bot_agent"],
                         v2.body["base_info"]["bot_agent"])
        self.assertEqual(
            {key: value for key, value in v1.headers.items()
             if key != "iLink-App-ClientVersion"},
            {key: value for key, value in v2.headers.items()
             if key != "iLink-App-ClientVersion"},
        )

    def test_v3_adds_expected_client_and_session_fields(self):
        v2 = fixture(
            context_token="TEST_CONTEXT",
            channel_version="2.4.8",
            app_client_version="132104",
        )
        v3 = fixture(
            context_token="TEST_CONTEXT",
            channel_version="2.4.8",
            app_client_version="132104",
            bot_agent="OpenClaw",
            client_id="openclaw-weixin:fixture-001",
            run_id="fixture-run-001",
            sk_route_tag="TEST_ROUTE",
        )
        self.assertEqual(v3.body["msg"]["client_id"], "openclaw-weixin:fixture-001")
        self.assertEqual(v3.body["msg"]["run_id"], "fixture-run-001")
        self.assertEqual(v3.body["base_info"]["bot_agent"], "OpenClaw")
        self.assertEqual(v3.headers["SKRouteTag"], "TEST_ROUTE")
        self.assertNotIn("run_id", v2.body["msg"])
        self.assertNotIn("SKRouteTag", v2.headers)

    def test_all_variants_keep_non_secret_message_shape(self):
        for spec in (
            fixture(),
            fixture(context_token="TEST_CONTEXT"),
            fixture(context_token="TEST_CONTEXT", channel_version="2.4.8",
                    app_client_version="132104"),
            fixture(context_token="TEST_CONTEXT", channel_version="2.4.8",
                    app_client_version="132104", bot_agent="OpenClaw",
                    client_id="openclaw-weixin:fixture-001",
                    run_id="fixture-run-001", sk_route_tag="TEST_ROUTE"),
        ):
            self.assertEqual(spec.body["msg"]["from_user_id"], "")
            self.assertEqual(spec.body["msg"]["message_type"], 2)
            self.assertEqual(spec.body["msg"]["message_state"], 2)
            self.assertEqual(spec.body["msg"]["item_list"], [
                {"type": 1, "text_item": {"text": "test"}},
            ])
        # The builder itself has no logging or filesystem side effects.
        self.assertEqual(fixture().url, "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage")

    def test_fixture_builder_emits_no_secret_log_output(self):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            fixture(
                token="SYNTHETIC_TOKEN_ONLY",
                recipient="SYNTHETIC_RECIPIENT_ONLY",
                context_token="SYNTHETIC_CONTEXT_ONLY",
            )
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_fixture_variants_never_call_transport(self):
        with patch("ilink_receive_probe.probe.proactive_send.urlopen") as opener:
            fixture()
            fixture(context_token="SYNTHETIC_CONTEXT_ONLY")
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
