"""Web-first, confirmed-plan delivery primitives.

This package deliberately has no planner, recommendation, roadmap, reading,
or inbound-chat responsibilities.  It validates and renders an already
CONFIRMED plan, then delegates one bounded send to the existing transport.
"""

from .models import DeliveryConstraint, DeliveryItem, DeliveryPlan
from .schema import DeliveryReadError, DeliverySchemaError, validate_delivery_payload
from .inbox import DeliveryInbox, DeliveryInboxEntry
from .store import (
    DeliveryLedger,
    DeliveryLedgerConflict,
    ReconciliationResult,
    SupervisedProbeEvidence,
)
from .formatter import format_delivery_plan
from .service import DeliveryResult, DeliveryService

__all__ = [
    "DeliveryConstraint", "DeliveryItem", "DeliveryPlan",
    "DeliveryReadError", "DeliverySchemaError", "validate_delivery_payload",
    "DeliveryInbox", "DeliveryInboxEntry",
    "DeliveryLedger", "DeliveryLedgerConflict", "ReconciliationResult",
    "SupervisedProbeEvidence",
    "format_delivery_plan", "DeliveryResult", "DeliveryService",
]
