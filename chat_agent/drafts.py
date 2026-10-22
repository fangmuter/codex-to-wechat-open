"""Durable REVIEW-first draft state machine."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
import uuid

from ilink_receive_probe.probe.send_result import (
    API_ACCEPTED_WITH_RECEIPT,
    AMBIGUOUS,
    DEFINITE_FAILED,
    DELIVERY_UNCONFIRMED,
    INVALID_RESPONSE,
)


PENDING = "PENDING"
NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
SUPERSEDED = "SUPERSEDED"
EXPIRED = "EXPIRED"
APPROVED = "APPROVED"
SENDING = "SENDING"
SENT_CONFIRMED = "SENT_CONFIRMED"
DELIVERY_UNCONFIRMED_STATE = "DELIVERY_UNCONFIRMED"
SEND_UNKNOWN = "SEND_UNKNOWN"
SEND_FAILED = "SEND_FAILED"
REJECTED = "REJECTED"
REVIEW_STATUSES = (
    PENDING,
    NEEDS_USER_INPUT,
    APPROVED,
    DELIVERY_UNCONFIRMED_STATE,
    SEND_UNKNOWN,
    SEND_FAILED,
)


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).isoformat()


class DraftError(RuntimeError):
    pass


class DraftExpiredError(DraftError):
    pass


class DraftConflictError(DraftError):
    pass


class SensitiveConfirmationRequired(DraftError):
    pass


class DraftStore:
    """Stores only local draft state; transport delivery is a separate step."""

    def __init__(self, path=":memory:"):
        # The local review HTTP server handles requests on a worker thread.
        # The lock below serialises store operations while this flag permits
        # that intentionally local connection to be used by the server.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS drafts(
              draft_id TEXT PRIMARY KEY,
              account_scope TEXT NOT NULL,
              contact_key TEXT NOT NULL,
              batch_id TEXT NOT NULL,
              source_message_ids TEXT NOT NULL,
              latest_message_id TEXT NOT NULL,
              context_ref TEXT NOT NULL,
              draft_text TEXT,
              final_text TEXT,
              status TEXT NOT NULL,
              needs_user_input INTEGER NOT NULL DEFAULT 0,
              risk_flags TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 0,
              approved_at TEXT,
              sent_at TEXT,
              send_state TEXT,
              superseded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS drafts_contact_active
              ON drafts(account_scope, contact_key, status);
            """
        )
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def create_draft(
        self,
        account_scope,
        contact_key,
        batch_id,
        source_message_ids,
        latest_message_id,
        context_ref,
        draft_text,
        *,
        needs_user_input=False,
        risk_flags=(),
        created_at=None,
        expires_in=timedelta(minutes=30),
        draft_id=None,
    ):
        with self._lock:
            source_message_ids = list(source_message_ids)
            if not source_message_ids:
                raise ValueError("at least one source message is required")
            created = _utc(created_at)
            if expires_in.total_seconds() <= 0:
                raise ValueError("draft expiry must be positive")
            draft_id = draft_id or uuid.uuid4().hex
            status = NEEDS_USER_INPUT if needs_user_input else PENDING
            with self.db:
                self.db.execute(
                    """UPDATE drafts SET status=?, superseded_at=?, version=version+1
                       WHERE account_scope=? AND contact_key=?
                         AND status IN (?,?,?)""",
                    (SUPERSEDED, _iso(created), account_scope, contact_key,
                     PENDING, NEEDS_USER_INPUT, APPROVED),
                )
                self.db.execute(
                    """INSERT INTO drafts(
                         draft_id,account_scope,contact_key,batch_id,source_message_ids,
                         latest_message_id,context_ref,draft_text,final_text,status,
                         needs_user_input,risk_flags,created_at,expires_at,version,
                         approved_at,sent_at,send_state,superseded_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (draft_id, account_scope, contact_key, batch_id,
                     json.dumps(source_message_ids, ensure_ascii=False),
                     latest_message_id, context_ref, draft_text, None, status,
                     int(bool(needs_user_input)),
                     json.dumps(list(risk_flags), ensure_ascii=False),
                     _iso(created), _iso(created + expires_in), 0,
                     None, None, None, None),
                )
            return self.get(draft_id)

    def get(self, draft_id):
        with self._lock:
            return self.db.execute(
                "SELECT * FROM drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()

    def list_drafts(self, *, statuses=REVIEW_STATUSES, now=None):
        """Return review-visible drafts after applying expiry transitions."""
        self.expire(now=now)
        statuses = tuple(statuses)
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            return self.db.execute(
                f"""SELECT * FROM drafts
                    WHERE status IN ({placeholders})
                    ORDER BY created_at DESC""",
                statuses,
            ).fetchall()

    def expire(self, *, now=None):
        timestamp = _iso(_utc(now))
        with self._lock, self.db:
            cursor = self.db.execute(
                """UPDATE drafts SET status=?, version=version+1
                   WHERE status IN (?,?,?) AND expires_at <= ?""",
                (EXPIRED, PENDING, NEEDS_USER_INPUT, APPROVED, timestamp),
            )
        return cursor.rowcount

    def approve(self, draft_id, *, edited_text=None, sensitive=False,
                second_confirmation=False, now=None):
        now = _utc(now)
        with self._lock:
            row = self.get(draft_id)
            if not row:
                raise KeyError("draft not found")
            if row["status"] not in (PENDING, NEEDS_USER_INPUT):
                raise DraftConflictError("draft is not pending or awaiting user input")
            if _utc(datetime.fromisoformat(row["expires_at"])) <= now:
                self.expire(now=now)
                raise DraftExpiredError("draft has expired")
            if row["status"] == NEEDS_USER_INPUT and edited_text is None:
                raise DraftConflictError("user input is required before approval")
            stored_risk_flags = json.loads(row["risk_flags"] or "[]")
            high_risk = "RISK_HIGH" in stored_risk_flags
            if (sensitive or high_risk) and not second_confirmation:
                raise SensitiveConfirmationRequired("sensitive draft needs a second confirmation")
            final_text = edited_text if edited_text is not None else row["draft_text"]
            if not isinstance(final_text, str) or not final_text.strip():
                raise DraftConflictError("approved text must not be empty")
            with self.db:
                cursor = self.db.execute(
                    """UPDATE drafts SET status=?, final_text=?, needs_user_input=0,
                         approved_at=?, version=version+1
                       WHERE draft_id=? AND status=? AND version=?""",
                    (APPROVED, final_text, _iso(now), draft_id, row["status"], row["version"]),
                )
        if cursor.rowcount != 1:
            raise DraftConflictError("draft changed while it was being approved")
        return self.get(draft_id)

    def reject(self, draft_id, *, now=None):
        """Reject a reviewable draft without touching transport state."""
        now = _utc(now)
        with self._lock:
            row = self.get(draft_id)
            if not row:
                raise KeyError("draft not found")
            if row["status"] not in (PENDING, NEEDS_USER_INPUT, APPROVED):
                raise DraftConflictError("draft is not reviewable")
            if _utc(datetime.fromisoformat(row["expires_at"])) <= now:
                self.expire(now=now)
                raise DraftExpiredError("draft has expired")
            with self.db:
                cursor = self.db.execute(
                    """UPDATE drafts SET status=?, version=version+1
                       WHERE draft_id=? AND status=? AND version=?""",
                    (REJECTED, draft_id, row["status"], row["version"]),
                )
            if cursor.rowcount != 1:
                raise DraftConflictError("draft changed while it was being rejected")
        return self.get(draft_id)

    def begin_send(self, draft_id, *, now=None):
        now = _utc(now)
        with self._lock:
            row = self.get(draft_id)
            if not row:
                raise KeyError("draft not found")
            if row["status"] != APPROVED:
                raise DraftConflictError("only an approved draft can be sent")
            if _utc(datetime.fromisoformat(row["expires_at"])) <= now:
                self.expire(now=now)
                raise DraftExpiredError("draft has expired")
            with self.db:
                cursor = self.db.execute(
                    """UPDATE drafts SET status=?, version=version+1
                       WHERE draft_id=? AND status=? AND version=?""",
                    (SENDING, draft_id, APPROVED, row["version"]),
                )
        if cursor.rowcount != 1:
            raise DraftConflictError("draft changed before sending")
        return self.get(draft_id)

    def record_send_state(self, draft_id, send_state, *, now=None):
        mapping = {
            API_ACCEPTED_WITH_RECEIPT: (SENT_CONFIRMED, True),
            DELIVERY_UNCONFIRMED: (DELIVERY_UNCONFIRMED_STATE, False),
            AMBIGUOUS: (SEND_UNKNOWN, False),
            DEFINITE_FAILED: (SEND_FAILED, False),
            INVALID_RESPONSE: (SEND_UNKNOWN, False),
        }
        final_state, delivered = mapping.get(send_state, (SEND_UNKNOWN, False))
        timestamp = _iso(_utc(now))
        with self._lock:
            with self.db:
                self.db.execute(
                    """UPDATE drafts SET status=?, send_state=?, sent_at=?,
                         version=version+1
                       WHERE draft_id=? AND status=?""",
                    (final_state, send_state, timestamp if delivered else None,
                     draft_id, SENDING),
                )
            return self.get(draft_id)
