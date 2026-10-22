"""Bounded, redacted lifecycle evidence for the iLink session experiment.

The logger is deliberately independent from the planner and Delivery runtime.
It records only an allow-listed set of identity fingerprints and transport
diagnostics.  Credentials, recipients, message text, context tokens, QR data,
and request bodies are never accepted or persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Mapping

from .proactive_send import sanitize_errmsg


LIFECYCLE_EVENT_TYPES = (
    "AUTH_STAGED",
    "AUTH_PROMOTED",
    "INBOUND_PROBE_OK",
    "INBOUND_PROBE_FAILED",
    "PROACTIVE_SMOKE_OK",
    "PROACTIVE_SMOKE_FAILED",
    "SESSION_PREPARE_FAILED",
    "PC_WECHAT_LOGGED_IN",
    "PC_WECHAT_LOGGED_OUT",
    "PC_WECHAT_RELOGIN",
    "SESSION_HEALTH_TRANSITION",
    "WINDOWS_BOOT_OBSERVED",
    "WINDOWS_REBOOT_DETECTED",
    "HEALTH_CHECK_STARTED",
    "HEALTH_CHECK_RESULT",
)

_FINGERPRINT = re.compile(r"^[0-9a-fA-F]{8,64}$")
_PC_STATES = {"PC_WECHAT_RUNNING", "PC_WECHAT_NOT_RUNNING", "UNKNOWN"}


def _iso(value: datetime | str | None = None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        parsed = _parse_timestamp(value)
        # Lifecycle records must use one comparable, timezone-aware format.
        # An invalid caller-supplied timestamp must never break the receive
        # path; use a fresh bounded UTC timestamp instead.
        return (parsed or datetime.now(timezone.utc)).isoformat()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a lifecycle timestamp and normalize it to UTC, if valid."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:64].lower() if _FINGERPRINT.fullmatch(text) else None


def _code(value: Any, limit: int = 80) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] if text else None


def _numeric(value: Any) -> int | float | str | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value if not isinstance(value, str) else value[:80]
    return str(value)[:80]


def _keys(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = value.keys()
    if isinstance(value, (str, bytes)):
        return []
    try:
        values = value
        return sorted({str(item)[:80] for item in values})[:32]
    except TypeError:
        return []


@dataclass(frozen=True)
class SessionLifecycleEvent:
    timestamp: str
    event_type: str
    bot_fingerprint: str | None = None
    account_fingerprint: str | None = None
    credential_fingerprint: str | None = None
    pc_wechat_state: str = "UNKNOWN"
    boot_session: str | None = None
    boot_time: str | None = None
    session_health: str | None = None
    inbound_result: str | None = None
    proactive_result: str | None = None
    http_status: int | None = None
    ret: int | float | str | None = None
    errcode: int | float | str | None = None
    errmsg: str | None = None
    response_keys: tuple[str, ...] = ()
    last_verified_healthy_at: str | None = None
    idle_seconds: int | float | None = None
    error_class: str | None = None
    previous_boot_session: str | None = None
    last_known_session_health_before_boot: str | None = None
    first_observed_session_health: str | None = None


def _boot_snapshot(now: datetime | None = None, *, uptime_ms: int | None = None) -> tuple[str | None, str | None]:
    """Return a non-secret boot time and a stable per-boot fingerprint."""
    if uptime_ms is None:
        try:
            if os.name == "nt":
                import ctypes

                uptime_ms = int(ctypes.windll.kernel32.GetTickCount64())
            else:
                uptime_ms = int(float(Path("/proc/uptime").read_text().split()[0]) * 1000)
        except (AttributeError, OSError, ValueError, IndexError):
            return None, None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    boot = current.astimezone(timezone.utc).timestamp() - uptime_ms / 1000.0
    # The uptime API is millisecond precision while ``now`` varies between
    # calls.  Quantize to whole seconds so repeated observations in one boot
    # produce the same session identity.
    boot_time = datetime.fromtimestamp(int(boot), timezone.utc).isoformat()
    return boot_time, hashlib.sha256(boot_time.encode("ascii")).hexdigest()[:12]


class SessionLifecycleLogger:
    """Append-safe JSONL logger with bounded retention and redaction."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, max_events=2000,
                 max_bytes=1_000_000, dedupe_seconds=60):
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
            raise ValueError("max_events must be positive")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 4096:
            raise ValueError("max_bytes must be at least 4096")
        if isinstance(dedupe_seconds, bool) or not isinstance(dedupe_seconds, (int, float)) or dedupe_seconds < 0:
            raise ValueError("dedupe_seconds must be non-negative")
        if path is None:
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            path = base / "ilink-receive-probe" / "session-lifecycle.jsonl"
        self.path = Path(path)
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.dedupe_seconds = float(dedupe_seconds)
        self._lock = threading.RLock()

    def _normalize(self, event_type: str, **fields: Any) -> dict[str, Any]:
        if event_type not in LIFECYCLE_EVENT_TYPES:
            raise ValueError("unsupported lifecycle event type")
        status = fields.get("http_status")
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        pc_state = _code(fields.get("pc_wechat_state"), 32) or "UNKNOWN"
        if pc_state not in _PC_STATES:
            pc_state = "UNKNOWN"
        return {
            "timestamp": _iso(fields.get("timestamp")),
            "event_type": event_type,
            "bot_fingerprint": _fingerprint(fields.get("bot_fingerprint")),
            "account_fingerprint": _fingerprint(fields.get("account_fingerprint")),
            "credential_fingerprint": _fingerprint(fields.get("credential_fingerprint")),
            "pc_wechat_state": pc_state,
            "boot_session": _fingerprint(fields.get("boot_session")),
            "boot_time": _code(fields.get("boot_time"), 64),
            "session_health": _code(fields.get("session_health"), 40),
            "inbound_result": _code(fields.get("inbound_result"), 40),
            "proactive_result": _code(fields.get("proactive_result"), 40),
            "http_status": status,
            "ret": _numeric(fields.get("ret")),
            "errcode": _numeric(fields.get("errcode")),
            "errmsg": sanitize_errmsg(fields.get("errmsg")),
            "response_keys": _keys(fields.get("response_keys")),
            "last_verified_healthy_at": _code(fields.get("last_verified_healthy_at"), 64),
            "idle_seconds": _numeric(fields.get("idle_seconds")),
            "error_class": _code(fields.get("error_class"), 64),
            "previous_boot_session": _fingerprint(fields.get("previous_boot_session")),
            "last_known_session_health_before_boot": _code(
                fields.get("last_known_session_health_before_boot"), 40
            ),
            "first_observed_session_health": _code(
                fields.get("first_observed_session_health"), 40
            ),
        }

    def record(self, event_type: str, **fields: Any) -> SessionLifecycleEvent:
        payload = self._normalize(event_type, **fields)
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        line = encoded + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._is_duplicate(payload):
                return SessionLifecycleEvent(
                    timestamp=payload["timestamp"], event_type=payload["event_type"],
                    bot_fingerprint=payload["bot_fingerprint"],
                    account_fingerprint=payload["account_fingerprint"],
                    credential_fingerprint=payload["credential_fingerprint"],
                    pc_wechat_state=payload["pc_wechat_state"],
                    boot_session=payload["boot_session"], boot_time=payload["boot_time"],
                    session_health=payload["session_health"],
                    inbound_result=payload["inbound_result"],
                    proactive_result=payload["proactive_result"],
                    http_status=payload["http_status"], ret=payload["ret"],
                    errcode=payload["errcode"], errmsg=payload["errmsg"],
                    response_keys=tuple(payload["response_keys"]),
                    last_verified_healthy_at=payload["last_verified_healthy_at"],
                    idle_seconds=payload["idle_seconds"], error_class=payload["error_class"],
                    previous_boot_session=payload["previous_boot_session"],
                    last_known_session_health_before_boot=payload[
                        "last_known_session_health_before_boot"
                    ],
                    first_observed_session_health=payload["first_observed_session_health"],
                )
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._bound_file()
        return SessionLifecycleEvent(
            timestamp=payload["timestamp"], event_type=payload["event_type"],
            bot_fingerprint=payload["bot_fingerprint"],
            account_fingerprint=payload["account_fingerprint"],
            credential_fingerprint=payload["credential_fingerprint"],
            pc_wechat_state=payload["pc_wechat_state"],
            boot_session=payload["boot_session"], boot_time=payload["boot_time"],
            session_health=payload["session_health"],
            inbound_result=payload["inbound_result"],
            proactive_result=payload["proactive_result"],
            http_status=payload["http_status"], ret=payload["ret"],
            errcode=payload["errcode"], errmsg=payload["errmsg"],
            response_keys=tuple(payload["response_keys"]),
            last_verified_healthy_at=payload["last_verified_healthy_at"],
            idle_seconds=payload["idle_seconds"], error_class=payload["error_class"],
            previous_boot_session=payload["previous_boot_session"],
            last_known_session_health_before_boot=payload[
                "last_known_session_health_before_boot"
            ],
            first_observed_session_health=payload["first_observed_session_health"],
        )

    @staticmethod
    def _dedupe_key(payload: Mapping[str, Any]) -> str:
        comparable = dict(payload)
        comparable.pop("timestamp", None)
        return json.dumps(comparable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def _is_duplicate(self, payload: Mapping[str, Any]) -> bool:
        if self.dedupe_seconds <= 0 or not self.path.exists():
            return False
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if not lines:
                return False
            previous = json.loads(lines[-1])
            if not isinstance(previous, dict) or self._dedupe_key(previous) != self._dedupe_key(payload):
                return False
            previous_at = datetime.fromisoformat(str(previous.get("timestamp")))
            current_at = datetime.fromisoformat(str(payload.get("timestamp")))
            if previous_at.tzinfo is None:
                previous_at = previous_at.replace(tzinfo=timezone.utc)
            if current_at.tzinfo is None:
                current_at = current_at.replace(tzinfo=timezone.utc)
            return abs((current_at - previous_at).total_seconds()) <= self.dedupe_seconds
        except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _bound_file(self) -> None:
        try:
            if self.path.stat().st_size <= self.max_bytes:
                return
            lines = self.path.read_text(encoding="utf-8").splitlines()[-self.max_events:]
            fd, temporary = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write("\n".join(lines) + ("\n" if lines else ""))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except (OSError, UnicodeDecodeError):
            # A diagnostic logger must never break the receive/send path.
            return

    def list_events(self, *, limit=100) -> tuple[dict[str, Any], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return ()
        events = []
        for line in lines[-min(limit, self.max_events):]:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get("event_type") in LIFECYCLE_EVENT_TYPES:
                events.append(value)
        return tuple(events)

    def timeline(self) -> dict[str, Any]:
        events = sorted(
            self.list_events(limit=self.max_events),
            key=lambda item: (
                _parse_timestamp(item.get("timestamp"))
                or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("timestamp") or ""),
            ),
        )
        def last(event_types):
            for event in reversed(events):
                if event.get("event_type") in event_types:
                    return event.get("timestamp")
            return None
        last_inbound = next(
            (
                event.get("timestamp") for event in reversed(events)
                if event.get("event_type") == "INBOUND_PROBE_OK"
                or (
                    event.get("event_type") == "HEALTH_CHECK_RESULT"
                    and event.get("inbound_result") == "OK"
                )
            ),
            None,
        )
        faults = {
            "INBOUND_PROBE_FAILED", "PROACTIVE_SMOKE_FAILED",
            "SESSION_PREPARE_FAILED",
        }
        first_fault = next(
            (
                e.get("timestamp") for e in events
                if e.get("event_type") in faults
                or (
                    e.get("event_type") == "SESSION_HEALTH_TRANSITION"
                    and (
                        e.get("session_health") not in (None, "HEALTHY")
                        or e.get("error_class") not in (None, "NONE", "")
                    )
                )
            ),
            None,
        )
        boot = next((e for e in reversed(events) if e.get("event_type") == "WINDOWS_BOOT_OBSERVED"), None)
        return {
            "auth_staged_at": last({"AUTH_STAGED"}),
            "auth_promoted_at": last({"AUTH_PROMOTED"}),
            "last_inbound_success_at": last_inbound,
            "last_proactive_success_at": last({"PROACTIVE_SMOKE_OK"}),
            "first_fault_at": first_fault,
            "boot_session": boot.get("boot_session") if boot else None,
            "boot_time": boot.get("boot_time") if boot else None,
            "event_count": len(events),
            "rows": tuple({
                "timestamp": event.get("timestamp"),
                "event": event.get("event_type"),
                "health": event.get("session_health"),
                "bot_fingerprint": event.get("bot_fingerprint"),
                "boot_session": event.get("boot_session"),
                "idle_seconds": event.get("idle_seconds"),
                "error_class": event.get("error_class"),
            } for event in events),
        }

    def correlate_faults(self) -> tuple[dict[str, Any], ...]:
        """Return time-distance facts around prepare-failed events only."""
        events = sorted(
            self.list_events(limit=self.max_events),
            key=lambda item: (
                _parse_timestamp(item.get("timestamp"))
                or datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("timestamp") or ""),
            ),
        )
        def is_anchor(event: Mapping[str, Any], name: str) -> bool:
            event_type = event.get("event_type")
            if name == "auth":
                return event_type in {"AUTH_STAGED", "AUTH_PROMOTED"}
            if name == "inbound":
                return event_type == "INBOUND_PROBE_OK" or (
                    event_type == "HEALTH_CHECK_RESULT"
                    and event.get("inbound_result") == "OK"
                )
            if name == "proactive":
                return event_type == "PROACTIVE_SMOKE_OK"
            if name == "health":
                return event_type in {"HEALTH_CHECK_RESULT", "SESSION_HEALTH_TRANSITION"}
            return False

        output = []
        for index, fault in enumerate(events):
            if fault.get("event_type") != "SESSION_PREPARE_FAILED":
                continue
            fault_at = _parse_timestamp(fault.get("timestamp"))
            row = {
                "timestamp": fault.get("timestamp"),
                "error_class": fault.get("error_class"),
                "pc_wechat_state": fault.get("pc_wechat_state"),
                "boot_session": fault.get("boot_session"),
                "idle_seconds": fault.get("idle_seconds"),
                "reboot_happened_since_last_healthy": "UNKNOWN",
            }
            prior_healthy_index = None
            prior_healthy = None
            for prior_index in range(index - 1, -1, -1):
                candidate = events[prior_index]
                if (
                    candidate.get("event_type") == "INBOUND_PROBE_OK"
                    or (
                        candidate.get("event_type") == "HEALTH_CHECK_RESULT"
                        and candidate.get("inbound_result") == "OK"
                    )
                    or (
                        candidate.get("event_type") == "SESSION_HEALTH_TRANSITION"
                        and candidate.get("session_health") == "HEALTHY"
                    )
                ):
                    prior_healthy_index = prior_index
                    prior_healthy = candidate
                    break
            if prior_healthy is not None:
                before_boot = prior_healthy.get("boot_session")
                fault_boot = fault.get("boot_session")
                if before_boot and fault_boot:
                    row["reboot_happened_since_last_healthy"] = (
                        "NO" if before_boot == fault_boot else "YES"
                    )
                elif prior_healthy_index is not None:
                    row["reboot_happened_since_last_healthy"] = (
                        "YES" if any(
                            event.get("event_type") == "WINDOWS_REBOOT_DETECTED"
                            for event in events[prior_healthy_index + 1:index]
                        ) else "UNKNOWN"
                    )

            for name in ("auth", "inbound", "proactive", "health"):
                previous = next(
                    (event for event in reversed(events[:index])
                     if is_anchor(event, name)), None
                )
                delta = None
                if previous is not None and fault_at is not None:
                    at = _parse_timestamp(previous.get("timestamp"))
                    if at is not None:
                        delta = max(0.0, (fault_at - at).total_seconds())
                row[f"seconds_since_{name}"] = delta
                if name == "health":
                    row["seconds_since_last_health_check"] = delta
            if row["idle_seconds"] is None and fault_at is not None:
                inbound = next(
                    (event for event in reversed(events[:index])
                     if is_anchor(event, "inbound")), None
                )
                inbound_at = _parse_timestamp(inbound.get("timestamp")) if inbound else None
                if inbound_at is not None:
                    row["idle_seconds"] = max(0.0, (fault_at - inbound_at).total_seconds())
            output.append(row)
        return tuple(output)


