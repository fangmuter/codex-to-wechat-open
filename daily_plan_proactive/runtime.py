"""Explicit construction of the Phase 1.6C real scheduler; it never schedules itself."""
from pathlib import Path

from ilink_receive_probe.probe.credential_store import CredentialStore
from ilink_receive_probe.probe.proactive_send import require_expected_recipient_fingerprint
from ilink_receive_probe.probe.recipient_binding import RecipientBindingStore

from .real_transport import BoundIlinkProactiveTransport, DeliveryLedger
from .scheduler import ProactiveScheduler


def build_real_scheduler(path, state_dir=None, proactive_due=None, reminder_enabled=True, key_namespace="phase16", ledger_path=None):
    """Build a manually ticked scheduler; no task, loop, listener, or send occurs here."""
    database = Path(path)
    credential = CredentialStore(state_dir)
    ledger = DeliveryLedger(Path(ledger_path) if ledger_path else database)
    transport = BoundIlinkProactiveTransport(
        credential.load(), RecipientBindingStore(state_dir), ledger,
        require_expected_recipient_fingerprint(),
    )
    scheduler = ProactiveScheduler(database, transport, proactive_due, reminder_enabled, key_namespace)
    return scheduler, transport, ledger
