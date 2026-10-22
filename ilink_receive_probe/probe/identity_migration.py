"""Rollback-safe staged iLink identity promotion primitives.

The class in this module is an operator workflow primitive, not a login
client.  QR authentication, staged probes, and user approval happen outside
it.  ``promote`` only accepts an already validated staging directory and
updates the credential metadata, allowed-bot metadata, and receive checkpoint
as one guarded transaction with an exact rollback snapshot.  It is never
called by the Delivery runner automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .credential_store import CredentialStore
from .storage import ProbeStorage


class IdentityPromotionError(RuntimeError):
    """The staged identity cannot be promoted without risking mixed state."""


def _fingerprint(value) -> str | None:
    if value is None:
        return None
    value = str(value)
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _safe_metadata(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError) as error:
        raise IdentityPromotionError("credential metadata is unavailable") from error
    if not isinstance(value, dict):
        raise IdentityPromotionError("credential metadata is invalid")
    return value


@dataclass(frozen=True)
class PromotionValidation:
    account_fingerprint: str
    bot_fingerprint: str
    credential_fingerprint: str | None
    checkpoint_present: bool


@dataclass(frozen=True)
class RollbackSnapshot:
    directory: Path
    credential_fingerprint: str | None
    bot_fingerprint: str | None
    checkpoint_fingerprint: str
    created_at: str


class StagedIdentityPromotion:
    """Promote one validated staging identity with rollback on every error."""

    def __init__(self, production_credential: CredentialStore,
                 staging_credential: CredentialStore, production_inbound_db,
                 staging_inbound_db, *, snapshot_dir=None,
                 expected_account_fingerprint=None,
                 process_guard=None):
        self.production_credential = production_credential
        self.staging_credential = staging_credential
        self.production_inbound_db = Path(production_inbound_db)
        self.staging_inbound_db = Path(staging_inbound_db)
        default_snapshot = Path(os.environ.get(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local"
        )) / "ilink-receive-probe" / "rollback"
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else default_snapshot
        self.expected_account_fingerprint = expected_account_fingerprint
        self.process_guard = process_guard

    @staticmethod
    def _checkpoint(path: Path) -> str:
        storage = ProbeStorage(path)
        try:
            return storage.cursor()
        finally:
            storage.close()

    def validate(self) -> PromotionValidation:
        if self.process_guard is not None and not self.process_guard():
            raise IdentityPromotionError("production process is active")
        try:
            token = self.staging_credential.load()
        except Exception as error:
            raise IdentityPromotionError("staged credential is unreadable") from error
        if not isinstance(token, str) or not token:
            raise IdentityPromotionError("staged credential is empty")
        metadata = _safe_metadata(self.staging_credential.meta)
        account = metadata.get("account_fingerprint")
        bot = metadata.get("bot_fingerprint")
        if (not isinstance(account, str)
                or not re.fullmatch(r"[0-9a-fA-F]{8,64}", account)):
            raise IdentityPromotionError("staged account identity is missing")
        if (not isinstance(bot, str)
                or not re.fullmatch(r"[0-9a-fA-F]{8,64}", bot)):
            raise IdentityPromotionError("staged bot identity is missing")
        if (self.expected_account_fingerprint is not None
                and account != self.expected_account_fingerprint):
            raise IdentityPromotionError("staged account identity mismatch")
        checkpoint = self._checkpoint(self.staging_inbound_db)
        if not checkpoint:
            raise IdentityPromotionError("staged receive checkpoint is missing")
        credential_fp = metadata.get("credential_fingerprint") or _fingerprint(token)
        return PromotionValidation(account, bot, credential_fp, True)

    def _snapshot(self, validation: PromotionValidation) -> RollbackSnapshot:
        directory = self.snapshot_dir / (
            "identity-rollback-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-" + uuid.uuid4().hex[:8]
        )
        directory.mkdir(parents=True, exist_ok=False)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        # Only the encrypted DPAPI blob and redacted metadata are copied.  No
        # plaintext credential is ever written by this operation.
        credential_blob = self.production_credential.path.read_bytes()
        metadata_blob = self.production_credential.meta.read_bytes()
        (directory / "credential.dat").write_bytes(credential_blob)
        (directory / "credential.meta.json").write_bytes(metadata_blob)
        old_cursor = self._checkpoint(self.production_inbound_db)
        (directory / "cursor.bin").write_bytes(old_cursor.encode("utf-8"))
        for child in directory.iterdir():
            try:
                os.chmod(child, 0o600)
            except OSError:
                pass
        return RollbackSnapshot(
            directory=directory,
            credential_fingerprint=_fingerprint(credential_blob),
            bot_fingerprint=_safe_metadata(self.production_credential.meta).get(
                "bot_fingerprint"
            ),
            checkpoint_fingerprint=_fingerprint(old_cursor) or "",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _atomic_copy(source: Path, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _set_checkpoint(self, cursor: str):
        storage = ProbeStorage(self.production_inbound_db)
        try:
            storage.advance(cursor)
        finally:
            storage.close()

    def _restore(self, snapshot: RollbackSnapshot):
        self._atomic_copy(snapshot.directory / "credential.dat",
                          self.production_credential.path)
        self._atomic_copy(snapshot.directory / "credential.meta.json",
                          self.production_credential.meta)
        self._set_checkpoint((snapshot.directory / "cursor.bin").read_text(encoding="utf-8"))

    def promote(self) -> tuple[PromotionValidation, RollbackSnapshot]:
        validation = self.validate()
        snapshot = self._snapshot(validation)
        staged_meta = _safe_metadata(self.staging_credential.meta)
        staged_meta["state"] = "PRODUCTION"
        # Prepare the metadata in a private temporary directory first.  The
        # DPAPI credential itself remains ciphertext throughout the swap.
        temporary_meta = self.snapshot_dir / (
            ".promotion-meta-" + uuid.uuid4().hex + ".json"
        )
        try:
            temporary_meta.parent.mkdir(parents=True, exist_ok=True)
            temporary_meta.write_text(
                json.dumps(staged_meta, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            self._atomic_copy(self.staging_credential.path,
                              self.production_credential.path)
            os.replace(temporary_meta, self.production_credential.meta)
            self._set_checkpoint(self._checkpoint(self.staging_inbound_db))
        except Exception as error:
            try:
                self._restore(snapshot)
            except Exception as rollback_error:
                raise IdentityPromotionError(
                    "identity promotion failed and rollback failed"
                ) from rollback_error
            raise IdentityPromotionError("identity promotion rolled back") from error
        finally:
            try:
                temporary_meta.unlink()
            except FileNotFoundError:
                pass
        return validation, snapshot


__all__ = [
    "IdentityPromotionError", "PromotionValidation", "RollbackSnapshot",
    "StagedIdentityPromotion",
]
