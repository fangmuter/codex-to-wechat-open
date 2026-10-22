"""Bound, context-free iLink transport for Phase 1.6C scheduler ticks."""

from dataclasses import dataclass
import json
import sqlite3

from ilink_receive_probe.probe.proactive_send import (
    AmbiguousProactiveSendError,
    DefiniteProactiveSendError,
    IlinkResponseDiagnostics,
    ProactiveIlinkTransport,
    sanitize_errmsg,
)


@dataclass(frozen=True)
class DeliveryResult:
    state: str  # ACKED_API | UNAVAILABLE | UNKNOWN | FAILED
    submitted: bool = False
    http_status: int | None = None
    ret: object = None
    errcode: object = None
    errmsg: str | None = None
    response_keys: tuple[str, ...] = ()
    attempt_no: int | None = None


def _as_text(value):
    return None if value is None else str(value)


def _response_keys_json(keys):
    return json.dumps(sorted({str(key) for key in (keys or ())}),
                      ensure_ascii=True, separators=(",", ":"))


class DeliveryLedger:
    """Durable redacted idempotency state and append-only attempt metadata.

    The primary row remains backward compatible with the Phase 1.6C schema.
    ``proactive_delivery_attempt_history`` preserves every attempt so an
    explicitly authorized retry cannot erase the original failure.
    """

    _METADATA_COLUMNS = {
        "http_status": "INTEGER",
        "ret": "TEXT",
        "api_errcode": "TEXT",
        "errmsg": "TEXT",
        "response_keys": "TEXT",
    }

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS proactive_delivery_attempts(
            idempotency_key TEXT PRIMARY KEY, recipient_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, submitted INTEGER NOT NULL, updated_at TEXT NOT NULL,
            errcode TEXT, http_status INTEGER, ret TEXT, api_errcode TEXT,
            errmsg TEXT, response_keys TEXT)""")
        self._ensure_columns("proactive_delivery_attempts", self._METADATA_COLUMNS)
        self.db.execute("""CREATE TABLE IF NOT EXISTS proactive_delivery_attempt_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            recipient_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            submitted INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            errcode TEXT,
            http_status INTEGER,
            ret TEXT,
            api_errcode TEXT,
            errmsg TEXT,
            response_keys TEXT,
            UNIQUE(idempotency_key, attempt_no))""")
        self.db.commit()
        self._active_attempts = {}

    def _ensure_columns(self, table, columns):
        present = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in present:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def close(self):
        self.db.close()

    @staticmethod
    def _diagnostic_values(response):
        if response is None:
            return None, None, None, None, None
        diagnostics = getattr(response, "diagnostics", response)
        if isinstance(diagnostics, dict):
            http_status = diagnostics.get("http_status")
            ret = diagnostics.get("ret")
            api_errcode = diagnostics.get("errcode")
            errmsg = diagnostics.get("errmsg")
            response_keys = diagnostics.get("response_keys", ())
        else:
            http_status = getattr(diagnostics, "http_status", None)
            ret = getattr(diagnostics, "ret", None)
            api_errcode = getattr(diagnostics, "errcode", None)
            errmsg = getattr(diagnostics, "errmsg", None)
            response_keys = getattr(diagnostics, "response_keys", ())
        return (
            http_status,
            _as_text(ret),
            _as_text(api_errcode),
            sanitize_errmsg(errmsg),
            _response_keys_json(response_keys),
        )

    def _insert_history(self, key, attempt_no, fingerprint, state, now,
                        errcode=None, response=None):
        http_status, ret, api_errcode, errmsg, response_keys = self._diagnostic_values(response)
        self.db.execute(
            """INSERT INTO proactive_delivery_attempt_history(
                idempotency_key,attempt_no,recipient_fingerprint,state,submitted,
                updated_at,errcode,http_status,ret,api_errcode,errmsg,response_keys)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, attempt_no, fingerprint, state, 1, now.isoformat(),
             _as_text(errcode), http_status, ret, api_errcode, errmsg, response_keys),
        )

    def _ensure_legacy_history(self, key, existing):
        count = self.db.execute(
            "SELECT COUNT(*) FROM proactive_delivery_attempt_history WHERE idempotency_key=?",
            (key,),
        ).fetchone()[0]
        if count:
            return
        # Existing production rows predate response observability.  Preserve
        # that first attempt as-is, with unavailable fields left NULL.
        self.db.execute(
            """INSERT INTO proactive_delivery_attempt_history(
                idempotency_key,attempt_no,recipient_fingerprint,state,submitted,
                updated_at,errcode,http_status,ret,api_errcode,errmsg,response_keys)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, 1, existing["recipient_fingerprint"], existing["state"],
             existing["submitted"], existing["updated_at"], existing["errcode"],
             existing["http_status"], existing["ret"], existing["api_errcode"],
             existing["errmsg"], existing["response_keys"]),
        )

    def begin(self, key, fingerprint, now, *, allow_failed_retry=False):
        existing = self.get(key)
        if existing:
            if not (allow_failed_retry and existing["state"] == "FAILED"):
                return False
            self._ensure_legacy_history(key, existing)
            latest = self.db.execute(
                "SELECT MAX(attempt_no) FROM proactive_delivery_attempt_history WHERE idempotency_key=?",
                (key,),
            ).fetchone()[0] or 0
            if latest >= 2:
                self.db.commit()
                return False
            attempt_no = latest + 1
            self._insert_history(key, attempt_no, fingerprint, "SENDING", now)
            self.db.execute(
                """UPDATE proactive_delivery_attempts
                   SET state='SENDING',submitted=1,updated_at=?,errcode=NULL,
                       http_status=NULL,ret=NULL,api_errcode=NULL,errmsg=NULL,
                       response_keys=NULL WHERE idempotency_key=?""",
                (now.isoformat(), key),
            )
            self.db.commit()
            self._active_attempts[key] = attempt_no
            return True

        try:
            self.db.execute(
                """INSERT INTO proactive_delivery_attempts(
                    idempotency_key,recipient_fingerprint,state,submitted,updated_at,
                    errcode,http_status,ret,api_errcode,errmsg,response_keys)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (key, fingerprint, "SENDING", 1, now.isoformat(),
                 None, None, None, None, None, None),
            )
            self._insert_history(key, 1, fingerprint, "SENDING", now)
            self.db.commit()
            self._active_attempts[key] = 1
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

    def get(self, key):
        return self.db.execute(
            "SELECT * FROM proactive_delivery_attempts WHERE idempotency_key=?",
            (key,),
        ).fetchone()

    def attempts(self, key):
        return self.db.execute(
            "SELECT * FROM proactive_delivery_attempt_history WHERE idempotency_key=? ORDER BY attempt_no",
            (key,),
        ).fetchall()

    def record(self, key, state, now, errcode=None, *, response=None):
        http_status, ret, api_errcode, errmsg, response_keys = self._diagnostic_values(response)
        self.db.execute(
            """UPDATE proactive_delivery_attempts
               SET state=?,updated_at=?,errcode=?,http_status=?,ret=?,api_errcode=?,
                   errmsg=?,response_keys=? WHERE idempotency_key=?""",
            (state, now.isoformat(), None if errcode is None else str(errcode),
             http_status, ret, api_errcode, errmsg, response_keys, key),
        )
        attempt_no = self._active_attempts.get(key)
        if attempt_no is None:
            row = self.db.execute(
                "SELECT MAX(attempt_no) FROM proactive_delivery_attempt_history WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            attempt_no = (row[0] or 1)
        self.db.execute(
            """UPDATE proactive_delivery_attempt_history
               SET state=?,updated_at=?,errcode=?,http_status=?,ret=?,api_errcode=?,
                   errmsg=?,response_keys=? WHERE idempotency_key=? AND attempt_no=?""",
            (state, now.isoformat(), None if errcode is None else str(errcode),
             http_status, ret, api_errcode, errmsg, response_keys, key, attempt_no),
        )
        self.db.commit()
        self._active_attempts.pop(key, None)


class BoundIlinkProactiveTransport:
    """Every send loads the sole recipient from DPAPI and never accepts it as input."""

    def __init__(self, token, binding_store, ledger, expected_fingerprint):
        self._api = ProactiveIlinkTransport(token)
        self._binding_store = binding_store
        self._ledger = ledger
        self._expected_fingerprint = expected_fingerprint
        self.calls = []

    @staticmethod
    def _diagnostics(value):
        if isinstance(value, IlinkResponseDiagnostics):
            return value
        if isinstance(value, dict):
            return IlinkResponseDiagnostics(
                http_status=value.get("http_status"),
                ret=value.get("ret"),
                errcode=value.get("errcode"),
                errmsg=sanitize_errmsg(value.get("errmsg")),
                response_keys=tuple(sorted(str(key) for key in value.keys())),
            )
        return IlinkResponseDiagnostics()

    @staticmethod
    def _result(state, submitted, diagnostics=None, attempt_no=None):
        diagnostics = diagnostics or IlinkResponseDiagnostics()
        return DeliveryResult(
            state, submitted,
            diagnostics.http_status, diagnostics.ret, diagnostics.errcode,
            diagnostics.errmsg, diagnostics.response_keys, attempt_no,
        )

    def send(self, text, idempotency_key, now, *, retry_failed=False,
             context_token=None):
        # Raw recipient lifetime is limited to this method and the HTTP body.
        recipient = self._binding_store.load_for_proactive_send(self._expected_fingerprint)
        if not self._ledger.begin(
            idempotency_key, self._expected_fingerprint, now,
            allow_failed_retry=retry_failed,
        ):
            existing = self._ledger.get(idempotency_key)
            diagnostics = IlinkResponseDiagnostics(
                http_status=existing["http_status"],
                ret=existing["ret"],
                errcode=existing["api_errcode"],
                errmsg=existing["errmsg"],
                response_keys=tuple(json.loads(existing["response_keys"] or "[]")),
            )
            latest = self._ledger.attempts(idempotency_key)
            attempt_no = latest[-1]["attempt_no"] if latest else None
            return self._result(existing["state"], bool(existing["submitted"]), diagnostics, attempt_no)
        self.calls.append("sendmessage")
        attempt_no = self._ledger._active_attempts.get(idempotency_key)
        try:
            response = self._api.send_text(
                recipient, text, idempotency_key, "ilink_receive_probe/phase16c",
                context_token=context_token,
            )
        except AmbiguousProactiveSendError as error:
            diagnostics = getattr(error, "diagnostics", IlinkResponseDiagnostics())
            self._ledger.record(idempotency_key, "UNKNOWN", now, "AMBIGUOUS", response=diagnostics)
            return self._result("UNKNOWN", True, diagnostics, attempt_no)
        except DefiniteProactiveSendError as error:
            diagnostics = getattr(error, "diagnostics", IlinkResponseDiagnostics())
            self._ledger.record(idempotency_key, "FAILED", now, error.errcode, response=diagnostics)
            return self._result("FAILED", True, diagnostics, attempt_no)
        diagnostics = self._diagnostics(response)
        self._ledger.record(idempotency_key, "ACKED_API", now, response=diagnostics)
        return self._result("ACKED_API", True, diagnostics, attempt_no)
