"""Fail-closed iLink session health state and response classification.

The receive and proactive transports deliberately remain small protocol
adapters.  This module is the shared, redacted policy layer that turns their
safe response diagnostics into a durable health state.  It never stores a
credential, recipient, cursor, context token, or request body.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping

from .proactive_send import IlinkResponseDiagnostics, sanitize_errmsg


class SessionHealthState(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"
    # An outbound send preparation rejection is not proof that the receive
    # session or credential is invalid.  Keep it as a separate durable
    # fail-closed state so a later preflight cannot silently retry it or turn
    # it into an automatic QR-login recommendation.
    OUTBOUND_BLOCKED = "OUTBOUND_BLOCKED"


class SessionErrorClass(str, Enum):
    NETWORK_TRANSIENT = "NETWORK_TRANSIENT"
    AUTH_INVALID = "AUTH_INVALID"
    # Retained for explicit/legacy session-preparation observations.  The
    # ambiguous sendmessage ``ret=-2, errmsg=prepare failed`` envelope is
    # classified as OUTBOUND_PREPARE_FAILED below instead.
    SESSION_PREPARE_FAILED = "SESSION_PREPARE_FAILED"
    OUTBOUND_PREPARE_FAILED = "OUTBOUND_PREPARE_FAILED"
    REMOTE_REJECT = "REMOTE_REJECT"
    UNKNOWN_REMOTE_ERROR = "UNKNOWN_REMOTE_ERROR"


HEALTHY = SessionHealthState.HEALTHY.value
UNKNOWN = SessionHealthState.UNKNOWN.value
DEGRADED = SessionHealthState.DEGRADED.value
REAUTH_REQUIRED = SessionHealthState.REAUTH_REQUIRED.value
OUTBOUND_BLOCKED = SessionHealthState.OUTBOUND_BLOCKED.value
OUTBOUND_PREPARE_FAILED = SessionErrorClass.OUTBOUND_PREPARE_FAILED.value
# The inspected protocol adapter exposes QR login, status, getUpdates and
# notify-start only.  There is no refresh-token or renew-session operation.
AUTOMATIC_REFRESH_SUPPORTED = False
AUTOMATIC_REFRESH_STATUS = "AUTOMATIC_SESSION_REFRESH_NOT_SUPPORTED"


def _safe_fingerprint(value: Any) -> str | None:
    """Keep only the short, non-secret identity fingerprints in state."""
    if value is None:
        return None
    text = str(value)
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", text):
        return None
    return text[:64].lower()


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _numeric(value: Any) -> int | float | str | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    return str(value)[:80]


def _response_values(value: Any) -> tuple[int | None, Any, Any, str | None, tuple[str, ...]]:
    """Extract the already bounded diagnostics shape from any transport."""
    if value is None:
        return None, None, None, None, ()
    diagnostics = getattr(value, "diagnostics", value)
    if isinstance(diagnostics, Mapping):
        status = diagnostics.get("http_status")
        ret = diagnostics.get("ret")
        errcode = diagnostics.get("errcode")
        errmsg = diagnostics.get("errmsg")
        keys = diagnostics.get("response_keys", ())
    else:
        status = getattr(diagnostics, "http_status", None)
        ret = getattr(diagnostics, "ret", None)
        errcode = getattr(diagnostics, "errcode", None)
        errmsg = getattr(diagnostics, "errmsg", None)
        keys = getattr(diagnostics, "response_keys", ())
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    safe_keys = tuple(sorted(str(key)[:80] for key in (keys or ())))
    return status, _numeric(ret), _numeric(errcode), sanitize_errmsg(errmsg), safe_keys


def _as_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SessionHealthObservation:
    state: str
    error_class: str | None
    checked_at: str
    http_status: int | None = None
    ret: Any = None
    errcode: Any = None
    errmsg: str | None = None
    response_keys: tuple[str, ...] = ()
    reason: str | None = None
    account_fingerprint: str | None = None
    bot_fingerprint: str | None = None
    credential_fingerprint: str | None = None
    last_inbound_success_at: str | None = None
    last_proactive_success_at: str | None = None
    first_prepare_failed_at: str | None = None

    @property
    def age_seconds(self) -> float | None:
        try:
            checked = datetime.fromisoformat(self.checked_at)
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(checked.tzinfo) - checked).total_seconds())
        except (TypeError, ValueError):
            return None

    @property
    def allowed(self) -> bool:
        return self.state == HEALTHY


@dataclass(frozen=True)
class OutboundProbeAuthorization:
    """Ephemeral authorization for one exact outbound diagnostic attempt.

    This object contains only a plan id, normalized payload hash, and issue
    timestamp.  It is intentionally kept in memory; a restart therefore
    cannot turn an old operator approval into a new send authorization.
    """

    plan_id: str
    payload_hash: str
    issued_at: str


@dataclass(frozen=True)
class HealthPreflightResult:
    allowed: bool
    state: str
    error_class: str | None
    checked_at: str
    age_seconds: float | None = None
    reason: str | None = None
    http_status: int | None = None
    ret: Any = None
    errcode: Any = None
    errmsg: str | None = None
    response_keys: tuple[str, ...] = ()
    authorization: OutboundProbeAuthorization | None = None

    @property
    def outcome(self) -> str:
        if self.allowed:
            return "HEALTHY"
        if self.error_class == OUTBOUND_PREPARE_FAILED or self.state == OUTBOUND_BLOCKED:
            return "BLOCKED_OUTBOUND_PREPARE_FAILED"
        if self.state == REAUTH_REQUIRED:
            return "BLOCKED_REAUTH_REQUIRED"
        if self.state == UNKNOWN:
            return "BLOCKED_SESSION_UNKNOWN"
        return "BLOCKED_SESSION_DEGRADED"


@dataclass(frozen=True)
class ReceiveHealthResult:
    """Safe result returned by a receive-only getUpdates health probe."""

    diagnostics: IlinkResponseDiagnostics
    message_count: int = 0
    cursor_present: bool = False
    account_fingerprint: str | None = None
    bot_fingerprint: str | None = None
    # Kept only in memory so the policy layer can distinguish a transport
    # timeout from an HTTP/API response.  It is never serialized or logged.
    exception: BaseException | None = None


def classify_response(response: Any = None, *, exception: BaseException | None = None) -> str:
    """Classify a transport outcome without guessing from transport text."""
    if exception is not None:
        # urllib's network exceptions are OSError subclasses; ValueError is
        # reserved for malformed response JSON and therefore remains unknown.
        if isinstance(exception, (TimeoutError, ConnectionError, OSError)):
            return SessionErrorClass.NETWORK_TRANSIENT.value
        return SessionErrorClass.UNKNOWN_REMOTE_ERROR.value

    http_status, ret, errcode, errmsg, keys = _response_values(response)
    ret_i = _as_int(ret)
    err_i = _as_int(errcode)
    if ret_i == -14 or err_i == -14:
        return SessionErrorClass.AUTH_INVALID.value
    # ``prepare failed`` is an outbound send preparation rejection.  It is
    # deliberately not treated as proof of stale credentials or a dead
    # receive session; only explicit AUTH_INVALID/session-expiry evidence may
    # request re-authentication.
    if (ret_i == -2 or err_i == -2) and (errmsg or "").strip().lower() == "prepare failed":
        return OUTBOUND_PREPARE_FAILED
    if (ret_i is not None and ret_i != 0) or (err_i is not None and err_i != 0):
        return SessionErrorClass.REMOTE_REJECT.value
    if ret_i is None and err_i is None:
        # getUpdates' normal success envelope historically carries ``msgs``
        # and a cursor but no ret/errcode.  A 2xx response with that explicit
        # shape is therefore a valid receive health result; an arbitrary JSON
        # object with no status fields remains unknown.
        if http_status is not None and 200 <= http_status < 300 and {
            "msgs", "get_updates_buf"
        }.intersection(keys):
            return "NONE"
        return SessionErrorClass.UNKNOWN_REMOTE_ERROR.value
    return "NONE"


def state_for_error(error_class: str | None) -> str:
    if error_class == OUTBOUND_PREPARE_FAILED:
        return OUTBOUND_BLOCKED
    if error_class in {
        SessionErrorClass.AUTH_INVALID.value,
        SessionErrorClass.SESSION_PREPARE_FAILED.value,
    }:
        return REAUTH_REQUIRED
    if error_class == SessionErrorClass.NETWORK_TRANSIENT.value:
        return DEGRADED
    if error_class in {
        SessionErrorClass.REMOTE_REJECT.value,
        SessionErrorClass.UNKNOWN_REMOTE_ERROR.value,
    }:
        return UNKNOWN
    return HEALTHY


class SessionHealthStore:
    """Atomic JSON store containing only bounded health metadata."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> SessionHealthObservation | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != self.SCHEMA_VERSION:
            return None
        state = payload.get("state")
        if state not in {item.value for item in SessionHealthState}:
            return None
        checked_at = payload.get("checked_at")
        if not isinstance(checked_at, str):
            return None
        keys = payload.get("response_keys") or []
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            return None
        error_class = payload.get("error_class")
        # Migrate the exact legacy representation produced by the old
        # ``ret=-2, errmsg=prepare failed`` mapping in memory.  The file is
        # not rewritten here; the next normal save persists the corrected
        # semantics.  This prevents a restart from reinterpreting an
        # outbound rejection as a credential re-auth requirement.
        if (
            state == REAUTH_REQUIRED
            and error_class == SessionErrorClass.SESSION_PREPARE_FAILED.value
            and (
                _as_int(payload.get("ret")) == -2
                or _as_int(payload.get("errcode")) == -2
            )
            and (sanitize_errmsg(payload.get("errmsg")) or "").strip().lower()
            == "prepare failed"
        ):
            state = OUTBOUND_BLOCKED
            error_class = OUTBOUND_PREPARE_FAILED
        return SessionHealthObservation(
            state=state,
            error_class=error_class,
            checked_at=checked_at,
            http_status=payload.get("http_status"),
            ret=payload.get("ret"),
            errcode=payload.get("errcode"),
            errmsg=sanitize_errmsg(payload.get("errmsg")),
            response_keys=tuple(keys),
            reason=str(payload.get("reason"))[:200] if payload.get("reason") else None,
            account_fingerprint=_safe_fingerprint(payload.get("account_fingerprint")),
            bot_fingerprint=_safe_fingerprint(payload.get("bot_fingerprint")),
            credential_fingerprint=_safe_fingerprint(payload.get("credential_fingerprint")),
            last_inbound_success_at=(
                str(payload.get("last_inbound_success_at"))[:64]
                if payload.get("last_inbound_success_at") else None
            ),
            last_proactive_success_at=(
                str(payload.get("last_proactive_success_at"))[:64]
                if payload.get("last_proactive_success_at") else None
            ),
            first_prepare_failed_at=(
                str(payload.get("first_prepare_failed_at"))[:64]
                if payload.get("first_prepare_failed_at") else None
            ),
        )

    def save(self, observation: SessionHealthObservation) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "state": observation.state,
            "error_class": observation.error_class,
            "checked_at": observation.checked_at,
            "http_status": observation.http_status,
            "ret": _numeric(observation.ret),
            "errcode": _numeric(observation.errcode),
            "errmsg": sanitize_errmsg(observation.errmsg),
            "response_keys": list(observation.response_keys)[:32],
            "reason": None if observation.reason is None else str(observation.reason)[:200],
            "account_fingerprint": _safe_fingerprint(observation.account_fingerprint),
            "bot_fingerprint": _safe_fingerprint(observation.bot_fingerprint),
            "credential_fingerprint": _safe_fingerprint(observation.credential_fingerprint),
            "last_inbound_success_at": observation.last_inbound_success_at,
            "last_proactive_success_at": observation.last_proactive_success_at,
            "first_prepare_failed_at": observation.first_prepare_failed_at,
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class SessionHealthManager:
    """Identity-aware, freshness-bounded health policy."""

    def __init__(self, store: SessionHealthStore, *, account_fingerprint=None,
                 bot_fingerprint=None, credential_fingerprint=None,
                 credential_loader: Callable[[], str] | None = None,
                 binding_loader: Callable[[], Any] | None = None,
                 probe: Callable[[], Any] | None = None,
                 freshness_seconds: int = 300,
                 clock: Callable[[], datetime] | None = None):
        if isinstance(freshness_seconds, bool) or not isinstance(freshness_seconds, int) or freshness_seconds <= 0:
            raise ValueError("freshness_seconds must be a positive integer")
        self.store = store
        self.account_fingerprint = _safe_fingerprint(account_fingerprint)
        self.bot_fingerprint = _safe_fingerprint(bot_fingerprint)
        self.credential_fingerprint = _safe_fingerprint(credential_fingerprint)
        self.credential_loader = credential_loader
        self.binding_loader = binding_loader
        self.probe = probe
        self.freshness_seconds = freshness_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_observation = store.load()
        self.last_transition: str | None = None
        # Never persist this authorization.  It is consumed by the explicit
        # controlled-probe method at most once and disappears on restart.
        self._outbound_probe_authorization: OutboundProbeAuthorization | None = None
        # Keep the exact authorization in memory while the one allowed send is
        # in flight.  A successful ACK may clear the durable outbound latch;
        # a direct mark_healthy() call, a restart, or an unrelated result may
        # not do so.
        self._outbound_probe_in_flight: OutboundProbeAuthorization | None = None

    def _now(self, now: datetime | None = None) -> datetime:
        value = now or self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def _observation(self, state: str, error_class: str | None, now: datetime,
                     *, response=None, reason=None) -> SessionHealthObservation:
        status, ret, errcode, errmsg, keys = _response_values(response)
        previous = self.last_observation or self.store.load()
        checked_at = _iso(now) or now.isoformat()
        first_prepare_failed_at = (
            previous.first_prepare_failed_at if previous else None
        )
        if error_class in {
            SessionErrorClass.SESSION_PREPARE_FAILED.value,
            OUTBOUND_PREPARE_FAILED,
        } and not first_prepare_failed_at:
            first_prepare_failed_at = checked_at
        return SessionHealthObservation(
            state=state,
            error_class=error_class,
            checked_at=_iso(now) or now.isoformat(),
            http_status=status,
            ret=ret,
            errcode=errcode,
            errmsg=errmsg,
            response_keys=keys,
            reason=None if reason is None else str(reason)[:200],
            account_fingerprint=self.account_fingerprint,
            bot_fingerprint=self.bot_fingerprint,
            credential_fingerprint=self.credential_fingerprint,
            last_inbound_success_at=(previous.last_inbound_success_at if previous else None),
            last_proactive_success_at=(previous.last_proactive_success_at if previous else None),
            first_prepare_failed_at=first_prepare_failed_at,
        )

    def _save(self, observation: SessionHealthObservation) -> HealthPreflightResult:
        previous = self.last_observation.state if self.last_observation else UNKNOWN
        self.last_transition = f"{previous}->{observation.state}"
        self.last_observation = observation
        # Any persisted transition invalidates an unconsumed one-shot arm.
        self._outbound_probe_authorization = None
        self._outbound_probe_in_flight = None
        self.store.save(observation)
        return HealthPreflightResult(
            allowed=observation.allowed,
            state=observation.state,
            error_class=observation.error_class,
            checked_at=observation.checked_at,
            age_seconds=0.0,
            reason=observation.reason,
            http_status=observation.http_status,
            ret=observation.ret,
            errcode=observation.errcode,
            errmsg=observation.errmsg,
            response_keys=observation.response_keys,
        )

    def mark_healthy(self, now: datetime | None = None, *, reason="verified") -> HealthPreflightResult:
        current = self._now(now)
        latched = self.last_observation or self.store.load()
        if self._is_outbound_latch(latched):
            # A caller must use the exact controlled-probe path.  In
            # particular, receive-only finalize and an arbitrary health
            # observation cannot clear OUTBOUND_BLOCKED.
            return self._outbound_latched_result(
                current, reason="outbound_recovery_required",
            )
        return self._mark_healthy_unlatched(current, reason=reason)

    def _mark_healthy_unlatched(
        self, current: datetime, *, reason: str,
    ) -> HealthPreflightResult:
        """Persist HEALTHY for paths that are not behind an outbound latch."""

        observation = self._observation(HEALTHY, None, current, reason=reason)
        stamp = observation.checked_at
        if reason == "proactive_send_ack":
            observation = replace(observation, last_proactive_success_at=stamp)
        else:
            observation = replace(observation, last_inbound_success_at=stamp)
        return self._save(observation)

    def mark_staged_validation(self, now: datetime | None = None) -> HealthPreflightResult:
        current = self._now(now)
        latched = self.last_observation or self.store.load()
        if self._is_outbound_latch(latched):
            # A receive-only re-auth finalize must not clear an outbound
            # preparation rejection.  Use the explicit outbound recovery gate
            # below after an operator has validated the request contract.
            return self._outbound_latched_result(
                current, reason="outbound_recovery_required",
            )
        return self.mark_healthy(now, reason="staged_validation_pass")

    def mark_outbound_staged_validation(
        self,
        now: datetime | None = None,
        *,
        plan_id: str,
        payload_hash: str,
        request_fixture_validated: bool,
        credential_readable: bool,
        binding_valid: bool,
        identity_match: bool,
        receive_health_acceptable: bool,
        operator_authorized: bool,
    ) -> HealthPreflightResult:
        """Arm exactly one controlled outbound probe for one plan/hash.

        This is an operator-controlled recovery boundary, not a health reset.
        All evidence flags are required, and the authorization is ephemeral:
        it is never persisted and is consumed by
        :meth:`preflight_controlled_probe` at most once.  A normal
        ``preflight`` or ``mark_staged_validation`` call remains blocked.
        """

        current = self._now(now)
        latched = self.last_observation or self.store.load()
        if not self._is_outbound_latch(latched):
            return HealthPreflightResult(
                allowed=False,
                state=latched.state if latched is not None else UNKNOWN,
                error_class=latched.error_class if latched is not None else None,
                checked_at=(latched.checked_at if latched is not None
                            else _iso(current) or current.isoformat()),
                age_seconds=(self._age(latched, current)
                             if latched is not None else 0.0),
                reason="outbound_recovery_not_required",
            )
        if (
            not isinstance(plan_id, str)
            or not plan_id.strip()
            or not isinstance(payload_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", payload_hash) is None
            or not all(
                flag is True
                for flag in (
                    request_fixture_validated,
                    credential_readable,
                    binding_valid,
                    identity_match,
                    receive_health_acceptable,
                    operator_authorized,
                )
            )
        ):
            return self._outbound_latched_result(
                current, reason="outbound_staged_validation_requirements_missing",
            )
        if self._outbound_probe_in_flight is not None:
            return self._outbound_latched_result(
                current, reason="outbound_probe_in_flight",
            )
        authorization = OutboundProbeAuthorization(
            plan_id=plan_id,
            payload_hash=payload_hash,
            issued_at=_iso(current) or current.isoformat(),
        )
        self._outbound_probe_authorization = authorization
        return replace(
            self._outbound_latched_result(
                current, reason="outbound_probe_authorized_for_exact_plan",
            ),
            authorization=authorization,
        )

    def preflight_controlled_probe(
        self,
        now: datetime | None = None,
        *,
        plan_id: str,
        payload_hash: str,
        authorization: OutboundProbeAuthorization,
    ) -> HealthPreflightResult:
        """Consume one exact outbound probe authorization, if still armed."""

        current = self._now(now)
        latched = self.last_observation or self.store.load()
        pending = self._outbound_probe_authorization
        if (
            not self._is_outbound_latch(latched)
            or pending is None
            or pending != authorization
            or pending.plan_id != plan_id
            or pending.payload_hash != payload_hash
        ):
            self._outbound_probe_authorization = None
            if self._is_outbound_latch(latched):
                return self._outbound_latched_result(
                    current, reason="outbound_probe_authorization_missing",
                )
            return HealthPreflightResult(
                allowed=False,
                state=latched.state if latched is not None else UNKNOWN,
                error_class=latched.error_class if latched is not None else None,
                checked_at=(latched.checked_at if latched is not None
                            else _iso(current) or current.isoformat()),
                reason="outbound_recovery_not_required",
            )
        # One-use consumption is in-memory only.  The send observer must record
        # the actual ACK/FAILED result; until then a restart remains blocked.
        self._outbound_probe_authorization = None
        self._outbound_probe_in_flight = authorization
        return HealthPreflightResult(
            allowed=True,
            state=HEALTHY,
            error_class=None,
            checked_at=_iso(current) or current.isoformat(),
            age_seconds=0.0,
            reason="outbound_controlled_probe_authorized",
            authorization=authorization,
        )

    def _identity_failure(self, now: datetime, reason: str) -> HealthPreflightResult:
        observation = self._observation(
            REAUTH_REQUIRED, SessionErrorClass.AUTH_INVALID.value, now, reason=reason,
        )
        return self._save(observation)

    def _fresh_observation(self, now: datetime) -> HealthPreflightResult | None:
        observation = self.last_observation or self.store.load()
        if not observation or observation.state != HEALTHY:
            return None
        # A health record is bound to the identity that produced it.  Never
        # reuse a fresh-looking record after QR rotation or credential swap.
        for expected, observed in (
            (self.account_fingerprint, observation.account_fingerprint),
            (self.bot_fingerprint, observation.bot_fingerprint),
            (self.credential_fingerprint, observation.credential_fingerprint),
        ):
            if expected is not None and observed != expected:
                return None
        try:
            checked = datetime.fromisoformat(observation.checked_at)
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=now.tzinfo)
            age = (now - checked).total_seconds()
        except (TypeError, ValueError):
            return None
        if age < 0 or age > self.freshness_seconds:
            return None
        return HealthPreflightResult(
            allowed=True, state=HEALTHY, error_class=None,
            checked_at=observation.checked_at, age_seconds=age,
            reason="fresh_verified_health", http_status=observation.http_status,
            ret=observation.ret, errcode=observation.errcode,
            errmsg=observation.errmsg, response_keys=observation.response_keys,
        )

    @staticmethod
    def _age(observation: SessionHealthObservation, now: datetime) -> float | None:
        try:
            checked = datetime.fromisoformat(observation.checked_at)
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=now.tzinfo)
            return max(0.0, (now - checked).total_seconds())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_outbound_latch(observation: SessionHealthObservation | None) -> bool:
        return bool(
            observation is not None
            and (
                observation.state == OUTBOUND_BLOCKED
                or observation.error_class == OUTBOUND_PREPARE_FAILED
            )
        )

    def _outbound_latched_result(
        self, now: datetime, *, reason: str,
    ) -> HealthPreflightResult:
        """Return a bounded result for the durable outbound-failure latch."""

        latched = self.last_observation or self.store.load()
        if latched is None:
            return HealthPreflightResult(
                allowed=False,
                state=OUTBOUND_BLOCKED,
                error_class=OUTBOUND_PREPARE_FAILED,
                checked_at=_iso(now) or now.isoformat(),
                age_seconds=0.0,
                reason=reason,
            )
        return HealthPreflightResult(
            allowed=False,
            state=OUTBOUND_BLOCKED,
            error_class=latched.error_class or OUTBOUND_PREPARE_FAILED,
            checked_at=latched.checked_at,
            age_seconds=self._age(latched, now),
            reason=reason,
            http_status=latched.http_status,
            ret=latched.ret,
            errcode=latched.errcode,
            errmsg=latched.errmsg,
            response_keys=latched.response_keys,
        )

    def preflight(self, now: datetime | None = None) -> HealthPreflightResult:
        current = self._now(now)
        # Identity checks always precede a freshness shortcut.  A changed or
        # unreadable credential must never be masked by an old HEALTHY record.
        if self.credential_loader is not None:
            try:
                credential = self.credential_loader()
                if not isinstance(credential, str) or not credential:
                    return self._identity_failure(current, "credential_unreadable")
            except Exception:
                return self._identity_failure(current, "credential_unreadable")
        if not self.account_fingerprint or not self.bot_fingerprint:
            return self._identity_failure(current, "identity_metadata_missing")
        if self.binding_loader is not None:
            try:
                self.binding_loader()
            except Exception:
                return self._identity_failure(current, "recipient_binding_unavailable")

        # REAUTH_REQUIRED and outbound preparation rejection are durable
        # safety latches.  A receive poll can still succeed after
        # sendmessage reports ``prepare failed``; treating that poll as an
        # implicit re-auth or send-clear would misclassify the failure and
        # could enable an unapproved retry.  Outbound recovery is cleared only
        # by an ACK paired with the exact explicit controlled-probe arm below.
        latched = self.last_observation or self.store.load()
        if latched is not None and (
            latched.state == REAUTH_REQUIRED
            or latched.state == OUTBOUND_BLOCKED
            or latched.error_class == OUTBOUND_PREPARE_FAILED
        ):
            outbound_latched = (
                latched.state == OUTBOUND_BLOCKED
                or latched.error_class == OUTBOUND_PREPARE_FAILED
            )
            return HealthPreflightResult(
                allowed=False,
                state=OUTBOUND_BLOCKED if outbound_latched else REAUTH_REQUIRED,
                error_class=latched.error_class,
                checked_at=latched.checked_at,
                age_seconds=self._age(latched, current),
                reason=(
                    "outbound_prepare_failed_latched"
                    if outbound_latched else "reauth_required_latched"
                ),
                http_status=latched.http_status,
                ret=latched.ret, errcode=latched.errcode,
                errmsg=latched.errmsg, response_keys=latched.response_keys,
            )

        fresh = self._fresh_observation(current)
        if fresh is not None:
            return fresh
        if self.probe is None:
            observation = self._observation(
                UNKNOWN, SessionErrorClass.UNKNOWN_REMOTE_ERROR.value, current,
                reason="no_receive_health_probe",
            )
            return self._save(observation)
        try:
            response = self.probe()
            probe_exception = getattr(response, "exception", None)
            diagnostics = getattr(response, "diagnostics", response)
            error_class = classify_response(response, exception=probe_exception)
            if error_class == "NONE":
                observed_account = getattr(response, "account_fingerprint", None)
                observed_bot = getattr(response, "bot_fingerprint", None)
                if ((observed_account and observed_account != self.account_fingerprint)
                        or (observed_bot and observed_bot != self.bot_fingerprint)):
                    return self._identity_failure(current, "receive_probe_identity_mismatch")
                observation = self._observation(HEALTHY, None, current,
                                                response=diagnostics,
                                                reason="receive_probe_pass")
                observation = replace(
                    observation, last_inbound_success_at=observation.checked_at
                )
            else:
                observation = self._observation(
                    state_for_error(error_class), error_class, current,
                    response=diagnostics, reason="receive_probe_failed",
                )
        except Exception as error:
            error_class = classify_response(exception=error)
            observation = self._observation(
                state_for_error(error_class), error_class, current,
                reason=type(error).__name__,
            )
        return self._save(observation)

    def observe_send_result(
        self,
        result: Any,
        now: datetime | None = None,
        *,
        authorization: OutboundProbeAuthorization | None = None,
    ) -> HealthPreflightResult:
        current = self._now(now)
        state = str(getattr(result, "state", "UNKNOWN"))
        if state == "ACKED_API":
            latched = self.last_observation or self.store.load()
            if self._is_outbound_latch(latched):
                in_flight = self._outbound_probe_in_flight
                if in_flight is None or authorization != in_flight:
                    return self._outbound_latched_result(
                        current, reason="outbound_controlled_probe_required",
                    )
                # Only an ACK paired with the exact one-shot authorization may
                # clear the durable latch.  This bypasses mark_healthy's
                # general guard through the private unlatched helper.
                self._outbound_probe_in_flight = None
                return self._mark_healthy_unlatched(
                    current, reason="proactive_send_ack",
                )
            return self.mark_healthy(current, reason="proactive_send_ack")
        error_class = classify_response(result)
        if error_class == "NONE":
            error_class = SessionErrorClass.UNKNOWN_REMOTE_ERROR.value
        observation = self._observation(
            state_for_error(error_class), error_class, current,
            response=result, reason="proactive_send_failed",
        )
        return self._save(observation)


# Keep the requested protocol-style name available without making the rest of
# the code depend on unusual capitalization.
class iLinkSessionHealth(SessionHealthManager):
    pass


__all__ = [
    "SessionHealthState", "SessionErrorClass", "SessionHealthObservation",
    "OutboundProbeAuthorization", "HealthPreflightResult", "ReceiveHealthResult", "SessionHealthStore",
    "SessionHealthManager", "iLinkSessionHealth", "classify_response",
    "state_for_error", "HEALTHY", "UNKNOWN", "DEGRADED", "REAUTH_REQUIRED",
    "OUTBOUND_BLOCKED", "OUTBOUND_PREPARE_FAILED",
    "AUTOMATIC_REFRESH_SUPPORTED", "AUTOMATIC_REFRESH_STATUS",
]
