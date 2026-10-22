"""Production entrypoint for delivering an already-confirmed web plan.

This module intentionally has no planner, recommendation, review, or scheduler
dependencies.  The finite runtime only scans the explicit Delivery Inbox,
validates one confirmed plan, formats it, and hands the text to the existing
bound iLink sender.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import time

from daily_plan_delivery import DeliveryInbox, DeliveryLedger as PlanLedger, DeliveryService
from daily_plan_delivery.service import DELIVERY_TZ
from daily_plan_proactive.real_transport import (
    BoundIlinkProactiveTransport,
    DeliveryLedger as SendLedger,
)
from ilink_receive_probe.probe.credential_store import CredentialStore
from ilink_receive_probe.probe.proactive_send import require_expected_recipient_fingerprint
from ilink_receive_probe.probe.recipient_binding import RecipientBindingStore
from ilink_receive_probe.probe.real_transport import RealIlinkTransport
from ilink_receive_probe.probe.receiver import Receiver
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.context_utils import snapshot_context_refs
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.session_health import (
    SessionHealthManager,
    SessionHealthStore,
)

from .lock import ProductionLock
from .context_gate import ProductionInboundContextGate
from .logging import build_logger
from .paths import ProductionPaths


def build_delivery_parts(paths: ProductionPaths | None = None, *,
                         transport=None, token=None, binding_store=None,
                         health_manager=None, health_probe=None,
                         receiver=None, context_store=None):
    """Construct the isolated Delivery Inbox chain without performing a send."""
    paths = paths or ProductionPaths.current_user()
    paths.root.mkdir(parents=True, exist_ok=True)
    transport_was_injected = transport is not None
    # An injected transport is the offline/test boundary. Keep all incidental
    # credential and recipient-store construction under the supplied temporary
    # state root; never inspect the developer's ambient Local AppData metadata.
    isolated_state_dir = paths.root / "ilink-state" if transport_was_injected else None
    credential_store = CredentialStore(isolated_state_dir)
    expected_recipient_fingerprint = (
        None
        if transport_was_injected or not token
        else require_expected_recipient_fingerprint()
    )
    if token is None:
        # Defer credential failure to the health gate.  Constructing a bound
        # transport with an empty in-memory token lets production return the
        # explicit WECHAT_REAUTH_REQUIRED outcome without touching a Delivery
        # ledger; it never permits a send.
        try:
            token = credential_store.load()
        except Exception:
            token = ""
    binding_store = binding_store or RecipientBindingStore(isolated_state_dir)
    send_ledger = SendLedger(paths.delivery_send_ledger_db)
    transport = transport or BoundIlinkProactiveTransport(
        token, binding_store, send_ledger, expected_recipient_fingerprint,
    )
    plan_ledger = PlanLedger(paths.plan_delivery_ledger_db)
    inbox = DeliveryInbox(paths.delivery_inbox_dir)
    health_inbound = None
    metadata = {}
    if not transport_was_injected and credential_store.meta.exists():
        try:
            metadata = json.loads(credential_store.meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
    receiver_transport = None
    if not transport_was_injected:
        # The receive transport is shared by the health preflight and the
        # inbound receiver, but each call remains receive-only.  The receiver
        # advances the persisted cursor; health diagnostics never do.
        health_inbound = ProbeStorage(paths.inbound_db)
        receiver_transport = RealIlinkTransport(token=token)
    if health_manager is None and not transport_was_injected:
        # The installed production path gets a receive-only preflight.  Test
        # transports remain opt-in so existing offline tests never contact the
        # network by accident.
        credential = credential_store
        probe = health_probe or (
            lambda: receiver_transport.getupdates_with_diagnostics(
                health_inbound.cursor()
            )
        )
        health_manager = SessionHealthManager(
            SessionHealthStore(paths.session_health_file),
            account_fingerprint=metadata.get("account_fingerprint"),
            bot_fingerprint=metadata.get("bot_fingerprint"),
            credential_fingerprint=metadata.get("credential_fingerprint"),
            credential_loader=credential.load,
            binding_loader=lambda: binding_store.load_for_proactive_send(
                expected_recipient_fingerprint
            ),
            probe=probe,
        )
    elif health_manager is not None and health_probe is not None:
        # A caller may replace only the remote probe while retaining the
        # manager's persisted state and identity policy.
        health_manager.probe = health_probe

    if not transport_was_injected:
        context_store = context_store or ReplyContextStore()
        receiver = receiver or Receiver(
            receiver_transport,
            health_inbound,
            metadata.get("account_fingerprint"),
            metadata.get("bot_fingerprint"),
            context_store=context_store,
        )

    preflight = None
    send_observer = None
    if health_manager is not None:
        preflight = lambda now, _plan, _digest: health_manager.preflight(now)
        send_observer = health_manager.observe_send_result
    service = DeliveryService(
        inbox, plan_ledger, transport,
        preflight=preflight, send_observer=send_observer,
    )
    logger = build_logger(paths.logs_dir)
    lock = ProductionLock(
        paths.lock_file,
        on_stale_recovered=lambda: logger.event(
            "stale_lock_recovered", stale_lock_recovered="YES", outcome="RECOVERED"
        ),
    )
    return {
        "paths": paths,
        "send_ledger": send_ledger,
        "plan_ledger": plan_ledger,
        "service": service,
        "transport": transport,
        "health_manager": health_manager,
        "health_inbound": health_inbound,
        "receiver": receiver,
        "context_store": context_store,
        "expected_sender_fingerprint": metadata.get("account_fingerprint"),
        "expected_bot_fingerprint": metadata.get("bot_fingerprint"),
        "lock": lock,
        "logger": logger,
    }


def close_delivery_parts(parts):
    """Close only resources owned by the Delivery runtime."""
    try:
        parts["plan_ledger"].close()
    finally:
        try:
            parts["send_ledger"].close()
        finally:
            if parts.get("health_inbound") is not None:
                parts["health_inbound"].close()
            was_owned = parts["lock"].owned
            released = parts["lock"].release()
            if was_owned:
                parts["logger"].event(
                    "lock_released", lock_released="YES" if released else "NO",
                    outcome="RELEASED" if released else "NOT_RELEASED",
                )
            parts["logger"].close()


def _as_delivery_time(value):
    """Normalize an injected or real clock value to the delivery timezone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=DELIVERY_TZ)
    return value.astimezone(DELIVERY_TZ)


