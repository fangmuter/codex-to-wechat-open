"""Explicit bridge from the existing iLink receiver to Chat Agent drafts."""

from dataclasses import dataclass
from datetime import datetime, timezone

from .audit import RECOVERY_EVENT
from .drafts import NEEDS_USER_INPUT, PENDING
from .notifications import NotificationEvent, RecoveryNotification
from .router import IGNORED_CONTACT

try:
    from ilink_receive_probe.probe.receiver import WECHAT_UNAVAILABLE
except ImportError:  # pragma: no cover - package import fallback
    WECHAT_UNAVAILABLE = "WECHAT_UNAVAILABLE"


@dataclass(frozen=True)
class ChatAgentPollResult:
    receiver_state: str
    accepted_count: int
    ingested: tuple
    flushed: tuple
    recovered: bool = False
    missed_count: int = 0


class ChatAgentReceiverAdapter:
    """Compose an existing receiver with the isolated draft pipeline.

    This adapter does not advance cursors itself, send replies, or catch up
    missed history.  ``Receiver.receive_once`` remains the sole owner of
    transport polling and cursor advancement.
    """

    def __init__(self, receiver, pipeline, *, context_ref_resolver=None, notification_sink=None, pairing_observer=None):
        self.receiver = receiver
        self.pipeline = pipeline
        self.context_ref_resolver = context_ref_resolver or self._stored_context_ref
        self.notification_sink = notification_sink
        self.notification_errors = []
        self.audit = getattr(pipeline, "audit", None)
        self.audit_errors = []
        self._pairing_observer_uses_time = False
        if pairing_observer is None:
            # The default composition records a bounded candidate in the
            # Chat Agent router's pairing store, but it still never creates a
            # contact or enables a mode.  Callers can pass an explicit sink
            # when they want a separate audit/UI integration.
            candidate_router = getattr(pipeline, "router", None)
            pairing_observer = getattr(candidate_router, "pair_candidate", None)
            self._pairing_observer_uses_time = callable(pairing_observer)
        self.pairing_observer = pairing_observer if callable(pairing_observer) else None
        self._transport_unavailable = getattr(receiver, "state", None) == WECHAT_UNAVAILABLE

    def _audit_recovery(self, *, occurred_at, missed_count):
        if self.audit is None:
            return
        try:
            self.audit.record(
                RECOVERY_EVENT,
                occurred_at=occurred_at,
                account_scope=getattr(self.receiver, "account_scope", None),
                source_count=missed_count,
                detail="WECHAT_RECOVERED",
            )
        except Exception as error:
            self.audit_errors.append(type(error).__name__)

    def _stored_context_ref(self, event):
        storage = getattr(self.receiver, "storage", None)
        if storage is None or not hasattr(storage, "inbound"):
            return None
        row = storage.inbound(event.account_scope, event.message_id)
        if not row:
            return None
        try:
            value = row["context_ref"]
        except (KeyError, IndexError, TypeError):
            return None
        return value if isinstance(value, str) and value.strip() else None

    def process_once(self, *, now=None):
        poll_now = now or datetime.now(timezone.utc)
        was_unavailable = self._transport_unavailable
        accepted = self.receiver.receive_once()
        acknowledged_events = tuple(accepted)
        receiver_state = getattr(self.receiver, "state", "UNKNOWN")
        recovered = was_unavailable and receiver_state != WECHAT_UNAVAILABLE
        self._transport_unavailable = receiver_state == WECHAT_UNAVAILABLE
        missed_count = 0
        if recovered:
            # The receiver has already advanced its transport cursor. Do not
            # feed this recovery batch into contacts, history, or the model.
            missed_count = len(getattr(self.receiver, "last_events", ()) or accepted)
            accepted = []
        ingested = []
        flushed = []
        for event in accepted:
            ingested_result = self.pipeline.ingest(
                    event,
                    context_ref=self.context_ref_resolver(event),
                    now=poll_now,
            )
            ingested.append(ingested_result)
            if ingested_result.route == IGNORED_CONTACT and self.pairing_observer:
                if self._pairing_observer_uses_time:
                    self.pairing_observer(event, observed_at=poll_now)
                else:
                    self.pairing_observer(event)
            flushed.extend(self.pipeline.last_flush_results)
        flushed.extend(self.pipeline.flush(now=poll_now))
        unique_flushed = []
        seen = set()
        for result in flushed:
            identity = (result.batch_id, result.draft_id, result.status)
            if identity in seen:
                continue
            seen.add(identity)
            unique_flushed.append(result)
            if (
                self.notification_sink
                and result.draft_id
                and result.status in (PENDING, NEEDS_USER_INPUT)
            ):
                try:
                    self.notification_sink.notify(
                        NotificationEvent(result.draft_id, result.status, result.reason)
                    )
                except Exception as error:
                    # Keep exception details out of the local observability
                    # surface; callback errors may contain message content.
                    self.notification_errors.append(type(error).__name__)
        if recovered and missed_count and self.notification_sink:
            try:
                self.notification_sink.notify(RecoveryNotification(missed_count))
            except Exception as error:
                self.notification_errors.append(type(error).__name__)
        if recovered and missed_count:
            self._audit_recovery(occurred_at=poll_now, missed_count=missed_count)
        acknowledge = getattr(self.receiver, "acknowledge", None)
        if callable(acknowledge):
            acknowledge(acknowledged_events)
        return ChatAgentPollResult(
            receiver_state=receiver_state,
            accepted_count=len(accepted),
            ingested=tuple(ingested),
            flushed=tuple(unique_flushed),
            recovered=recovered,
            missed_count=missed_count,
        )
