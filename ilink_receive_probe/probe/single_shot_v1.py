"""Single-shot V1 controlled-probe orchestration.

The real production delivery path remains unchanged.  This module provides a
small, dependency-injected flow for an operator-supervised experiment:

* all plan/identity/request checks happen before waiting;
* only one newly accepted direct inbound event may bind a context reference;
* the existing :class:`SessionHealthManager` one-shot authorization remains
  the state-machine authority; and
* a caller-supplied sender is invoked at most once.

No network client is created here.  Tests can therefore exercise the complete
control flow without a real ``sendmessage`` call.  A future operator harness
may pass the existing receive event/context store and the existing transport
as explicit dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from typing import Any, Callable, Mapping

from .context_utils import (
    as_datetime as _as_datetime,
    load_reply_context_record,
    snapshot_context_refs,
)
from .models import InboundEvent
from .proactive_send import (
    ProactiveRequestSpec,
    build_proactive_request_spec,
)
from .session_health import (
    OUTBOUND_BLOCKED,
    OutboundProbeAuthorization,
    SessionHealthManager,
)


CONTEXT_TTL_SECONDS = 300
MAX_CONTEXT_AGE_SECONDS = 30


def _diff_paths(left: Any, right: Any, path: str = "") -> list[str]:
    """Return structural/value differences without exposing their values."""

    if isinstance(left, dict) and isinstance(right, dict):
        differences: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            child = f"{path}.{key}" if path else str(key)
            differences.extend(_diff_paths(left.get(key), right.get(key), child))
        return differences
    if left != right:
        return [path]
    return []


@dataclass(frozen=True)
class StaticPreflight:
    """Checks that do not depend on the fresh inbound context."""

    plan_id: str
    payload_hash: str
    target_date: str
    exact_plan_valid: bool
    ledger_eligible: bool
    credential_valid: bool
    binding_valid: bool
    identity_match: bool
    receive_health_acceptable: bool
    request_fixture_validated: bool
    operator_authorized: bool = True

    @property
    def passed(self) -> bool:
        return all(
            flag is True
            for flag in (
                self.exact_plan_valid,
                self.ledger_eligible,
                self.credential_valid,
                self.binding_valid,
                self.identity_match,
                self.receive_health_acceptable,
                self.request_fixture_validated,
                self.operator_authorized,
            )
        )


@dataclass(frozen=True)
class RequestInvariant:
    """Safe summary of a V0/V1 request comparison.

    The request specs are retained for the caller that must send V1, but this
    dataclass has no custom repr that would print their headers/body.  Callers
    should log only ``only_diff`` and ``other_diffs``.
    """

    v0: ProactiveRequestSpec = field(repr=False)
    v1: ProactiveRequestSpec = field(repr=False)
    only_diff: str | None
    other_diffs: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.only_diff == "context_token" and not self.other_diffs


def compare_v0_v1(v0: ProactiveRequestSpec, v1: ProactiveRequestSpec) -> RequestInvariant:
    """Compare two request specs without logging any request values."""

    differences = _diff_paths(v0.body, v1.body)
    differences.extend(_diff_paths(v0.headers, v1.headers))
    if v0.url != v1.url:
        differences.append("url")
    context_path = "msg.context_token"
    other = tuple(path for path in differences if path != context_path)
    only = "context_token" if differences == [context_path] else None
    return RequestInvariant(v0, v1, only, other)


def build_synthetic_v0_v1_fixture(
    *,
    context_token: str = "SYNTHETIC_CONTEXT",
    fixed_uin: str = "SYNTHETIC_UIN",
) -> RequestInvariant:
    """Build deterministic, network-free V0/V1 specs for static preflight."""

    common = {
        "token": "TEST_TOKEN",
        "recipient": "TEST_RECIPIENT",
        "text": "test",
        "idempotency_key": "fixture-client-001",
        "bot_agent": "ilink_receive_probe/phase16c",
        "channel_version": "1.0.0",
        "app_client_version": "1.0.0",
        "x_wechat_uin": fixed_uin,
    }
    v0 = build_proactive_request_spec(**common, context_token=None)
    v1 = build_proactive_request_spec(**common, context_token=context_token)
    return compare_v0_v1(v0, v1)


@dataclass(frozen=True)
class BoundInbound:
    """Exact event/context identity; raw token is intentionally repr-hidden."""

    event: InboundEvent = field(repr=False)
    context_ref: str
    created_at: datetime
    expires_at: datetime
    context_token: str = field(repr=False)

    @property
    def event_id(self) -> str:
        return self.event.message_id


@dataclass(frozen=True)
class ContextBoundAuthorization:
    """One-shot manager auth additionally bound to one inbound context."""

    plan_id: str
    payload_hash: str
    context_ref: str
    event_id: str
    manager_authorization: OutboundProbeAuthorization = field(repr=False)


@dataclass(frozen=True)
class InboundAcceptance:
    accepted: bool
    reason: str
    context_ref: str | None = None
    event_id: str | None = None
    age_seconds: float | None = None


@dataclass(frozen=True)
class SendOutcome:
    submitted: bool
    state: str
    duplicate: bool = False
    reason: str | None = None
    network_send_count: int = 0
    health_state: str | None = None


class SingleShotV1ProbeFlow:
    """Prepare and optionally execute exactly one context-bound V1 probe."""

    def __init__(
        self,
        *,
        static_preflight: StaticPreflight,
        health_manager: SessionHealthManager,
        runner_started_at: datetime,
        expected_sender_fingerprint: str,
        expected_bot_fingerprint: str,
        context_loader: Callable[[str], Mapping[str, Any]],
        existing_context_refs: frozenset[str] | set[str] = frozenset(),
        now: Callable[[], datetime] | None = None,
        max_context_age_seconds: int = MAX_CONTEXT_AGE_SECONDS,
    ):
        if max_context_age_seconds <= 0:
            raise ValueError("max_context_age_seconds must be positive")
        self.static = static_preflight
        self.health_manager = health_manager
        self.runner_started_at = _as_datetime(runner_started_at)
        if self.runner_started_at is None:
            raise ValueError("runner_started_at is invalid")
        self.expected_sender_fingerprint = expected_sender_fingerprint
        self.expected_bot_fingerprint = expected_bot_fingerprint
        self.context_loader = context_loader
        self.existing_context_refs = frozenset(existing_context_refs)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_context_age_seconds = max_context_age_seconds
        self.state = "NEW"
        self.bound_inbound: BoundInbound | None = None
        self.authorization: ContextBoundAuthorization | None = None
        self.request_pair: RequestInvariant | None = None
        self.network_send_count = 0
        self._sent = False

    def prepare_static(self) -> str:
        """Complete all context-independent checks, then enter WAITING."""

        if self.state != "NEW":
            return "ALREADY_PREPARED"
        if not self.static.passed:
            self.state = "BLOCKED_STATIC"
            return "STATIC_PREFLIGHT_FAILED"
        self.state = "WAITING"
        return "PASS"

    def _reject(self, reason: str, *, context_ref: str | None = None,
                event_id: str | None = None, age: float | None = None) -> InboundAcceptance:
        return InboundAcceptance(False, reason, context_ref, event_id, age)

    def accept_inbound(
        self,
        event: InboundEvent,
        context_ref: str,
        *,
        received_at: Any = None,
        now: datetime | None = None,
    ) -> InboundAcceptance:
        """Accept only the first new, direct, identity-matched event."""

        if self.state != "WAITING":
            reason = "INBOUND_ALREADY_BOUND" if self.bound_inbound else "NOT_WAITING"
            return self._reject(reason, context_ref=context_ref, event_id=event.message_id)
        if context_ref in self.existing_context_refs:
            return self._reject("OLD_CONTEXT_IGNORED", context_ref=context_ref,
                                event_id=event.message_id)
        if not isinstance(context_ref, str) or not context_ref.strip():
            return self._reject("CONTEXT_REF_INVALID", event_id=event.message_id)
        event_time = _as_datetime(received_at if received_at is not None else event.received_at)
        if event_time is None:
            return self._reject("INBOUND_TIMESTAMP_UNAVAILABLE", context_ref=context_ref,
                                event_id=event.message_id)
        if event_time <= self.runner_started_at:
            return self._reject("EVENT_BEFORE_RUNNER_START", context_ref=context_ref,
                                event_id=event.message_id)
        if event.conversation_type != "one_to_one" or event.is_forwarded:
            return self._reject("DIRECT_MESSAGE_REQUIRED", context_ref=context_ref,
                                event_id=event.message_id)
        if event.sender_fingerprint != self.expected_sender_fingerprint:
            return self._reject("SENDER_MISMATCH", context_ref=context_ref,
                                event_id=event.message_id)
        if event.bot_id != self.expected_bot_fingerprint:
            return self._reject("BOT_MISMATCH", context_ref=context_ref,
                                event_id=event.message_id)
        try:
            record = self.context_loader(context_ref)
        except Exception:
            return self._reject("CONTEXT_UNAVAILABLE", context_ref=context_ref,
                                event_id=event.message_id)
        if record.get("context_ref", context_ref) != context_ref:
            return self._reject("CONTEXT_REF_MISMATCH", context_ref=context_ref,
                                event_id=event.message_id)
        if record.get("message_id") != event.message_id:
            return self._reject("CONTEXT_EVENT_MISMATCH", context_ref=context_ref,
                                event_id=event.message_id)
        if record.get("account_scope") not in (None, event.account_scope):
            return self._reject("CONTEXT_ACCOUNT_MISMATCH", context_ref=context_ref,
                                event_id=event.message_id)
        if record.get("sender_fingerprint") not in (None, event.sender_fingerprint):
            return self._reject("CONTEXT_SENDER_MISMATCH", context_ref=context_ref,
                                event_id=event.message_id)
        token = record.get("context_token")
        if not isinstance(token, str) or not token:
            return self._reject("CONTEXT_TOKEN_MISSING", context_ref=context_ref,
                                event_id=event.message_id)
        if event.context_token is not None and event.context_token != token:
            return self._reject("CONTEXT_TOKEN_MISMATCH", context_ref=context_ref,
                                event_id=event.message_id)
        created = _as_datetime(record.get("created_at"))
        expires = _as_datetime(record.get("expires_at"))
        current = _as_datetime(now if now is not None else self.now())
        if created is None or expires is None or current is None:
            return self._reject("CONTEXT_TIMESTAMP_INVALID", context_ref=context_ref,
                                event_id=event.message_id)
        age = max(0.0, (current - created).total_seconds())
        if age >= self.max_context_age_seconds or current >= expires:
            return self._reject("CONTEXT_TOO_OLD", context_ref=context_ref,
                                event_id=event.message_id, age=age)

        # The existing manager remains the authority for the durable latch.
        # Fresh context is deliberately checked before this one-shot arm.
        armed = self.health_manager.mark_outbound_staged_validation(
            plan_id=self.static.plan_id,
            payload_hash=self.static.payload_hash,
            request_fixture_validated=True,
            credential_readable=True,
            binding_valid=True,
            identity_match=True,
            receive_health_acceptable=True,
            operator_authorized=self.static.operator_authorized,
        )
        if armed.authorization is None or armed.state != OUTBOUND_BLOCKED:
            return self._reject("OUTBOUND_STAGED_VALIDATION_BLOCKED",
                                context_ref=context_ref, event_id=event.message_id,
                                age=age)
        allowed = self.health_manager.preflight_controlled_probe(
            plan_id=self.static.plan_id,
            payload_hash=self.static.payload_hash,
            authorization=armed.authorization,
        )
        if not allowed.allowed:
            return self._reject("CONTROLLED_PROBE_NOT_AUTHORIZED",
                                context_ref=context_ref, event_id=event.message_id,
                                age=age)
        self.bound_inbound = BoundInbound(event, context_ref, created, expires, token)
        self.authorization = ContextBoundAuthorization(
            self.static.plan_id, self.static.payload_hash, context_ref,
            event.message_id, armed.authorization,
        )
        self.state = "READY"
        return InboundAcceptance(True, "INBOUND_ACCEPTED", context_ref,
                                 event.message_id, age)

    def build_v1_request(
        self,
        *,
        token: str,
        recipient: str,
        text: str,
        idempotency_key: str,
        bot_agent: str,
        channel_version: str = "1.0.0",
        app_client_version: str | None = None,
        x_wechat_uin: str,
    ) -> RequestInvariant:
        """Construct real-material V0/V1 specs without sending either one."""

        if self.state != "READY" or self.bound_inbound is None:
            raise RuntimeError("fresh inbound has not authorized a V1 request")
        common = {
            "token": token,
            "recipient": recipient,
            "text": text,
            "idempotency_key": idempotency_key,
            "bot_agent": bot_agent,
            "channel_version": channel_version,
            "app_client_version": app_client_version,
            "x_wechat_uin": x_wechat_uin,
        }
        v0 = build_proactive_request_spec(**common, context_token=None)
        v1 = build_proactive_request_spec(
            **common, context_token=self.bound_inbound.context_token,
        )
        pair = compare_v0_v1(v0, v1)
        if not pair.passed:
            self.state = "BLOCKED_REQUEST_INVARIANT"
            raise RuntimeError("V0/V1 request invariant failed")
        self.request_pair = pair
        return pair

    def _context_age(self, now: datetime | None = None) -> float | None:
        if self.bound_inbound is None:
            return None
        current = _as_datetime(now if now is not None else self.now())
        if current is None:
            return None
        return max(0.0, (current - self.bound_inbound.created_at).total_seconds())

    def send_once(
        self,
        send: Callable[[ProactiveRequestSpec], Any],
        *,
        now: datetime | None = None,
    ) -> SendOutcome:
        """Invoke the supplied transport at most once, with no fallback."""

        if self._sent:
            return SendOutcome(False, self.state, duplicate=True,
                               reason="ALREADY_SENT", network_send_count=self.network_send_count)
        if self.state != "READY" or self.authorization is None or self.request_pair is None:
            return SendOutcome(False, self.state, reason="NOT_READY",
                               network_send_count=self.network_send_count)
        age = self._context_age(now)
        if age is None or age >= self.max_context_age_seconds:
            self.state = "BLOCKED_CONTEXT_TOO_OLD"
            return SendOutcome(False, self.state, reason="CONTEXT_TOO_OLD",
                               network_send_count=self.network_send_count)
        self._sent = True
        self.network_send_count = 1
        try:
            result = send(self.request_pair.v1)
            state = str(getattr(result, "state", result.get("state") if isinstance(result, dict) else "UNKNOWN"))
        except Exception as error:
            result = type("UnknownResult", (), {
                "state": "UNKNOWN", "http_status": None, "ret": None,
                "errcode": None, "errmsg": type(error).__name__,
                "response_keys": (),
            })()
            state = "UNKNOWN"
        health = self.health_manager.observe_send_result(
            result, now, authorization=self.authorization.manager_authorization,
        )
        self.state = "ACKED" if state == "ACKED_API" else "FAILED"
        return SendOutcome(
            True, self.state,
            reason=None if state == "ACKED_API" else "SEND_FAILED",
            network_send_count=self.network_send_count,
            health_state=health.state,
        )


__all__ = [
    "CONTEXT_TTL_SECONDS", "MAX_CONTEXT_AGE_SECONDS", "StaticPreflight",
    "RequestInvariant", "compare_v0_v1", "build_synthetic_v0_v1_fixture",
    "snapshot_context_refs", "load_reply_context_record", "BoundInbound",
    "ContextBoundAuthorization", "InboundAcceptance", "SendOutcome",
    "SingleShotV1ProbeFlow",
]
