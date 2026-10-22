"""Offline acceptance harness for the pre-live Chat Agent pilot."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from ilink_receive_probe.probe.models import Batch, InboundEvent

from .adapter import ChatAgentReceiverAdapter
from .contacts import ContactPairingStore
from .drafts import DraftExpiredError, DraftStore, PENDING
from .history import ChatHistoryStore
from .inbox import ChatInboxStore
from .model import ModelResult
from .pipeline import ChatDraftPipeline
from .router import ChatAgentRouter, DUPLICATE_IGNORED, IGNORED_CONTACT
from .send_coordinator import ManualSendCoordinator
from .receiver import ChatAgentReceiver
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.send_result import ClassifiedSendResult, API_ACCEPTED_WITH_RECEIPT


UTC = timezone.utc


@dataclass(frozen=True)
class OfflineAcceptanceReport:
    passed: bool
    checks: dict
    metrics: dict


class _Provider:
    def __init__(self):
        self.calls = []
        self.latencies = []

    def generate(self, messages, *, timeout_seconds):
        self.calls.append((messages, timeout_seconds))
        self.latencies.append(1)
        return ModelResult("离线验收草稿", provider="offline", latency_ms=1)


class _NoSendTransport:
    def __init__(self):
        self.calls = []

    def send(self, context, idempotency_key, text):
        self.calls.append((context, idempotency_key, text))
        return ClassifiedSendResult(API_ACCEPTED_WITH_RECEIPT, True, False, receipt_id="offline")


class _Receiver:
    def __init__(self, events, *, failure_count=0):
        self.events = list(events)
        self.failure_count = failure_count
        self.calls = 0
        self.state = "IDLE"
        self.last_events = ()

    def receive_once(self):
        self.calls += 1
        if self.calls <= self.failure_count:
            self.state = "WECHAT_UNAVAILABLE"
            self.last_events = ()
            return []
        self.state = "IDLE"
        self.last_events = tuple(self.events)
        events, self.events = self.events, []
        return events


def _event(message_id, sender="sender", text=None):
    return InboundEvent(
        "acceptance-account",
        message_id,
        sender,
        message_id,
        text or f"测试消息 {message_id}",
        "bot",
        "one_to_one",
        f"raw-context-{message_id}",
        f"raw-recipient-{message_id}",
    )


def _pair(router, event, now):
    router.pair_candidate(event, observed_at=now)
    router.contacts.confirm_candidate(
        event.account_scope,
        event.sender_fingerprint,
        event.bot_id,
        alias="验收联系人",
        now=now,
    )


def _batch_probe(batch_count, now):
    contacts = ContactPairingStore()
    inbox = ChatInboxStore()
    router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
    first = _event("m-0")
    _pair(router, first, now)
    drafts = DraftStore()
    history = ChatHistoryStore()
    provider = _Provider()
    pipeline = ChatDraftPipeline(router, provider, drafts, history=history)
    for index in range(batch_count):
        event = first if index == 0 else _event(f"m-{index}")
        pipeline.ingest(
            event,
            context_ref=f"ctx-{index}",
            now=now + timedelta(seconds=index * 9),
        )
    pipeline.flush(now=now + timedelta(seconds=batch_count * 9 + 8))
    calls_before_unknown = len(provider.calls)
    unknown = pipeline.ingest(_event("unknown", sender="not-paired"), context_ref="ctx-unknown", now=now)
    calls_after_unknown = len(provider.calls)
    calls_before_duplicate = calls_after_unknown
    duplicate = pipeline.ingest(
        _event(f"m-{batch_count - 1}"),
        context_ref=f"ctx-{batch_count - 1}",
        now=now + timedelta(seconds=batch_count * 9 + 9),
    )
    draft_count = drafts.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
    result = {
        "model_calls": len(provider.calls),
        "model_calls_before_unknown": calls_before_unknown,
        "model_calls_after_unknown": calls_after_unknown,
        "model_calls_after_duplicate": len(provider.calls),
        "model_latency_p95_ms": _p95(provider.latencies),
        "send_attempts": 0,
        "draft_rows": draft_count,
        "unknown_route_ignored": unknown.route == IGNORED_CONTACT,
        "unknown_contact_no_model": calls_after_unknown == calls_before_unknown,
        "duplicate_route_ignored": duplicate.route == DUPLICATE_IGNORED,
        "duplicate_no_model": len(provider.calls) == calls_before_duplicate,
        "history_rows": history.db.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0],
    }
    pipeline.close()
    history.close()
    drafts.close()
    router.close()
    return result


def _p95(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    return values[min(len(values) - 1, int((len(values) - 1) * 0.95))]


def _expiry_send_probe(now):
    """Verify an approved-but-expired draft cannot reach the transport."""
    contacts = ContactPairingStore()
    inbox = ChatInboxStore()
    router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
    event = _event("expiry-1")
    _pair(router, event, now)
    drafts = DraftStore()
    draft = drafts.create_draft(
        event.account_scope,
        "\x1f".join((event.account_scope, event.sender_fingerprint, event.bot_id)),
        "expiry-batch",
        [event.message_id],
        event.message_id,
        "expiry-context",
        "过期前草稿",
        created_at=now,
        expires_in=timedelta(seconds=1),
    )
    drafts.approve(draft["draft_id"], now=now)
    transport = _NoSendTransport()
    coordinator = ManualSendCoordinator(
        drafts,
        transport,
        contacts,
        lambda _ref: {
            "account_scope": event.account_scope,
            "sender_fingerprint": event.sender_fingerprint,
            "bot_fingerprint": event.bot_id,
            "recipient": "offline-recipient",
            "context_token": "offline-context",
            "conversation_type": "one_to_one",
            "is_forwarded": False,
        },
    )
    blocked = False
    try:
        coordinator.send_approved(draft["draft_id"], confirmed=True, now=now + timedelta(seconds=2))
    except DraftExpiredError:
        blocked = True
    result = {"blocked": blocked, "send_attempts": len(transport.calls)}
    drafts.close()
    router.close()
    return result


def _receiver_handoff_probe():
    """Verify a deferred receiver replays a batch until adapter acknowledgement."""
    with TemporaryDirectory() as directory:
        path = directory + "/receiver.sqlite"
        event = _event("handoff-1")
        batch = Batch((event,), "handoff-cursor")

        class _Transport:
            def getupdates(self, _cursor):
                return batch

        first_storage = ProbeStorage(path)
        first = ChatAgentReceiver(
            _Transport(), first_storage,
            account_scope="acceptance-account", defer_cursor=True,
        )
        first.receive_once()
        cursor_before_ack = first_storage.cursor()
        first_storage.close()

        second_storage = ProbeStorage(path)
        second = ChatAgentReceiver(
            _Transport(), second_storage,
            account_scope="acceptance-account", defer_cursor=True,
        )
        replay = second.receive_once()
        second.acknowledge(replay)
        result = {
            "replayed": len(replay) == 1 and replay[0].message_id == "handoff-1",
            "cursor_before_ack": cursor_before_ack,
            "cursor_after_ack": second_storage.cursor(),
            "processed_status": second_storage.event_status("acceptance-account", "handoff-1"),
        }
        second_storage.close()
        return result


def _restart_probe(restart_count, now):
    provider = _Provider()
    with TemporaryDirectory() as directory:
        contacts_path = directory + "/contacts.sqlite"
        inbox_path = directory + "/inbox.sqlite"
        drafts_path = directory + "/drafts.sqlite"
        batches_path = directory + "/batches.sqlite"
        event = _event("restart-1")
        contacts = ContactPairingStore(contacts_path)
        inbox = ChatInboxStore(inbox_path)
        router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
        _pair(router, event, now)
        drafts = DraftStore(drafts_path)
        pipeline = ChatDraftPipeline(router, provider, drafts, path=batches_path)
        pipeline.ingest(event, context_ref="restart-context", now=now)
        pipeline.close()
        drafts.close()
        router.close()
        for _ in range(restart_count):
            contacts = ContactPairingStore(contacts_path)
            inbox = ChatInboxStore(inbox_path)
            router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
            drafts = DraftStore(drafts_path)
            pipeline = ChatDraftPipeline(router, provider, drafts, path=batches_path)
            if _ == restart_count - 1:
                results = pipeline.flush(now=now + timedelta(seconds=8))
            else:
                results = []
            pipeline.close()
            drafts.close()
            router.close()
    return {
        "restart_count": restart_count,
        "recovered_drafts": len(results),
        "model_calls": len(provider.calls),
        "recovery_status_pending": bool(results and results[0].status == PENDING),
    }


def _network_probe(failure_count, now):
    contacts = ContactPairingStore()
    inbox = ChatInboxStore()
    router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=True)
    event = _event("missed-1")
    _pair(router, event, now)
    provider = _Provider()
    drafts = DraftStore()
    pipeline = ChatDraftPipeline(router, provider, drafts)
    receiver = _Receiver([event, _event("missed-2")], failure_count=failure_count)
    adapter = ChatAgentReceiverAdapter(receiver, pipeline)
    first_result = None
    for _ in range(failure_count):
        first_result = adapter.process_once(now=now)
    recovery_result = adapter.process_once(now=now + timedelta(seconds=1))
    result = {
        "failure_count": failure_count,
        "recovered": recovery_result.recovered,
        "missed_count": recovery_result.missed_count,
        "model_calls": len(provider.calls),
        "first_state_unavailable": bool(first_result and first_result.receiver_state == "WECHAT_UNAVAILABLE"),
    }
    pipeline.close()
    drafts.close()
    router.close()
    return result


def run_offline_acceptance(*, batch_count=100, restart_count=5, failure_count=3):
    """Run the fixed-contact pre-live acceptance matrix without real transport."""
    if batch_count <= 0 or restart_count <= 0 or failure_count <= 0:
        raise ValueError("acceptance counts must be positive")
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    batch = _batch_probe(batch_count, now)
    restarts = _restart_probe(restart_count, now)
    network = _network_probe(failure_count, now)
    expiry = _expiry_send_probe(now)
    handoff = _receiver_handoff_probe()
    checks = {
        "batch_count": batch["model_calls"] == batch_count,
        "history_bounded": batch["history_rows"] <= 200,
        "unknown_contact_no_model": (
            batch["unknown_route_ignored"] and batch["unknown_contact_no_model"]
        ),
        "unknown_contact_model_calls_unchanged": batch["unknown_contact_no_model"],
        "duplicate_event_no_model": (
            batch["duplicate_route_ignored"] and batch["duplicate_no_model"]
        ),
        "duplicate_model_calls_unchanged": batch["duplicate_no_model"],
        "p95_latency_under_20s": (
            batch["model_latency_p95_ms"] is not None
            and batch["model_latency_p95_ms"] <= 20_000
        ),
        "restart_recovered_once": (
            restarts["recovered_drafts"] == 1
            and restarts["model_calls"] == 1
            and restarts["recovery_status_pending"]
        ),
        "network_recovery_skips_old_batch": (
            network["recovered"]
            and network["missed_count"] == 2
            and network["model_calls"] == 0
            and network["first_state_unavailable"]
        ),
        "expired_send_blocked": expiry["blocked"],
        "no_send_attempts": batch["send_attempts"] + expiry["send_attempts"] == 0,
        "receiver_handoff_replayed": (
            handoff["replayed"]
            and handoff["cursor_before_ack"] == ""
            and handoff["cursor_after_ack"] == "handoff-cursor"
            and handoff["processed_status"] == "chat_agent_processed"
        ),
    }
    checks["no_send_path"] = checks["no_send_attempts"]
    manual_gates = (
        "real_local_model_benchmark",
        "human_edit_quality_80_percent",
        "real_account_receive_pilot",
        "real_receipt_aware_send_pilot",
    )
    return OfflineAcceptanceReport(
        passed=all(checks.values()),
        checks=checks,
        metrics={
            "batch": batch,
            "restarts": restarts,
            "network": network,
            "expiry": expiry,
            "receiver_handoff": handoff,
            "manual_gates": manual_gates,
        },
    )
