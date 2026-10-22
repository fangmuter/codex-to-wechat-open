"""One-shot, context-free proactive iLink text-send adapter.

The request-shape interoperability review referenced Tencent
``openclaw-weixin`` v2.4.8. No upstream source files are vendored; see
``THIRD_PARTY_NOTICES.md`` for attribution and the preserved MIT notice.
This project is not endorsed by or affiliated with Tencent.
"""
import base64
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .recipient_binding import RecipientBindingUnavailable

PILOT_KEY = "phase16b:single-proactive-pilot-001"
PILOT_TEXT = "开始准备明日计划了吗？"

def require_expected_recipient_fingerprint() -> str:
    """Load the operator's recipient fingerprint without shipping one in code."""
    value = os.environ.get("ILINK_EXPECTED_RECIPIENT_FINGERPRINT", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{12}", value):
        raise RuntimeError(
            "Set ILINK_EXPECTED_RECIPIENT_FINGERPRINT to the approved 12-hex fingerprint"
        )
    return value


def sanitize_errmsg(value, limit=200):
    """Return a bounded, control-free response diagnostic.

    Only the provider's error string is accepted here.  Known credential,
    recipient, cookie, and context-token assignments are redacted before the
    value is persisted by the delivery ledger.
    """
    if value is None:
        return None
    text = str(value)
    text = re.sub(
        r"(?i)(authorization|bearer|token|cookie|recipient|credential|context[_-]?token|dpapi)\s*[=:]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    text = "".join(ch if ch in "\t " or ord(ch) >= 32 else " " for ch in text)
    return " ".join(text.split())[:limit]


@dataclass(frozen=True)
class IlinkResponseDiagnostics:
    """Safe response metadata; never contains request data or response body."""
    http_status: int | None = None
    ret: object = None
    errcode: object = None
    errmsg: str | None = None
    response_keys: tuple[str, ...] = ()

    # Small compatibility shim for the retired pilot helper, without exposing
    # the raw provider response body.
    def get(self, key, default=None):
        values = {
            "ret": self.ret,
            "errcode": self.errcode,
            "errmsg": self.errmsg,
        }
        return values.get(key, default)


@dataclass(frozen=True)
class ProactiveRequestSpec:
    """Pure, inspectable sendmessage request material.

    The spec is deliberately separate from ``urllib.request.Request`` so
    offline contract fixtures can inspect headers/body without touching the
    network.  Production still creates the same Request from this spec below.
    """

    url: str
    headers: dict[str, str]
    body: dict[str, Any]


def build_proactive_request_spec(
    *,
    token: str,
    recipient: str,
    text: str,
    idempotency_key: str,
    bot_agent: str,
    base_url: str | None = None,
    channel_version: str = "1.0.0",
    app_client_version: str | None = None,
    client_id: str | None = None,
    context_token: str | None = None,
    run_id: str | None = None,
    sk_route_tag: str | None = None,
    x_wechat_uin: str | None = None,
    random_source: Callable[[], int] | None = None,
) -> ProactiveRequestSpec:
    """Build a sendmessage request without performing I/O.

    ``x_wechat_uin`` or a deterministic ``random_source`` must be supplied by
    the caller.  Keeping random generation outside this pure builder makes
    offline fixtures reproducible; the production transport generates its UIN
    immediately before calling the builder.  Optional protocol fields are
    omitted unless explicitly given, preserving the current context-free
    production request by default.
    """

    if x_wechat_uin is None:
        if random_source is None:
            raise ValueError("x_wechat_uin or random_source is required")
        x_wechat_uin = base64.b64encode(str(random_source()).encode()).decode()
    body_msg: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": recipient,
        "client_id": idempotency_key if client_id is None else client_id,
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token is not None:
        body_msg["context_token"] = context_token
    if run_id is not None:
        body_msg["run_id"] = run_id
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": "Bearer " + token,
        "X-WECHAT-UIN": x_wechat_uin,
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": (
            channel_version if app_client_version is None else app_client_version
        ),
    }
    if sk_route_tag is not None:
        headers["SKRouteTag"] = sk_route_tag
    return ProactiveRequestSpec(
        url=(base_url or "https://ilinkai.weixin.qq.com").rstrip("/")
        + "/ilink/bot/sendmessage",
        headers=headers,
        body={
            "msg": body_msg,
            "base_info": {"channel_version": channel_version, "bot_agent": bot_agent},
        },
    )


class DefiniteProactiveSendError(RuntimeError):
    def __init__(self, errcode=None, *, http_status=None, ret=None,
                 api_errcode=None, errmsg=None, response_keys=()):
        super().__init__("sendmessage rejected")
        self.errcode = errcode
        self.diagnostics = IlinkResponseDiagnostics(
            http_status=http_status,
            ret=ret,
            errcode=api_errcode,
            errmsg=sanitize_errmsg(errmsg),
            response_keys=tuple(response_keys),
        )


class AmbiguousProactiveSendError(RuntimeError):
    """The request may have reached iLink; it must never be retried blindly."""

    def __init__(self, message="send result is unknown", *, http_status=None,
                 errmsg=None, response_keys=()):
        super().__init__(message)
        self.diagnostics = IlinkResponseDiagnostics(
            http_status=http_status,
            errmsg=sanitize_errmsg(errmsg),
            response_keys=tuple(response_keys),
        )


