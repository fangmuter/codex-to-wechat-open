"""Safe composition for a local, manual Chat Agent pilot.

The factory constructs isolated databases and a disabled runtime.  It does
not poll, open a listener, call a model, or connect a send transport; callers
must explicitly enable the returned runtime and separately inject any future
receipt-aware sender.
"""

from dataclasses import dataclass
from pathlib import Path

from ilink_receive_probe.probe.storage import ProbeStorage

from .adapter import ChatAgentReceiverAdapter
from .audit import ChatAuditStore
from .contacts import ContactPairingStore
from .drafts import DraftStore
from .history import ChatHistoryStore
from .inbox import ChatInboxStore
from .pipeline import ChatDraftPipeline
from .review_server import LocalReviewServer
from .router import ChatAgentRouter
from .runtime import ChatAgentRuntime, RuntimeControlStore
from .receiver import ChatAgentReceiver


@dataclass
class ManualPilot:
    """All isolated resources for a manual pilot, with explicit shutdown."""

    receiver: ChatAgentReceiver
    adapter: ChatAgentReceiverAdapter
    pipeline: ChatDraftPipeline
    router: ChatAgentRouter
    contacts: ContactPairingStore
    inbox: ChatInboxStore
    drafts: DraftStore
    history: ChatHistoryStore
    receiver_storage: ProbeStorage
    runtime: ChatAgentRuntime
    runtime_control: RuntimeControlStore
    review_server: LocalReviewServer
    audit: ChatAuditStore
    _closed: bool = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        # Stop request handling before closing shared stores.  The review page
        # is never started by the factory, but stop is idempotent for callers
        # that chose to start it.
        self.review_server.stop()
        self.runtime.request_stop()
        self.runtime.close()
        self.runtime_control.close()
        self.pipeline.close()
        self.drafts.close()
        self.history.close()
        self.router.close()
        self.receiver_storage.close()
        self.audit.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def build_manual_pilot(
    transport,
    provider,
    state_dir,
    *,
    account_scope="chat-agent",
    context_store=None,
    notification_sink=None,
    poll_interval_seconds=2,
):
    """Build an isolated, disabled REVIEW pilot without performing I/O.

    ``transport`` and ``provider`` are already-constructed boundaries.  A
    caller may use a real iLink/Ollama implementation later, but construction
    itself only creates local SQLite schemas and returns a stopped review
    server.
    """
    if not isinstance(account_scope, str) or not account_scope.strip():
        raise ValueError("account_scope is required")
    if transport is None or not callable(getattr(transport, "getupdates", None)):
        raise ValueError("a receive transport with getupdates is required")
    if provider is None or not callable(getattr(provider, "generate", None)):
        raise ValueError("a model provider with generate is required")
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    contacts = ContactPairingStore(root / "contacts.sqlite")
    inbox = ChatInboxStore(root / "inbox.sqlite")
    drafts = DraftStore(root / "drafts.sqlite")
    history = ChatHistoryStore(root / "history.sqlite")
    receiver_storage = ProbeStorage(root / "receiver.sqlite")
    control = RuntimeControlStore(root / "runtime.sqlite")
    audit = ChatAuditStore(root / "audit.sqlite")
    router = ChatAgentRouter(contacts=contacts, inbox=inbox, enabled=False)
    receiver = ChatAgentReceiver(
        transport,
        receiver_storage,
        context_store=context_store,
        account_scope=account_scope,
        defer_cursor=True,
    )
    pipeline = ChatDraftPipeline(
        router,
        provider,
        drafts,
        path=root / "batches.sqlite",
        history=history,
        audit=audit,
    )
    adapter = ChatAgentReceiverAdapter(
        receiver,
        pipeline,
        notification_sink=notification_sink,
    )
    runtime = ChatAgentRuntime(
        adapter,
        control=control,
        poll_interval_seconds=poll_interval_seconds,
        route_controller=router,
    )
    review_server = LocalReviewServer(drafts, contact_store=contacts)
    return ManualPilot(
        receiver=receiver,
        adapter=adapter,
        pipeline=pipeline,
        router=router,
        contacts=contacts,
        inbox=inbox,
        drafts=drafts,
        history=history,
        receiver_storage=receiver_storage,
        runtime=runtime,
        runtime_control=control,
        review_server=review_server,
        audit=audit,
    )
