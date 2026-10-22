"""Account-scoped claimable dedupe for Chat Agent inbound events."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading


CLAIMED = "CLAIMED"
COMPLETED = "COMPLETED"
RETRYABLE = "RETRYABLE"


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).isoformat()


def _key_value(event, *names):
    for name in names:
        value = event.get(name) if hasattr(event, "get") else getattr(event, name, None)
        if value is not None and str(value) != "":
            return str(value)
    return None


def stable_event_key(event):
    """Prefer transport IDs, then fall back to a canonical body digest."""
    for prefix, names in (
        ("message", ("message_id",)),
        ("client", ("client_id",)),
        ("seq", ("seq", "sequence")),
    ):
        value = _key_value(event, *names)
        if value is not None:
            return f"{prefix}:{value}"
    if hasattr(event, "items"):
        payload = dict(event)
    else:
        payload = {
            name: getattr(event, name)
            for name in getattr(event, "__dataclass_fields__", {})
        }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "body:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ClaimResult:
    key: str
    claimed: bool
    duplicate: bool
    state: str


class ChatInboxStore:
    """Durable admission claim; raw message text is not stored here."""

    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS inbound_claims(
                 account_scope TEXT NOT NULL,
                 event_key TEXT NOT NULL,
                 state TEXT NOT NULL,
                 claimed_at TEXT NOT NULL,
                 lease_until TEXT NOT NULL,
                 completed_at TEXT,
                 PRIMARY KEY(account_scope, event_key))"""
        )
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def claim(self, account_scope, event, *, now=None, lease_seconds=120):
        with self._lock:
            if not isinstance(account_scope, str) or not account_scope.strip():
                raise ValueError("account_scope is required")
            if lease_seconds <= 0:
                raise ValueError("lease_seconds must be positive")
            key = stable_event_key(event)
            now = _utc(now)
            lease_until = now + timedelta(seconds=lease_seconds)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    """SELECT * FROM inbound_claims
                       WHERE account_scope=? AND event_key=?""",
                    (account_scope, key),
                ).fetchone()
                if row and row["state"] == COMPLETED:
                    self.db.commit()
                    return ClaimResult(key, False, True, COMPLETED)
                if row and row["state"] == CLAIMED and _utc(datetime.fromisoformat(row["lease_until"])) > now:
                    self.db.commit()
                    return ClaimResult(key, False, True, CLAIMED)
                if row:
                    self.db.execute(
                        """UPDATE inbound_claims SET state=?, claimed_at=?,
                             lease_until=?, completed_at=NULL
                           WHERE account_scope=? AND event_key=?""",
                        (CLAIMED, _iso(now), _iso(lease_until), account_scope, key),
                    )
                else:
                    self.db.execute(
                        """INSERT INTO inbound_claims(
                             account_scope,event_key,state,claimed_at,lease_until,completed_at)
                           VALUES(?,?,?,?,?,NULL)""",
                        (account_scope, key, CLAIMED, _iso(now), _iso(lease_until)),
                    )
                self.db.commit()
                return ClaimResult(key, True, False, CLAIMED)
            except Exception:
                self.db.rollback()
                raise

    def complete(self, account_scope, event_key, *, now=None):
        with self._lock, self.db:
            self.db.execute(
                """UPDATE inbound_claims SET state=?, completed_at=?
                   WHERE account_scope=? AND event_key=? AND state=?""",
                (COMPLETED, _iso(_utc(now)), account_scope, event_key, CLAIMED),
            )

    def release(self, account_scope, event_key, *, now=None):
        with self._lock, self.db:
            self.db.execute(
                """UPDATE inbound_claims SET state=?, claimed_at=?, lease_until=?
                   WHERE account_scope=? AND event_key=? AND state=?""",
                (RETRYABLE, _iso(_utc(now)), _iso(_utc(now)),
                 account_scope, event_key, CLAIMED),
            )

    def get(self, account_scope, event_key):
        with self._lock:
            return self.db.execute(
                """SELECT * FROM inbound_claims
                   WHERE account_scope=? AND event_key=?""",
                (account_scope, event_key),
            ).fetchone()
