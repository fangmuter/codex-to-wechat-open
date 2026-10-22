from dataclasses import dataclass
import os
from pathlib import Path

from ilink_receive_probe.probe.credential_store import CredentialStore
from ilink_receive_probe.probe.recipient_binding import RecipientBindingStore

@dataclass(frozen=True)
class ProductionPaths:
    root: Path
    review_inbox_dir: Path | None = None
    delivery_inbox_dir: Path | None = None

    def __post_init__(self):
        # Explicit constructor values win.  For isolated tests/development,
        # omitted inboxes live under the supplied state root.  The installed
        # current-user runtime below requires explicit inbox environment
        # variables instead of assuming a machine-specific DriveFS mount.
        root = Path(self.root)
        if self.review_inbox_dir is None:
            object.__setattr__(
                self, "review_inbox_dir",
                Path(os.environ.get("DAILY_PLAN_REVIEW_INBOX", root / "review-inbox")),
            )
        if self.delivery_inbox_dir is None:
            object.__setattr__(
                self, "delivery_inbox_dir",
                Path(os.environ.get("DAILY_PLAN_DELIVERY_INBOX", root / "delivery-inbox")),
            )

    @property
    def scheduler_db(self): return self.root/'scheduler.db'
    @property
    def delivery_ledger_db(self): return self.root/'delivery-ledger.db'
    @property
    def plan_delivery_ledger_db(self): return self.root/'plan-delivery-ledger.db'
    @property
    def delivery_send_ledger_db(self): return self.root/'delivery-send-ledger.db'
    @property
    def inbound_db(self): return self.root/'inbound.db'
    @property
    def planner_db(self): return self.root/'planner.db'
    @property
    def exchange_db(self): return self.root/'exchange.db'
    @property
    def metadata_db(self): return self.root/'phase19-metadata.db'
    @property
    def roadmap_db(self): return self.root/'roadmap-progress.db'
    @property
    def session_health_file(self): return self.root/'session-health.json'
    @property
    def logs_dir(self): return self.root/'logs'
    @property
    def disabled_sentinel(self): return self.root/'disabled'
    @property
    def stop_sentinel(self): return self.root/'stop.requested'
    @property
    def lock_file(self): return self.root/'runner.lock'

    @classmethod
    def current_user(cls):
        review_inbox = os.environ.get("DAILY_PLAN_REVIEW_INBOX")
        delivery_inbox = os.environ.get("DAILY_PLAN_DELIVERY_INBOX")
        if not review_inbox or not delivery_inbox:
            raise RuntimeError(
                "Set DAILY_PLAN_REVIEW_INBOX and DAILY_PLAN_DELIVERY_INBOX "
                "to explicit readable directories before constructing the live runtime"
            )
        state_root = os.environ.get("DAILY_PLAN_STATE_DIR")
        root = (
            Path(state_root)
            if state_root
            else CredentialStore().state_dir / "production" / "daily-plan"
        )
        return cls(root, Path(review_inbox), Path(delivery_inbox))

    def describe(self):
        credential=CredentialStore(); binding=RecipientBindingStore()
        return {
            'root':str(self.root), 'scheduler_db':str(self.scheduler_db),
            'delivery_ledger_db':str(self.delivery_ledger_db), 'inbound_db':str(self.inbound_db),
            'planner_db':str(self.planner_db), 'exchange_db':str(self.exchange_db),
            'metadata_db':str(self.metadata_db), 'logs_dir':str(self.logs_dir),
            'review_inbox_dir':str(self.review_inbox_dir),
            'delivery_inbox_dir':str(self.delivery_inbox_dir),
            'plan_delivery_ledger_db':str(self.plan_delivery_ledger_db),
            'delivery_send_ledger_db':str(self.delivery_send_ledger_db),
            'recipient_binding':str(binding.path), 'credential':str(credential.path),
        }
