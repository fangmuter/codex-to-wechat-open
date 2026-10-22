"""Fresh inbound context binding for the Web-First production runner.

The production runner must never guess a "latest" context token.  This small
gate accepts one newly observed, identity-matched direct event and binds the
exact event to the DPAPI-backed :class:`ReplyContextStore` reference created by
the receive adapter.  The token is kept in memory only until the one delivery
attempt finishes (or the bounded context expires).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from ilink_receive_probe.probe.models import InboundEvent
from ilink_receive_probe.probe.context_utils import (
    as_datetime as _as_datetime,
    load_reply_context_record,
)


@dataclass(frozen=True)
class ProductionInboundContext:
    """Exact inbound/context binding; raw token is intentionally hidden."""

    event_id: str
    context_ref: str
    created_at: datetime
    expires_at: datetime
    context_token: str = field(repr=False)

    @property
    def age_seconds(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.created_at).total_seconds())

    def as_transport_context(self) -> Mapping[str, Any]:
        """Return the in-memory context record required by a transport.

        Callers must not log or persist this mapping.  The transport only uses
        ``context_token``; the reference and event id remain available to the
        runner's final-gate/diagnostic logic.
        """

        return {
            "context_ref": self.context_ref,
            "message_id": self.event_id,
            "context_token": self.context_token,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ProductionInboundContextGate:
    """Bind at most one fresh accepted inbound event per runtime instance."""

    def __init__(
        self,
        *,
        runner_started_at: datetime,
        expected_sender_fingerprint: str | None,
        expected_bot_fingerprint: str | None,
        context_store: Any,
        context_ref_for_event: Callable[[InboundEvent], str | None],
        existing_context_refs: Iterable[str] = (),
        now: Callable[[], datetime] | None = None,
    ):
        started = _as_datetime(runner_started_at)
        if started is None:
            raise ValueError("runner_started_at is invalid")
        self.runner_started_at = started
        self.expected_sender_fingerprint = expected_sender_fingerprint
        self.expected_bot_fingerprint = expected_bot_fingerprint
        self.context_store = context_store
        self.context_ref_for_event = context_ref_for_event
        self.existing_context_refs = set(existing_context_refs)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.binding: ProductionInboundContext | None = None
        self.last_rejection: str | None = None

    @property
    def waiting_for_context(self) -> bool:
        return self.binding is None

    def _current(self, value: datetime | None = None) -> datetime:
        current = _as_datetime(value if value is not None else self.now())
        if current is None:
            raise ValueError("context clock is invalid")
        return current

    def _reject(self, reason: str) -> None:
        # Only bounded reason labels are retained; no message text or token.
        self.last_rejection = reason[:80]

    def accept(self, events: Iterable[InboundEvent], *, now: datetime | None = None) -> bool:
        """Accept the first new direct event with an exact DPAPI context ref."""

        if self.binding is not None:
            self._reject("INBOUND_ALREADY_BOUND")
            return False
        current = self._current(now)
        for event in events or ():
            event_time = _as_datetime(event.received_at)
            if event_time is None:
                # A legacy/ill-formed row without an ingestion timestamp is
                # never eligible as fresh context.  Do not substitute the
                # current clock: that would turn historical data into a new
                # inbound event and weaken the runner-start boundary.
                self._reject("INBOUND_TIMESTAMP_UNAVAILABLE")
                continue
            if event_time <= self.runner_started_at:
                self._reject("EVENT_BEFORE_RUNNER_START")
                continue
            if event.conversation_type != "one_to_one" or event.is_forwarded:
                self._reject("DIRECT_MESSAGE_REQUIRED")
                continue
            if (
                self.expected_sender_fingerprint is not None
                and event.sender_fingerprint != self.expected_sender_fingerprint
            ):
                self._reject("SENDER_MISMATCH")
                continue
            if (
                self.expected_bot_fingerprint is not None
                and event.bot_id != self.expected_bot_fingerprint
            ):
                self._reject("BOT_MISMATCH")
                continue
            try:
                context_ref = self.context_ref_for_event(event)
            except Exception:
                context_ref = None
            if not isinstance(context_ref, str) or not context_ref.strip():
                self._reject("CONTEXT_REF_MISSING")
                continue
            if context_ref in self.existing_context_refs:
                self._reject("OLD_CONTEXT_IGNORED")
                continue
            try:
                record = load_reply_context_record(self.context_store, context_ref)
            except Exception:
                self._reject("CONTEXT_UNAVAILABLE")
                continue
            if record.get("message_id") != event.message_id:
                self._reject("CONTEXT_EVENT_MISMATCH")
                continue
            if record.get("account_scope") not in (None, event.account_scope):
                self._reject("CONTEXT_ACCOUNT_MISMATCH")
                continue
            if record.get("sender_fingerprint") not in (None, event.sender_fingerprint):
                self._reject("CONTEXT_SENDER_MISMATCH")
                continue
            if record.get("bot_fingerprint") not in (None, event.bot_id):
                self._reject("CONTEXT_BOT_MISMATCH")
                continue
            token = record.get("context_token")
            if not isinstance(token, str) or not token:
                self._reject("CONTEXT_TOKEN_MISSING")
                continue
            if event.context_token is not None and event.context_token != token:
                self._reject("CONTEXT_TOKEN_MISMATCH")
                continue
            created = _as_datetime(record.get("created_at"))
            expires = _as_datetime(record.get("expires_at"))
            if created is None or expires is None:
                self._reject("CONTEXT_TIMESTAMP_INVALID")
                continue
            age = max(0.0, (current - created).total_seconds())
            if current >= expires or age >= max(0.0, (expires - created).total_seconds()):
                self._reject("CONTEXT_TOO_OLD")
                try:
                    self.context_store.consume(context_ref)
                except Exception:
                    pass
                continue
            self.existing_context_refs.add(context_ref)
            self.binding = ProductionInboundContext(
                event_id=event.message_id,
                context_ref=context_ref,
                created_at=created,
                expires_at=expires,
                context_token=token,
            )
            self.last_rejection = None
            return True
        return False

    def expire_if_needed(self, *, now: datetime | None = None) -> bool:
        """Consume and clear an expired binding without attempting delivery."""

        if self.binding is None:
            return False
        current = self._current(now)
        if current < self.binding.expires_at:
            return False
        self.consume()
        self.last_rejection = "CONTEXT_TOO_OLD"
        return True

    def transport_context(self) -> Mapping[str, Any] | None:
        if self.binding is None:
            return None
        return self.binding.as_transport_context()

    def consume(self) -> None:
        """Invalidate the exact context reference after its one-shot use."""

        if self.binding is None:
            return
        ref = self.binding.context_ref
        try:
            self.context_store.consume(ref)
        finally:
            self.binding = None


__all__ = ["ProductionInboundContext", "ProductionInboundContextGate"]
