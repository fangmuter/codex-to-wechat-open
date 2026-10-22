"""Manual, multi-contact pairing for Chat Agent.

Only stable fingerprints are stored here. Raw iLink user IDs remain owned by
the transport/credential boundary and must not be auto-bound on first sight.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
import threading


MODES = ("review", "auto", "disabled")
PAIRING_WINDOW_SECONDS = 300


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _from_iso(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


@dataclass(frozen=True)
class ContactRecord:
    account_scope: str
    sender_fingerprint: str
    bot_fingerprint: str
    alias: str
    mode: str
    state: str
    consent_recorded: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PairingCandidate:
    account_scope: str
    sender_fingerprint: str
    bot_fingerprint: str
    message_id: str
    preview_text: str
    observed_at: str
    expires_at: str


class PairingError(RuntimeError):
    """Base class for pairing failures."""


class PairingExpiredError(PairingError):
    pass


class ConsentRequiredError(PairingError):
    pass


class ContactPairingStore:
    """SQLite-backed candidate pairing and per-contact mode state."""

    def __init__(self, path=":memory:"):
        # The local review server handles HTTP requests in a worker thread,
        # while the receiver/pipeline may observe candidates from its own
        # thread.  Keep one store connection, but serialize all store access
        # explicitly instead of relying on sqlite's same-thread default.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS pairing_candidates(
              account_scope TEXT NOT NULL,
              sender_fingerprint TEXT NOT NULL,
              bot_fingerprint TEXT NOT NULL,
              message_id TEXT NOT NULL,
              preview_text TEXT NOT NULL DEFAULT '',
              observed_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              PRIMARY KEY(account_scope, sender_fingerprint, bot_fingerprint, message_id)
            );
            CREATE TABLE IF NOT EXISTS contacts(
              account_scope TEXT NOT NULL,
              sender_fingerprint TEXT NOT NULL,
              bot_fingerprint TEXT NOT NULL,
              alias TEXT NOT NULL,
              mode TEXT NOT NULL CHECK(mode IN ('review','auto','disabled')),
              state TEXT NOT NULL CHECK(state IN ('allowed','disabled')),
              consent_recorded INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(account_scope, sender_fingerprint, bot_fingerprint)
            );
            """
        )
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(pairing_candidates)")}
        if "preview_text" not in existing:
            self.db.execute("ALTER TABLE pairing_candidates ADD COLUMN preview_text TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    @staticmethod
    def _validate_identity(account_scope, sender_fingerprint, bot_fingerprint):
        if not all(isinstance(value, str) and value.strip()
                   for value in (account_scope, sender_fingerprint, bot_fingerprint)):
            raise ValueError("account scope and fingerprints are required")

    def observe_candidate(
        self,
        account_scope,
        sender_fingerprint,
        bot_fingerprint,
        message_id,
        *,
        observed_at=None,
        pairing_window_seconds=PAIRING_WINDOW_SECONDS,
        preview_text="",
    ):
        with self._lock:
            self._validate_identity(account_scope, sender_fingerprint, bot_fingerprint)
            if not isinstance(message_id, str) or not message_id.strip():
                raise ValueError("message_id is required")
            observed = _utc(observed_at)
            if pairing_window_seconds <= 0:
                raise ValueError("pairing window must be positive")
            if not isinstance(preview_text, str):
                raise ValueError("preview_text must be text")
            preview_text = preview_text.strip()[:500]
            expires = observed + timedelta(seconds=pairing_window_seconds)
            self.db.execute(
                """INSERT OR REPLACE INTO pairing_candidates(
                     account_scope,sender_fingerprint,bot_fingerprint,message_id,
                     preview_text,observed_at,expires_at) VALUES(?,?,?,?,?,?,?)""",
                (account_scope, sender_fingerprint, bot_fingerprint, message_id,
                 preview_text, _iso(observed), _iso(expires)),
            )
            self.db.commit()
            return self.db.execute(
                """SELECT * FROM pairing_candidates
                   WHERE account_scope=? AND sender_fingerprint=?
                     AND bot_fingerprint=? AND message_id=?""",
                (account_scope, sender_fingerprint, bot_fingerprint, message_id),
            ).fetchone()

    def list_candidates(self, *, now=None):
        with self._lock:
            timestamp = _iso(_utc(now))
            rows = self.db.execute(
                """SELECT * FROM pairing_candidates AS candidate
                   WHERE candidate.expires_at > ?
                     AND NOT EXISTS (
                       SELECT 1 FROM contacts AS contact
                       WHERE contact.account_scope = candidate.account_scope
                         AND contact.sender_fingerprint = candidate.sender_fingerprint
                         AND contact.bot_fingerprint = candidate.bot_fingerprint
                     )
                   ORDER BY candidate.observed_at DESC""",
                (timestamp,),
            ).fetchall()
            return tuple(
                PairingCandidate(
                    row["account_scope"], row["sender_fingerprint"], row["bot_fingerprint"],
                    row["message_id"], row["preview_text"] or "", row["observed_at"], row["expires_at"],
                )
                for row in rows
            )

    def _candidate(self, account_scope, sender_fingerprint, bot_fingerprint, now):
        with self._lock:
            row = self.db.execute(
                """SELECT * FROM pairing_candidates
                   WHERE account_scope=? AND sender_fingerprint=?
                     AND bot_fingerprint=?
                   ORDER BY observed_at DESC LIMIT 1""",
                (account_scope, sender_fingerprint, bot_fingerprint),
            ).fetchone()
            if not row or _from_iso(row["expires_at"]) <= _utc(now):
                return None
            return row

    def confirm_candidate(
        self,
        account_scope,
        sender_fingerprint,
        bot_fingerprint,
        *,
        alias,
        mode="review",
        consent_recorded=False,
        now=None,
    ):
        with self._lock:
            self._validate_identity(account_scope, sender_fingerprint, bot_fingerprint)
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError("alias is required")
            if mode not in MODES:
                raise ValueError("unsupported contact mode")
            if mode == "auto" and not consent_recorded:
                raise ConsentRequiredError("AUTO requires recorded consent")
            if not self._candidate(account_scope, sender_fingerprint, bot_fingerprint, now):
                raise PairingExpiredError("pairing candidate is missing or expired")
            timestamp = _iso(_utc(now))
            self.db.execute(
                """INSERT INTO contacts(
                     account_scope,sender_fingerprint,bot_fingerprint,alias,mode,
                     state,consent_recorded,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_scope,sender_fingerprint,bot_fingerprint)
                   DO UPDATE SET alias=excluded.alias, mode=excluded.mode,
                     state=excluded.state, consent_recorded=excluded.consent_recorded,
                     updated_at=excluded.updated_at""",
                (account_scope, sender_fingerprint, bot_fingerprint, alias.strip(),
                 mode, "allowed", int(bool(consent_recorded)), timestamp, timestamp),
            )
            self.db.commit()
            return self.get(account_scope, sender_fingerprint, bot_fingerprint)

    def get(self, account_scope, sender_fingerprint, bot_fingerprint):
        with self._lock:
            row = self.db.execute(
                """SELECT * FROM contacts
                   WHERE account_scope=? AND sender_fingerprint=? AND bot_fingerprint=?""",
                (account_scope, sender_fingerprint, bot_fingerprint),
            ).fetchone()
            if not row:
                return None
            return ContactRecord(
                row["account_scope"], row["sender_fingerprint"], row["bot_fingerprint"],
                row["alias"], row["mode"], row["state"], bool(row["consent_recorded"]),
                row["created_at"], row["updated_at"],
            )

    def record_consent(self, account_scope, sender_fingerprint, bot_fingerprint, *, now=None):
        with self._lock:
            if not self.get(account_scope, sender_fingerprint, bot_fingerprint):
                raise KeyError("contact is not paired")
            self.db.execute(
                """UPDATE contacts SET consent_recorded=1, updated_at=?
                   WHERE account_scope=? AND sender_fingerprint=? AND bot_fingerprint=?""",
                (_iso(_utc(now)), account_scope, sender_fingerprint, bot_fingerprint),
            )
            self.db.commit()
            return self.get(account_scope, sender_fingerprint, bot_fingerprint)

    def set_mode(self, account_scope, sender_fingerprint, bot_fingerprint, mode, *, now=None):
        with self._lock:
            if mode not in MODES:
                raise ValueError("unsupported contact mode")
            contact = self.get(account_scope, sender_fingerprint, bot_fingerprint)
            if not contact:
                raise KeyError("contact is not paired")
            if mode == "auto" and not contact.consent_recorded:
                raise ConsentRequiredError("AUTO requires recorded consent")
            self.db.execute(
                """UPDATE contacts SET mode=?, updated_at=?
                   WHERE account_scope=? AND sender_fingerprint=? AND bot_fingerprint=?""",
                (mode, _iso(_utc(now)), account_scope, sender_fingerprint, bot_fingerprint),
            )
            self.db.commit()
            return self.get(account_scope, sender_fingerprint, bot_fingerprint)

    def is_allowed(self, account_scope, sender_fingerprint, bot_fingerprint):
        with self._lock:
            contact = self.get(account_scope, sender_fingerprint, bot_fingerprint)
            return bool(contact and contact.state == "allowed" and contact.mode != "disabled")