class ProactiveSendStorage:
    """Stores only idempotency and redacted delivery state, never a recipient."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS proactive_send_attempts(
            idempotency_key TEXT PRIMARY KEY, recipient_fingerprint TEXT NOT NULL,
            submitted_at TEXT NOT NULL, state TEXT NOT NULL, api_success INTEGER,
            errcode TEXT)""")
        self.db.commit()

    def close(self):
        self.db.close()

    def begin(self, key, fingerprint):
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.db.execute("INSERT INTO proactive_send_attempts VALUES(?,?,?,?,?,?)", (key, fingerprint, now, "SENDING", None, None))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def record(self, key, state, success=None, errcode=None):
        self.db.execute("UPDATE proactive_send_attempts SET state=?,api_success=?,errcode=? WHERE idempotency_key=?", (state, success, None if errcode is None else str(errcode), key))
        self.db.commit()

    def get(self, key):
        return self.db.execute("SELECT * FROM proactive_send_attempts WHERE idempotency_key=?", (key,)).fetchone()


class ProactiveIlinkTransport:
    """One text ``sendmessage`` surface with an optional bound context token.

    Context-free callers retain the historical V0 request.  The Web-First
    production path passes a freshly accepted inbound token explicitly; no
    token is ever inferred from global state by this transport.
    """
    BASE = "https://ilinkai.weixin.qq.com"
    ALLOWED = ("sendmessage",)

    def __init__(self, token, base_url=None, timeout=15_000, channel_version="1.0.0"):
        self.token = token
        self.base_url = (base_url or self.BASE).rstrip("/")
        self.timeout = timeout / 1000
        self.channel_version = channel_version
        self.calls = []

    def send_text(self, recipient, text, idempotency_key, bot_agent,
                  *, context_token=None):
        uin = base64.b64encode(str(secrets.randbits(32)).encode()).decode()
        spec = build_proactive_request_spec(
            token=self.token,
            recipient=recipient,
            text=text,
            idempotency_key=idempotency_key,
            bot_agent=bot_agent,
            base_url=self.base_url,
            channel_version=self.channel_version,
            app_client_version=self.channel_version,
            context_token=context_token,
            x_wechat_uin=uin,
        )
        request = Request(
            spec.url, data=json.dumps(spec.body).encode(), method="POST",
            headers=spec.headers,
        )
        self.calls.append("sendmessage")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                http_status = getattr(response, "status", None)
                if http_status is None:
                    http_status = getattr(response, "code", None)
                result = json.loads(response.read().decode())
        except HTTPError as error:
            raise AmbiguousProactiveSendError(
                "send result is unknown", http_status=getattr(error, "code", None),
                errmsg=type(error).__name__,
            ) from error
        except (TimeoutError, ConnectionError, URLError, ValueError) as error:
            raise AmbiguousProactiveSendError(
                "send result is unknown", errmsg=type(error).__name__,
            ) from error
        response_keys = tuple(sorted(str(key) for key in result.keys()))
        ret = result.get("ret")
        api_errcode = result.get("errcode")
        errmsg = sanitize_errmsg(result.get("errmsg"))
        diagnostics = IlinkResponseDiagnostics(
            http_status=http_status,
            ret=ret,
            errcode=api_errcode,
            errmsg=errmsg,
            response_keys=response_keys,
        )
        # Existing semantics intentionally remain ret-driven: a nonzero ret
        # is a definite API rejection.  errcode is still preserved separately
        # even when ret is zero.
        if ret not in (None, 0):
            preferred_errcode = api_errcode if api_errcode is not None else ret
            raise DefiniteProactiveSendError(
                preferred_errcode,
                http_status=http_status,
                ret=ret,
                api_errcode=api_errcode,
                errmsg=errmsg,
                response_keys=response_keys,
            )
        return diagnostics

    def send_pilot_text(self, recipient, idempotency_key):
        return self.send_text(recipient, PILOT_TEXT, idempotency_key, "ilink_receive_probe/phase16b")


class SingleProactivePilot:
    def __init__(self, binding_store, storage, transport, expected_fingerprint=None):
        self.binding_store = binding_store
        self.storage = storage
        self.transport = transport
        self.expected_fingerprint = expected_fingerprint

    def send_once(self):
        if self.expected_fingerprint is None:
            self.expected_fingerprint = require_expected_recipient_fingerprint()
        recipient = self.binding_store.load_for_proactive_send(self.expected_fingerprint)
        if not self.storage.begin(PILOT_KEY, self.expected_fingerprint):
            existing = self.storage.get(PILOT_KEY)
            return {"submitted": False, "success": existing["api_success"], "errcode": existing["errcode"], "state": existing["state"], "duplicate": True}
        try:
            result = self.transport.send_pilot_text(recipient, PILOT_KEY)
        except AmbiguousProactiveSendError:
            self.storage.record(PILOT_KEY, "UNKNOWN", False, "AMBIGUOUS")
            return {"submitted": True, "success": False, "errcode": "AMBIGUOUS", "state": "UNKNOWN", "duplicate": False}
        except DefiniteProactiveSendError as error:
            self.storage.record(PILOT_KEY, "FAILED", False, error.errcode)
            return {"submitted": True, "success": False, "errcode": error.errcode, "state": "FAILED", "duplicate": False}
        # Keep the legacy pilot's result shape while never collapsing ret and
        # errcode into one ambiguous value.
        if isinstance(result, IlinkResponseDiagnostics):
            result_errcode = result.errcode
        else:
            result_errcode = result.get("errcode") if "errcode" in result else None
        self.storage.record(PILOT_KEY, "ACKED_API", True, result_errcode)
        return {"submitted": True, "success": True, "errcode": result_errcode,
                "state": "ACKED_API", "duplicate": False}