def _event_context_ref(storage, event):
    """Resolve exactly one receive-stored event to its context reference."""
    if storage is None:
        return None
    try:
        row = storage.inbound(event.account_scope, event.message_id)
    except Exception:
        return None
    if row is None:
        return None
    return row["context_ref"]


def _context_mode_default(transport, health_manager, require_fresh_context):
    """Enable context-bound behavior for the installed, non-injected runner."""
    if require_fresh_context is not None:
        return bool(require_fresh_context)
    # Existing unit tests inject a fake transport and intentionally exercise
    # the context-free DeliveryService contract.  The installed scheduler
    # path supplies neither transport nor health manager, so it is the only
    # default that opts into real Web-First inbound gating.
    return transport is None and health_manager is None


def run_delivery_production(max_hours=4, poll_seconds=60, *, paths=None,
                            now: datetime | None = None, transport=None,
                            token=None, binding_store=None,
                            health_manager=None, health_probe=None,
                            authorized_retry_plan_id=None,
                            authorized_retry_hash=None,
                            authorized_retry_target_date=None,
                            clock=None, sleep=None,
                            receiver=None, context_store=None,
                            require_fresh_context=None):
    """Run a bounded Delivery Inbox polling window.

    ``max_hours`` and ``poll_seconds`` remain accepted for compatibility with
    the installed task action.  The target date is pinned once at startup so a
    runner crossing midnight cannot silently switch to the following day.
    ``clock`` and ``sleep`` are injectable only for deterministic tests.
    """
    if isinstance(max_hours, bool) or not isinstance(max_hours, int) or not 1 <= max_hours <= 6:
        return "FAILED_INVALID_RUNTIME_LIMIT"
    if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or poll_seconds <= 0:
        return "FAILED_INVALID_POLL_INTERVAL"
    clock_fn = clock or (lambda: datetime.now(DELIVERY_TZ))
    sleep_fn = sleep or time.sleep
    context_mode = _context_mode_default(
        transport, health_manager, require_fresh_context,
    )
    parts = build_delivery_parts(
        paths, transport=transport, token=token, binding_store=binding_store,
        health_manager=health_manager, health_probe=health_probe,
        receiver=receiver, context_store=context_store,
    )
    lock = parts["lock"]
    logger = parts["logger"]
    started = _as_delivery_time(now if now is not None else clock_fn())
    deadline = started + timedelta(hours=max_hours)
    pinned_target_date = (started.date() + timedelta(days=1)).isoformat()
    request_target_date = authorized_retry_target_date or pinned_target_date
    current = started
    context_gate = None
    try:
        lock.acquire()
        logger.event(
            "runner_started", pid=os.getpid(), run_id=lock.run_id,
            pinned_target_date=pinned_target_date,
        )
        if context_mode:
            inbound_receiver = parts.get("receiver")
            context_store = parts.get("context_store")
            inbound_storage = parts.get("health_inbound")
            if inbound_storage is None and inbound_receiver is not None:
                inbound_storage = getattr(inbound_receiver, "storage", None)
            if context_store is None and inbound_receiver is not None:
                context_store = getattr(inbound_receiver, "context_store", None)
            if inbound_receiver is None or context_store is None:
                logger.event(
                    "delivery_exit", state="FAILED",
                    outcome="INBOUND_CONTEXT_RUNTIME_UNAVAILABLE",
                )
                return "FAILED_INBOUND_CONTEXT_RUNTIME"
            expected_sender = parts.get("expected_sender_fingerprint")
            expected_bot = parts.get("expected_bot_fingerprint")
            whitelist = getattr(inbound_receiver, "whitelist", None)
            expected_sender = expected_sender or getattr(whitelist, "sender_id", None)
            expected_bot = expected_bot or getattr(whitelist, "bot_id", None)
            context_gate = ProductionInboundContextGate(
                runner_started_at=started,
                expected_sender_fingerprint=expected_sender,
                expected_bot_fingerprint=expected_bot,
                context_store=context_store,
                context_ref_for_event=lambda event: _event_context_ref(
                    inbound_storage, event,
                ),
                existing_context_refs=snapshot_context_refs(context_store),
                now=clock_fn,
            )
            # Perform the receive-only health gate before waiting.  A blocked
            # identity/outbound latch remains a terminal fail-closed result;
            # a transient degraded result can recover on a later poll.
            manager = parts.get("health_manager")
            if manager is not None:
                initial_health = manager.preflight(started)
                if not initial_health.allowed:
                    if initial_health.outcome == "BLOCKED_REAUTH_REQUIRED":
                        return "WECHAT_REAUTH_REQUIRED"
                    if initial_health.outcome == "BLOCKED_OUTBOUND_PREPARE_FAILED":
                        return "OUTBOUND_PREPARE_FAILED"
                    if initial_health.outcome == "BLOCKED_SESSION_UNKNOWN":
                        return "WECHAT_SESSION_UNHEALTHY"
        poll_count = 0
        last_result_status = None
        while True:
            if parts["paths"].disabled_sentinel.exists():
                logger.event("delivery_exit", state="DISABLED", outcome="DISABLED")
                return "DISABLED"
            if parts["paths"].stop_sentinel.exists():
                logger.event("delivery_exit", state="STOPPED", outcome="STOPPED")
                return "STOPPED"
            if current > deadline:
                if context_mode and last_result_status == "WAITING_FOR_FRESH_CONTEXT":
                    return "WAITING_CONTEXT_TIMEOUT"
                return "NO_CONFIRMED_PLAN"
            if context_gate is not None:
                context_gate.expire_if_needed(now=current)
                inbound_receiver = parts.get("receiver")
                try:
                    inbound_events = inbound_receiver.receive_once()
                except (TimeoutError, ConnectionError, OSError, ValueError) as error:
                    inbound_events = ()
                    logger.event(
                        "inbound_poll", outcome="ERROR",
                        exception_type=type(error).__name__,
                    )
                accepted = context_gate.accept(inbound_events, now=current)
                logger.event(
                    "inbound_poll",
                    outcome="ACCEPTED" if accepted else "WAITING",
                    context_state=("BOUND" if context_gate.binding else "NONE"),
                    context_age=(
                        max(0.0, (current - context_gate.binding.created_at).total_seconds())
                        if context_gate.binding else None
                    ),
                )
            transport_context = (
                context_gate.transport_context() if context_gate is not None else None
            )
            context_token = (
                transport_context.get("context_token")
                if transport_context is not None else None
            )
            result = parts["service"].deliver_tomorrow(
                current,
                target_date=request_target_date,
                authorized_retry_plan_id=authorized_retry_plan_id,
                authorized_retry_hash=authorized_retry_hash,
                authorized_retry_target_date=authorized_retry_target_date,
                require_context=context_mode,
                context_token=context_token,
            )
            last_result_status = result.status
            # Record only bounded scan facts; never include plan text, request
            # bodies, recipients, credentials, or DriveFS file contents.
            logger.event("delivery_scan", **parts["service"].last_scan_diagnostics)
            manager = parts.get("health_manager")
            if manager is not None and manager.last_observation is not None:
                observation = manager.last_observation
                logger.event(
                    "session_health",
                    session_health=observation.state,
                    health_checked_at=observation.checked_at,
                    health_age=observation.age_seconds,
                    bot_fingerprint=observation.bot_fingerprint,
                    account_fingerprint=observation.account_fingerprint,
                    credential_fingerprint=observation.credential_fingerprint,
                    preflight_result=("PASS" if observation.state == "HEALTHY" else "BLOCKED"),
                    error_class=observation.error_class or "NONE",
                    transition=manager.last_transition or "NONE",
                    http_status=observation.http_status,
                    ret=observation.ret,
                    errcode=observation.errcode,
                    errmsg=observation.errmsg,
                    response_keys=observation.response_keys,
                )
            key = f"daily-plan-delivery:{result.plan_id}" if result.plan_id else "NONE"
            poll_count += 1
            submitted = result.status in {"DELIVERED", "FAILED", "UNKNOWN"}
            logger.event("delivery_event", state=result.status, idempotency_key=key,
                         outcome=result.status, poll_count=poll_count,
                         delivery_discovered="YES" if result.plan_id else "NO",
                         submitted="YES" if submitted else "NO")
            if result.status == "BLOCKED_REAUTH_REQUIRED":
                logger.event(
                    "delivery_exit", state="REAUTH_REQUIRED",
                    outcome="WECHAT_REAUTH_REQUIRED",
                )
                return "WECHAT_REAUTH_REQUIRED"
            if result.status == "BLOCKED_OUTBOUND_PREPARE_FAILED":
                # An outbound preparation rejection is deliberately distinct
                # from credential/session expiry.  Keep the runner fail-closed
                # and surface an operator-diagnosable outcome without
                # suggesting QR re-authentication or retrying automatically.
                logger.event(
                    "delivery_exit", state="OUTBOUND_BLOCKED",
                    outcome="OUTBOUND_PREPARE_FAILED",
                )
                return "OUTBOUND_PREPARE_FAILED"
            if result.status == "BLOCKED_SESSION_UNKNOWN":
                logger.event(
                    "delivery_exit", state="UNKNOWN",
                    outcome="WECHAT_SESSION_UNHEALTHY",
                )
                return "WECHAT_SESSION_UNHEALTHY"
            if result.status == "BLOCKED_SESSION_DEGRADED":
                # A transient receive/network fault is retried only inside the
                # existing bounded window; no Delivery ledger row exists yet.
                remaining = (deadline - current).total_seconds()
                if remaining <= 0:
                    return "NO_CONFIRMED_PLAN"
                delay = min(float(poll_seconds), remaining)
                sleep_fn(delay)
                observed = _as_delivery_time(clock_fn())
                current = observed if observed > current else current + timedelta(seconds=delay)
                continue
            if (
                context_gate is not None
                and context_gate.binding is not None
                and result.status not in {
                    "NO_CONFIRMED_PLAN", "WAITING_FOR_FRESH_CONTEXT",
                }
            ):
                # Once a candidate reaches a terminal outcome (including a
                # closed health/idempotency result), consume the exact inbound
                # context so it cannot authorize another automatic attempt.
                context_gate.consume()
            if result.status not in {
                "NO_CONFIRMED_PLAN", "WAITING_FOR_FRESH_CONTEXT",
            }:
                return result.status

            remaining = (deadline - current).total_seconds()
            if remaining <= 0:
                if context_mode and result.status == "WAITING_FOR_FRESH_CONTEXT":
                    return "WAITING_CONTEXT_TIMEOUT"
                return "NO_CONFIRMED_PLAN"
            delay = min(float(poll_seconds), remaining)
            sleep_fn(delay)
            observed = _as_delivery_time(clock_fn())
            # A fake clock is allowed to be observational only; still make
            # deterministic tests progress rather than spin forever.
            current = observed if observed > current else current + timedelta(seconds=delay)
    except Exception as error:
        logger.event(
            "runner_exception", outcome="FAILED",
            exception_type=type(error).__name__, exception_message=str(error),
        )
        raise
    finally:
        logger.event(
            "runner_exit", outcome="completed",
            runtime_minutes=int((datetime.now(DELIVERY_TZ) - started).total_seconds() / 60),
        )
        close_delivery_parts(parts)
