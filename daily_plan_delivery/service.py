"""Selection, idempotency, and one-shot send for confirmed Delivery plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    DELIVERY_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    DELIVERY_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

from .formatter import format_delivery_plan
from .inbox import DeliveryInbox, DeliveryInboxEntry
from .models import DeliveryPlan
from .store import DeliveryLedger, DeliveryLedgerConflict


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    target_date: str
    plan_id: str | None = None
    source_filename: str | None = None
    payload_hash: str | None = None
    send_state: str | None = None
    rejected_count: int = 0
    duplicate: bool = False


def payload_hash(plan: DeliveryPlan) -> str:
    encoded = json.dumps(
        plan.normalized_dict(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DeliveryService:
    def __init__(self, inbox: DeliveryInbox, ledger: DeliveryLedger, transport,
                 *, preflight=None, send_observer=None):
        self.inbox = inbox
        self.ledger = ledger
        self.transport = transport
        # Optional production-only hooks.  The core Delivery package remains
        # unaware of iLink, Roadmap, Reading, or any session state machine.
        self.preflight = preflight
        self.send_observer = send_observer
        self.last_scan_diagnostics: dict[str, object] = {}

    def _record_scan_diagnostics(self, target, entries, rejected, valid):
        """Expose bounded, non-content scan facts to the production logger."""
        diagnostics = dict(getattr(self.inbox, "last_scan_diagnostics", {}))
        categories: dict[str, int] = {}
        target_candidates = 0
        for entry in entries:
            if entry.plan is None:
                category = entry.error_category or "INVALID_SCHEMA"
                categories[category] = categories.get(category, 0) + 1
                continue
            if entry.plan.target_date == target:
                target_candidates += 1
            else:
                categories["WRONG_DATE"] = categories.get("WRONG_DATE", 0) + 1
        if diagnostics.get("inbox_accessible") is False:
            categories["UNREADABLE"] = categories.get("UNREADABLE", 0) + 1
        diagnostics.update({
            "target_date_candidate_count": target_candidates,
            "valid_target_candidate_count": len(valid),
            "reject_reason_category": ",".join(
                f"{key}:{categories[key]}" for key in sorted(categories)
            ) or "NONE",
        })
        self.last_scan_diagnostics = diagnostics

    @staticmethod
    def _target_date(now: datetime) -> str:
        return (now.astimezone(DELIVERY_TZ).date() + timedelta(days=1)).isoformat()

    @staticmethod
    def _send_state(result) -> str:
        return str(getattr(result, "state", "ACKED_API"))

    def deliver_tomorrow(self, now: datetime, *, target_date: str | None = None,
                         authorized_retry_plan_id: str | None = None,
                         authorized_retry_hash: str | None = None,
                         authorized_retry_target_date: str | None = None,
                         require_context: bool = False,
                         context_token: str | None = None) -> DeliveryResult:
        if authorized_retry_target_date is not None and not (
            authorized_retry_plan_id and authorized_retry_hash
        ):
            raise ValueError("exact target date requires an authorized retry")
        if authorized_retry_target_date is not None:
            try:
                date.fromisoformat(authorized_retry_target_date)
            except (TypeError, ValueError) as error:
                raise ValueError("authorized retry target date must be ISO date") from error
        if target_date is not None:
            try:
                target_date = date.fromisoformat(target_date).isoformat()
            except (TypeError, ValueError) as error:
                raise ValueError("target date must be ISO date") from error
        if target_date is not None and authorized_retry_target_date is not None and target_date != authorized_retry_target_date:
            raise ValueError("target date conflicts with authorized retry target date")
        target = authorized_retry_target_date or target_date or self._target_date(now)
        entries = self.inbox.scan()
        rejected = 0
        valid: list[DeliveryInboxEntry] = []
        for entry in entries:
            if entry.plan is None:
                rejected += 1
                # A DriveFS hydration/read failure is transient.  Keep it out
                # of the permanent rejection ledger so a later poll can retry
                # the same file after its contents become available.
                if entry.error_category != "UNREADABLE":
                    self.ledger.reject(entry.path.name, entry.error or "invalid delivery", now)
            elif entry.plan.target_date == target:
                valid.append(entry)

        self._record_scan_diagnostics(target, entries, rejected, valid)

        by_id: dict[str, tuple[DeliveryInboxEntry, str]] = {}
        for entry in valid:
            assert entry.plan is not None
            digest = payload_hash(entry.plan)
            previous = by_id.get(entry.plan.plan_id)
            if previous and previous[1] != digest:
                return DeliveryResult("CONFLICT_REJECT", target, entry.plan.plan_id,
                                      entry.path.name, digest, rejected_count=rejected)
            by_id.setdefault(entry.plan.plan_id, (entry, digest))

        if not by_id:
            return DeliveryResult("NO_CONFIRMED_PLAN", target, rejected_count=rejected)
        if len(by_id) != 1:
            return DeliveryResult("CONFLICT_REJECT", target, rejected_count=rejected)

        entry, digest = next(iter(by_id.values()))
        assert entry.plan is not None

        existing = self.ledger.get(entry.plan.plan_id)
        if existing:
            if existing["payload_hash"] != digest:
                return DeliveryResult("CONFLICT_REJECT", target, entry.plan.plan_id,
                                      entry.path.name, digest, rejected_count=rejected)
            if existing["delivery_result"] == "DELIVERED":
                return DeliveryResult("NOOP", target, entry.plan.plan_id,
                                      entry.path.name, digest, "ACKED_API",
                                      rejected_count=rejected, duplicate=True)
            # SENDING/UNKNOWN/FAILED remain closed unless the operator passes
            # the exact plan id and normalized hash for one explicit retry.
            authorized = (
                authorized_retry_plan_id == entry.plan.plan_id
                and authorized_retry_hash == digest
                and existing["delivery_result"] == "FAILED"
            )
            if not authorized:
                return DeliveryResult("NOOP_EXISTING", target, entry.plan.plan_id,
                                      entry.path.name, digest, existing["delivery_result"],
                                      rejected_count=rejected, duplicate=True)
            retry_authorized = True
        else:
            retry_authorized = False

        # Web-First production requires an exact fresh inbound context before
        # a first attempt.  This gate comes after idempotency checks so a
        # DELIVERED plan is still an immediate NOOP and FAILED/UNKNOWN plans
        # remain closed; with no context, no health gate, ledger begin, or
        # transport call is reached.
        if require_context and not isinstance(context_token, str):
            return DeliveryResult(
                "WAITING_FOR_FRESH_CONTEXT", target, entry.plan.plan_id,
                entry.path.name, digest, rejected_count=rejected,
            )
        if require_context and not context_token:
            return DeliveryResult(
                "WAITING_FOR_FRESH_CONTEXT", target, entry.plan.plan_id,
                entry.path.name, digest, rejected_count=rejected,
            )

        # Run the session health gate before touching either ledger or calling
        # sendmessage.  A failed gate therefore leaves submitted=0 and lets
        # the bounded production runner decide whether to wait or exit.  The
        # gate is deliberately after the idempotency NOOP checks: a plan that
        # was already delivered never needs another network health call.
        if self.preflight is not None:
            try:
                preflight = self.preflight(now, entry.plan, digest)
            except Exception:
                # A broken health hook must fail closed before either ledger
                # or transport changes.  This is deliberately not recorded as
                # a delivery attempt because no send was submitted.
                return DeliveryResult(
                    "BLOCKED_SESSION_UNKNOWN", target, entry.plan.plan_id,
                    entry.path.name, digest, "UNKNOWN", rejected_count=rejected,
                )
            if not getattr(preflight, "allowed", False):
                outcome = getattr(preflight, "outcome", "BLOCKED_SESSION_DEGRADED")
                return DeliveryResult(
                    outcome, target, entry.plan.plan_id, entry.path.name, digest,
                    getattr(preflight, "state", None), rejected_count=rejected,
                )

        try:
            started = self.ledger.begin(
                entry.plan.plan_id, digest, entry.path.name, now,
                allow_failed_retry=retry_authorized,
            )
        except DeliveryLedgerConflict:
            return DeliveryResult("CONFLICT_REJECT", target, entry.plan.plan_id,
                                  entry.path.name, digest, rejected_count=rejected)
        if not started:
            existing_state = existing["delivery_result"] if existing else "SENDING"
            return DeliveryResult("NOOP_EXISTING", target, entry.plan.plan_id,
                                  entry.path.name, digest, existing_state,
                                  rejected_count=rejected, duplicate=bool(existing))
        key = f"daily-plan-delivery:{entry.plan.plan_id}"
        try:
            kwargs = {"retry_failed": True} if retry_authorized else {}
            if context_token is not None:
                kwargs["context_token"] = context_token
            result = self.transport.send(format_delivery_plan(entry.plan), key, now, **kwargs)
            state = self._send_state(result)
            if self.send_observer is not None:
                try:
                    self.send_observer(result, now)
                except Exception:
                    # Health telemetry must never change the established send
                    # and ledger semantics.
                    pass
        except Exception as exc:
            # The send boundary may be ambiguous.  Persist a closed state and
            # never retry blindly after a process restart.
            if self.send_observer is not None:
                try:
                    self.send_observer(SimpleNamespace(
                        state="UNKNOWN", http_status=None, ret=None,
                        errcode=None, errmsg=type(exc).__name__,
                        response_keys=(),
                    ), now)
                except Exception:
                    pass
            self.ledger.record(entry.plan.plan_id, "UNKNOWN", now, reason=type(exc).__name__)
            return DeliveryResult("UNKNOWN", target, entry.plan.plan_id,
                                  entry.path.name, digest, "UNKNOWN",
                                  rejected_count=rejected)
        if state == "ACKED_API":
            self.ledger.record(entry.plan.plan_id, "DELIVERED", now)
            return DeliveryResult("DELIVERED", target, entry.plan.plan_id,
                                  entry.path.name, digest, state,
                                  rejected_count=rejected)
        result_state = "UNKNOWN" if state == "UNKNOWN" else "FAILED"
        self.ledger.record(entry.plan.plan_id, result_state, now, reason=state)
        return DeliveryResult(result_state, target, entry.plan.plan_id,
                              entry.path.name, digest, state,
                              rejected_count=rejected)
