"""Bounded, redacted observability for the Chat Agent pilot.

Audit rows contain stable fingerprints and lifecycle metadata only.  Message
text, raw user IDs, recipients, context references/tokens, model prompts, and
exception values are intentionally not accepted by this store.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
import threading
import uuid


ROUTE_EVENT = "route"
DRAFT_EVENT = "draft"
SEND_EVENT = "send"
RECOVERY_EVENT = "recovery"
EVENT_TYPES = (ROUTE_EVENT, DRAFT_EVENT, SEND_EVENT, RECOVERY_EVENT)


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).isoformat()


def _code(value, *, default=None, limit=120):
    if value is None:
        return default
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:limit] if value else default


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    event_type: str
    occurred_at: str
    account_scope: str | None
    contact_key: str | None
    batch_id: str | None
    draft_id: str | None
    route: str | None
    mode: str | None
    model: str | None
    latency_ms: int | None
    risk_level: str | None
    risk_flags: tuple[str, ...]
    source_count: int | None
    draft_generated: bool | None
    send_state: str | None
    detail: str | None


class ChatAuditStore:
    """SQLite-backed redacted lifecycle audit with bounded local retention."""

    def __init__(self, path=":memory:", *, max_events=5000):
        if not isinstance(max_events, int) or max_events <= 0:
            raise ValueError("max_events must be positive")
        self.max_events = max_events
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_events(
              event_id TEXT PRIMARY KEY,
              event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              account_scope TEXT,
              contact_key TEXT,
              batch_id TEXT,
              draft_id TEXT,
              route TEXT,
              mode TEXT,
              model TEXT,
              latency_ms INTEGER,
              risk_level TEXT,
              risk_flags TEXT NOT NULL DEFAULT '[]',
              source_count INTEGER,
              draft_generated INTEGER,
              send_state TEXT,
              detail TEXT
            );
            CREATE INDEX IF NOT EXISTS audit_events_time
              ON audit_events(occurred_at, event_id);
            """
        )
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def record(
        self,
        event_type,
        *,
        occurred_at=None,
        account_scope=None,
        contact_key=None,
        batch_id=None,
        draft_id=None,
        route=None,
        mode=None,
        model=None,
        latency_ms=None,
        risk_level=None,
        risk_flags=(),
        source_count=None,
        draft_generated=None,
        send_state=None,
        detail=None,
    ):
        if event_type not in EVENT_TYPES:
            raise ValueError("unsupported audit event type")
        if account_scope is not None and (not isinstance(account_scope, str) or not account_scope.strip()):
            raise ValueError("account_scope must be non-empty text")
        if contact_key is not None and (not isinstance(contact_key, str) or not contact_key.strip()):
            raise ValueError("contact_key must be non-empty text")
        if latency_ms is not None and (not isinstance(latency_ms, int) or latency_ms < 0):
            raise ValueError("latency_ms must be a non-negative integer")
        if source_count is not None and (not isinstance(source_count, int) or source_count < 0):
            raise ValueError("source_count must be a non-negative integer")
        if draft_generated is not None and not isinstance(draft_generated, bool):
            raise ValueError("draft_generated must be boolean")
        clean_flags = []
        for flag in risk_flags or ():
            value = _code(flag, limit=64)
            if value and value not in clean_flags:
                clean_flags.append(value)
        event = AuditEvent(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            occurred_at=_iso(occurred_at),
            account_scope=_code(account_scope),
            contact_key=_code(contact_key),
            batch_id=_code(batch_id),
            draft_id=_code(draft_id),
            route=_code(route),
            mode=_code(mode),
            model=_code(model),
            latency_ms=latency_ms,
            risk_level=_code(risk_level),
            risk_flags=tuple(clean_flags),
            source_count=source_count,
            draft_generated=draft_generated,
            send_state=_code(send_state),
            # Detail is a short stable code, never an exception string/body.
            detail=(
                re.sub(r"[^A-Za-z0-9_.:-]+", "_", _code(detail, limit=64))
                if detail is not None else None
            ),
        )
        with self._lock, self.db:
            self.db.execute(
                """INSERT INTO audit_events(
                     event_id,event_type,occurred_at,account_scope,contact_key,
                     batch_id,draft_id,route,mode,model,latency_ms,risk_level,
                     risk_flags,source_count,draft_generated,send_state,detail)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id, event.event_type, event.occurred_at,
                    event.account_scope, event.contact_key, event.batch_id,
                    event.draft_id, event.route, event.mode, event.model,
                    event.latency_ms, event.risk_level,
                    json.dumps(event.risk_flags, ensure_ascii=False),
                    event.source_count,
                    None if event.draft_generated is None else int(event.draft_generated),
                    event.send_state, event.detail,
                ),
            )
            stale = self.db.execute(
                """SELECT event_id FROM audit_events
                   ORDER BY occurred_at DESC, event_id DESC
                   LIMIT -1 OFFSET ?""",
                (self.max_events,),
            ).fetchall()
            if stale:
                self.db.executemany("DELETE FROM audit_events WHERE event_id=?", ((row["event_id"],) for row in stale))
        return event

    def list_events(self, *, limit=100):
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self.db.execute(
                """SELECT * FROM audit_events
                   ORDER BY occurred_at DESC, event_id DESC LIMIT ?""",
                (min(limit, self.max_events),),
            ).fetchall()
        return tuple(
            AuditEvent(
                row["event_id"], row["event_type"], row["occurred_at"],
                row["account_scope"], row["contact_key"], row["batch_id"],
                row["draft_id"], row["route"], row["mode"], row["model"],
                row["latency_ms"], row["risk_level"],
                tuple(json.loads(row["risk_flags"] or "[]")),
                row["source_count"],
                None if row["draft_generated"] is None else bool(row["draft_generated"]),
                row["send_state"], row["detail"],
            )
            for row in rows
        )
