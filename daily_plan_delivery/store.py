"""Small independent, redacted Delivery Inbox ledger."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class DeliveryLedgerConflict(ValueError):
    pass


@dataclass(frozen=True)
class SupervisedProbeEvidence:
    """Bounded evidence for a completed, operator-authorized probe.

    The evidence object intentionally carries no recipient, credential,
    context token, request body, or raw message id.  Exact plan/hash binding,
    explicit authorization, and an ACK with a message-id presence marker are
    sufficient for the reconciliation gate; the attempt history remains the
    source of truth for every network attempt.
    """

    plan_id: str
    payload_hash: str
    authorized: bool
    acked: bool
    message_id_present: bool
    source: str = "controlled_v1_probe"


@dataclass(frozen=True)
class ReconciliationResult:
    """Safe result for a no-network supervised-ACK reconciliation."""

    status: str  # RECONCILED | NOOP_ALREADY_DELIVERED
    plan_id: str
    payload_hash: str
    delivered_at: str | None = None


class DeliveryLedger:
    def __init__(self, path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS delivery_plans(
            plan_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            delivered_at TEXT,
            delivery_result TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            last_reason TEXT
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS delivery_rejections(
            source_filename TEXT PRIMARY KEY,
            rejected_at TEXT NOT NULL,
            reason TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS delivery_plan_attempt_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            payload_hash TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            state TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            delivered_at TEXT,
            reason TEXT,
            UNIQUE(plan_id, attempt_no))""")
        self.db.commit()
        self._active_attempts = {}

    def close(self):
        self.db.close()

    @staticmethod
    def _now(now=None):
        if isinstance(now, str):
            return now
        return (now or datetime.now(timezone.utc)).isoformat()

    def get(self, plan_id):
        return self.db.execute(
            "SELECT * FROM delivery_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()

    def _insert_attempt(self, plan_id, attempt_no, payload_hash, source_filename,
                        state, now, *, reason=None, delivered_at=None):
        self.db.execute(
            """INSERT INTO delivery_plan_attempt_history(
                plan_id,attempt_no,payload_hash,source_filename,state,started_at,
                updated_at,delivered_at,reason)
                VALUES(?,?,?,?,?,?,?,?,?)""",
            (plan_id, attempt_no, payload_hash, source_filename, state,
             self._now(now), self._now(now), delivered_at,
             None if reason is None else str(reason)[:160]),
        )

    def _ensure_legacy_attempt(self, existing):
        count = self.db.execute(
            "SELECT COUNT(*) FROM delivery_plan_attempt_history WHERE plan_id=?",
            (existing["plan_id"],),
        ).fetchone()[0]
        if count:
            return
        delivered_at = existing["delivered_at"]
        self._insert_attempt(
            existing["plan_id"], 1, existing["payload_hash"],
            existing["source_filename"], existing["delivery_result"],
            existing["first_seen_at"], reason=existing["last_reason"],
            delivered_at=delivered_at,
        )

    def begin(self, plan_id, payload_hash, source_filename, now, *,
              allow_failed_retry=False):
        existing = self.get(plan_id)
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise DeliveryLedgerConflict("plan_id has a different payload hash")
            if allow_failed_retry and existing["delivery_result"] == "FAILED":
                self._ensure_legacy_attempt(existing)
                latest = self.db.execute(
                    "SELECT MAX(attempt_no) FROM delivery_plan_attempt_history WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()[0] or 0
                if latest >= 2:
                    self.db.commit()
                    return False
                attempt_no = latest + 1
                self._insert_attempt(
                    plan_id, attempt_no, payload_hash, source_filename,
                    "SENDING", now,
                )
                self.db.execute(
                    """UPDATE delivery_plans SET delivery_result='SENDING',
                       delivered_at=NULL,last_reason=NULL WHERE plan_id=?""",
                    (plan_id,),
                )
                self.db.commit()
                self._active_attempts[plan_id] = attempt_no
                return True
            return False
        try:
            self.db.execute(
                "INSERT INTO delivery_plans(plan_id,payload_hash,first_seen_at,delivery_result,source_filename) VALUES(?,?,?,?,?)",
                (plan_id, payload_hash, self._now(now), "SENDING", source_filename),
            )
            self._insert_attempt(plan_id, 1, payload_hash, source_filename, "SENDING", now)
            self.db.commit()
            self._active_attempts[plan_id] = 1
            return True
        except sqlite3.IntegrityError:
            # Another process won the idempotency race.  Re-read the row and
            # apply the same hash contract instead of attempting a second send.
            self.db.rollback()
            existing = self.get(plan_id)
            if existing and existing["payload_hash"] == payload_hash:
                return False
            raise DeliveryLedgerConflict("plan_id has a different payload hash")

    def record(self, plan_id, result, now, *, reason=None):
        delivered_at = self._now(now) if result == "DELIVERED" else None
        self.db.execute(
            "UPDATE delivery_plans SET delivery_result=?,delivered_at=?,last_reason=? WHERE plan_id=?",
            (result, delivered_at, None if reason is None else str(reason)[:160], plan_id),
        )
        attempt_no = self._active_attempts.get(plan_id)
        if attempt_no is None:
            row = self.db.execute(
                "SELECT MAX(attempt_no) FROM delivery_plan_attempt_history WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            attempt_no = row[0] or 1
        self.db.execute(
            """UPDATE delivery_plan_attempt_history
               SET state=?,updated_at=?,delivered_at=?,reason=?
               WHERE plan_id=? AND attempt_no=?""",
            (result, self._now(now), delivered_at,
             None if reason is None else str(reason)[:160], plan_id, attempt_no),
        )
        self.db.commit()
        self._active_attempts.pop(plan_id, None)

    def record_supervised_probe_success(
        self,
        plan_id,
        payload_hash,
        evidence: SupervisedProbeEvidence,
        delivered_at=None,
    ) -> ReconciliationResult:
        """Reconcile one exact supervised ACK without creating an attempt.

        This is deliberately separate from :meth:`record`: the normal send
        path records a result for an active attempt, whereas a controlled V1
        probe may have succeeded outside the formal Delivery attempt boundary.
        The method only transitions an existing, same-hash FAILED primary row
        to DELIVERED.  It never calls transport code, increments submitted or
        retry counts, inserts history, or mutates the historical FAILED rows.
        """

        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValueError("plan_id is required")
        if not isinstance(payload_hash, str) or len(payload_hash) != 64:
            raise ValueError("payload_hash must be a 64-character hex digest")
        try:
            int(payload_hash, 16)
        except (TypeError, ValueError) as error:
            raise ValueError("payload_hash must be a 64-character hex digest") from error
        if not isinstance(evidence, SupervisedProbeEvidence):
            raise ValueError("supervised probe evidence is required")
        if (
            evidence.plan_id != plan_id
            or evidence.payload_hash != payload_hash
        ):
            raise DeliveryLedgerConflict("supervised probe evidence does not match plan/hash")
        if evidence.source != "controlled_v1_probe":
            raise ValueError("unsupported supervised probe evidence source")
        if not (evidence.authorized and evidence.acked and evidence.message_id_present):
            raise ValueError("authorized ACK with message_id evidence is required")

        existing = self.get(plan_id)
        if existing is None:
            raise ValueError("cannot reconcile a plan absent from the ledger")
        if existing["payload_hash"] != payload_hash:
            raise DeliveryLedgerConflict("plan_id has a different payload hash")
        if existing["delivery_result"] == "DELIVERED":
            return ReconciliationResult(
                "NOOP_ALREADY_DELIVERED", plan_id, payload_hash,
                existing["delivered_at"],
            )
        if existing["delivery_result"] != "FAILED":
            raise ValueError("only a FAILED plan can be reconciled")

        stamp = self._now(delivered_at)
        # Update only the primary projection.  Keep all attempt-history rows
        # byte-for-byte represented as FAILED and do not create a new attempt.
        cursor = self.db.execute(
            """UPDATE delivery_plans
               SET delivery_result='DELIVERED', delivered_at=?,
                   last_reason='SUPERVISED_PROBE_RECONCILIATION'
               WHERE plan_id=? AND payload_hash=? AND delivery_result='FAILED'""",
            (stamp, plan_id, payload_hash),
        )
        if cursor.rowcount != 1:
            self.db.rollback()
            current = self.get(plan_id)
            if current and current["payload_hash"] == payload_hash \
                    and current["delivery_result"] == "DELIVERED":
                return ReconciliationResult(
                    "NOOP_ALREADY_DELIVERED", plan_id, payload_hash,
                    current["delivered_at"],
                )
            raise ValueError("supervised reconciliation lost its FAILED-row compare-and-set")
        self.db.commit()
        return ReconciliationResult("RECONCILED", plan_id, payload_hash, stamp)

    def reject(self, source_filename, reason, now):
        self.db.execute(
            "INSERT OR REPLACE INTO delivery_rejections(source_filename,rejected_at,reason) VALUES(?,?,?)",
            (source_filename, self._now(now), str(reason)[:160]),
        )
        self.db.commit()

    def rows(self):
        return self.db.execute(
            "SELECT * FROM delivery_plans ORDER BY plan_id"
        ).fetchall()

    def attempts(self, plan_id):
        return self.db.execute(
            "SELECT * FROM delivery_plan_attempt_history WHERE plan_id=? ORDER BY attempt_no",
            (plan_id,),
        ).fetchall()
