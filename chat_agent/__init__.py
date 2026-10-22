"""Independent Chat Agent building blocks.

This package is intentionally not wired into Daily Plan. It contains the
durable contact, draft, model-provider, and send-result boundaries needed for
the REVIEW-first pilot.
"""

from .contacts import ContactPairingStore, ContactRecord, PairingCandidate
from .drafts import DraftStore
from .inbox import ChatInboxStore, ClaimResult, stable_event_key
from .model import ModelProvider, ModelResult
from .ollama_provider import OllamaProvider
from .pipeline import ChatDraftPipeline, DraftPipeline, PipelineResult
from .adapter import ChatAgentPollResult, ChatAgentReceiverAdapter
from .review_server import LocalReviewServer
from .history import (
    ChatHistoryStore,
    HistoryMessage,
    SensitiveManualEditRequired,
    build_prompt_messages,
)
from .runtime import (
    ChatAgentRuntime,
    RuntimeControlStore,
    RuntimeTick,
    RUNTIME_DISABLED,
    RUNTIME_POLLED,
    RUNTIME_STOPPED,
)
from .benchmark import BenchmarkCase, BenchmarkReport, BenchmarkResult, run_benchmark
from .notifications import (
    CallbackNotificationSink,
    NotificationEvent,
    NullNotificationSink,
    RecoveryNotification,
    WindowsMessageNotificationSink,
)
from .acceptance import OfflineAcceptanceReport, run_offline_acceptance
from .send_coordinator import (
    ContextUnavailableError,
    ManualSendCoordinator,
    SendConfirmationRequired,
    SendNotAllowedError,
    SendOutcome,
)
from .receiver import CHAT_AGENT_PROCESSED, WECHAT_UNAVAILABLE, ChatAgentReceiver
from .router import ChatAgentRouter, RouterDecision
from .transport import ChatReplyTransport
from .risk import HIGH, LOW, MEDIUM, RiskAssessment, assess_text, combine_assessments
from .factory import ManualPilot, build_manual_pilot
from .preflight import (
    OLLAMA_INVALID_REQUEST,
    OLLAMA_INVALID_RESPONSE,
    OLLAMA_MODEL_NOT_INSTALLED,
    OLLAMA_READY,
    OLLAMA_SERVICE_UNAVAILABLE,
    OllamaPreflightReport,
    check_ollama,
)
from .audit import (
    AuditEvent,
    ChatAuditStore,
    DRAFT_EVENT,
    EVENT_TYPES,
    RECOVERY_EVENT,
    ROUTE_EVENT,
    SEND_EVENT,
)

__all__ = [
    "ChatAgentRouter",
    "ChatInboxStore",
    "ChatReplyTransport",
    "ClaimResult",
    "ContactPairingStore",
    "ContactRecord",
    "PairingCandidate",
    "DraftStore",
    "ModelProvider",
    "ModelResult",
    "OllamaProvider",
    "ChatDraftPipeline",
    "DraftPipeline",
    "PipelineResult",
    "ChatAgentPollResult",
    "ChatAgentReceiverAdapter",
    "LocalReviewServer",
    "ChatHistoryStore",
    "HistoryMessage",
    "SensitiveManualEditRequired",
    "build_prompt_messages",
    "ChatAgentRuntime",
    "RuntimeControlStore",
    "RuntimeTick",
    "RUNTIME_DISABLED",
    "RUNTIME_POLLED",
    "RUNTIME_STOPPED",
    "BenchmarkCase",
    "BenchmarkReport",
    "BenchmarkResult",
    "run_benchmark",
    "CallbackNotificationSink",
    "NotificationEvent",
    "NullNotificationSink",
    "RecoveryNotification",
    "WindowsMessageNotificationSink",
    "OfflineAcceptanceReport",
    "run_offline_acceptance",
    "ContextUnavailableError",
    "ManualSendCoordinator",
    "SendConfirmationRequired",
    "SendNotAllowedError",
    "SendOutcome",
    "ChatAgentReceiver",
    "CHAT_AGENT_PROCESSED",
    "WECHAT_UNAVAILABLE",
    "RouterDecision",
    "stable_event_key",
    "RiskAssessment",
    "LOW",
    "MEDIUM",
    "HIGH",
    "assess_text",
    "combine_assessments",
    "ManualPilot",
    "build_manual_pilot",
    "OllamaPreflightReport",
    "check_ollama",
    "OLLAMA_READY",
    "OLLAMA_SERVICE_UNAVAILABLE",
    "OLLAMA_MODEL_NOT_INSTALLED",
    "OLLAMA_INVALID_RESPONSE",
    "OLLAMA_INVALID_REQUEST",
    "AuditEvent",
    "ChatAuditStore",
    "ROUTE_EVENT",
    "DRAFT_EVENT",
    "SEND_EVENT",
    "RECOVERY_EVENT",
    "EVENT_TYPES",
]
