"""Explicit, receipt-aware manual send coordinator for REVIEW."""

from dataclasses import dataclass
from collections.abc import Mapping

from ilink_receive_probe.probe.send_result import (
    AMBIGUOUS,
    ClassifiedSendResult,
    classify_send_response,
)

from .audit import SEND_EVENT
from .drafts import APPROVED, DraftConflictError, DraftStore


class SendCoordinatorError(RuntimeError):
    pass


class SendConfirmationRequired(SendCoordinatorError):
    pass


class ContextUnavailableError(SendCoordinatorError):
    pass


class SendNotAllowedError(SendCoordinatorError):
    pass


@dataclass(frozen=True)
class SendOutcome:
    draft_id: str
    status: str
    send_state: str
    confirmed: bool
    retry_allowed: bool
    receipt_id: str | None = None


class ManualSendCoordinator:
    """Send only a user-approved REVIEW draft for a still-REVIEW contact."""

    def __init__(self, drafts, transport, contact_store, context_resolver, *, audit=None):
        if not isinstance(drafts, DraftStore):
            raise ValueError("a DraftStore is required")
        if not transport or not hasattr(transport, "send"):
            raise ValueError("a receipt-aware transport is required")
        if not contact_store or not hasattr(contact_store, "get"):
            raise ValueError("a contact store is required")
        if not callable(context_resolver):
            raise ValueError("a context resolver is required")
        self.drafts = drafts
        self.transport = transport
        self.contact_store = contact_store
        self.context_resolver = context_resolver
        self.audit = audit
        self.audit_errors = []

    def _audit(self, **kwargs):
        if self.audit is None:
            return
        try:
            self.audit.record(SEND_EVENT, **kwargs)
        except Exception as error:
            # Audit failure must not change a receipt-aware send outcome.
            self.audit_errors.append(type(error).__name__)

    @staticmethod
    def _contact_parts(contact_key):
        parts = contact_key.split("\x1f") if isinstance(contact_key, str) else ()
        if len(parts) != 3 or not all(parts):
            raise SendNotAllowedError("draft contact identity is not bound")
        return parts

    def _authorize_review_contact(self, contact_key):
        account_scope, sender_fingerprint, bot_fingerprint = self._contact_parts(contact_key)
        contact = self.contact_store.get(account_scope, sender_fingerprint, bot_fingerprint)
        if not contact or contact.state != "allowed" or contact.mode != "review":
            raise SendNotAllowedError("manual send requires an allowed REVIEW contact")
        return account_scope, sender_fingerprint, bot_fingerprint

    @staticmethod
    def _validate_context_identity(context, expected):
        if not isinstance(context, Mapping):
            raise ContextUnavailableError("reply context is unavailable")
        if not context.get("recipient") or not context.get("context_token"):
            raise ContextUnavailableError("reply context is unavailable")
        identity = tuple(context.get(key) for key in (
            "account_scope", "sender_fingerprint", "bot_fingerprint",
        ))
        if identity != expected:
            raise ContextUnavailableError("reply context identity does not match draft contact")
        if context.get("conversation_type") not in (None, "one_to_one"):
            raise ContextUnavailableError("reply context is not one-to-one")
        if context.get("is_forwarded") is True:
            raise ContextUnavailableError("forwarded context cannot be sent")

    def send_approved(self, draft_id, *, confirmed=False, now=None):
        if not confirmed:
            raise SendConfirmationRequired("manual send requires explicit confirmation")
        row = self.drafts.get(draft_id)
        if not row:
            raise KeyError("draft not found")
        if row["status"] != APPROVED:
            raise DraftConflictError("only an approved draft can be sent")
        expected_identity = self._authorize_review_contact(row["contact_key"])
        try:
            context = self.context_resolver(row["context_ref"])
        except Exception as error:
            raise ContextUnavailableError("reply context is unavailable") from error
        self._validate_context_identity(context, expected_identity)

        sending = self.drafts.begin_send(draft_id, now=now)
        try:
            result = self.transport.send(context, draft_id, sending["final_text"])
            if isinstance(result, ClassifiedSendResult):
                pass
            elif isinstance(result, Mapping):
                # Keep the boundary tolerant of a small hand-written adapter,
                # but classify raw responses before they reach the draft
                # state machine.  Unknown shapes are still fail-closed.
                result = classify_send_response(result)
            else:
                raise TypeError("transport returned an invalid send result")
        except Exception:
            # The transport may have reached the server before a local error;
            # never make that state look retryable or confirmed.
            result = ClassifiedSendResult(AMBIGUOUS, False, False)
        stored = self.drafts.record_send_state(draft_id, result.state, now=now)
        self._audit(
            occurred_at=now,
            account_scope=expected_identity[0],
            contact_key=row["contact_key"],
            draft_id=draft_id,
            send_state=result.state,
            detail=result.state,
        )
        return SendOutcome(
            draft_id=draft_id,
            status=stored["status"],
            send_state=result.state,
            confirmed=result.confirmed,
            retry_allowed=result.retry_allowed,
            receipt_id=result.receipt_id,
        )