def record_boot_observation(logger: SessionLifecycleLogger, *, now: datetime | None = None,
                            pc_wechat_state="UNKNOWN", session_health=None,
                            bot_fingerprint=None, account_fingerprint=None,
                            credential_fingerprint=None,
                            uptime_ms: int | None = None) -> SessionLifecycleEvent | None:
    boot_time, boot_session = _boot_snapshot(now, uptime_ms=uptime_ms)
    if boot_time is None or boot_session is None:
        return None
    prior = logger.timeline()
    previous_boot = prior.get("boot_session")
    prior_rows = prior.get("rows", ())
    last_known_health = next(
        (row.get("health") for row in reversed(prior_rows) if row.get("health")),
        None,
    )
    if previous_boot and previous_boot != boot_session:
        logger.record(
            "WINDOWS_REBOOT_DETECTED", timestamp=now, boot_time=boot_time,
            boot_session=boot_session, previous_boot_session=previous_boot,
            last_known_session_health_before_boot=last_known_health,
            first_observed_session_health=session_health,
            pc_wechat_state=pc_wechat_state, session_health=session_health,
            bot_fingerprint=bot_fingerprint, account_fingerprint=account_fingerprint,
            credential_fingerprint=credential_fingerprint,
        )
    return logger.record(
        "WINDOWS_BOOT_OBSERVED", timestamp=now, boot_time=boot_time,
        boot_session=boot_session, pc_wechat_state=pc_wechat_state,
        last_known_session_health_before_boot=last_known_health,
        first_observed_session_health=session_health,
        session_health=session_health, bot_fingerprint=bot_fingerprint,
        account_fingerprint=account_fingerprint,
        credential_fingerprint=credential_fingerprint,
    )


__all__ = [
    "LIFECYCLE_EVENT_TYPES", "SessionLifecycleEvent", "SessionLifecycleLogger",
    "record_boot_observation",
]
