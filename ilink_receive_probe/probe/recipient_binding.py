"""DPAPI-protected, single-recipient binding for a proactive send pilot."""
import hashlib
import json
import os
from pathlib import Path

from .credential_store import CredentialStore


class RecipientBindingCollision(RuntimeError):
    """A different recipient attempted to replace an existing binding."""


class RecipientBindingUnavailable(RuntimeError):
    """A proactive send has no valid, single-recipient DPAPI binding."""


class RecipientBindingStore:
    """Stores a raw recipient ID only in a dedicated DPAPI Current User blob."""

    def __init__(self, state_dir=None, protect=None, unprotect=None):
        base = Path(state_dir) if state_dir else Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        ) / "ilink-receive-probe"
        self.state_dir = base
        self.path = base / "recipient-binding.dat"
        self.meta = base / "recipient-binding.meta.json"
        if protect is None or unprotect is None:
            dpapi = CredentialStore(base)
            protect = dpapi._protect
            unprotect = lambda value: dpapi._protect(value, True)
        self._protect = protect
        self._unprotect = unprotect

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    def bind(self, event):
        if (
            event.conversation_type != "one_to_one"
            or event.is_forwarded
            or not event.reply_to_user_id
            or self._fingerprint(event.reply_to_user_id) != event.sender_fingerprint
        ):
            raise RuntimeError("recipient binding requires a direct, non-forwarded, fingerprint-matched inbound")
        candidate = {"recipient_id": event.reply_to_user_id, "sender_fingerprint": event.sender_fingerprint}
        if self.path.exists():
            current = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
            if current != candidate:
                raise RecipientBindingCollision("existing recipient binding differs; refusing overwrite")
            return "REUSED"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self._protect(json.dumps(candidate, separators=(",", ":")).encode("utf-8")))
        # This metadata deliberately contains fingerprints only.
        self.meta.write_text(json.dumps({
            "schema_version": 1,
            "recipient_fingerprint": event.sender_fingerprint,
            "sender_fingerprint": event.sender_fingerprint,
            "storage": "DPAPI_CURRENT_USER",
        }, separators=(",", ":")), encoding="utf-8")
        return "BOUND"

    def delete(self):
        for value in (self.path, self.meta):
            try:
                value.unlink()
            except FileNotFoundError:
                pass

    def load_for_proactive_send(self, expected_fingerprint):
        """Return the one raw recipient only after all binding invariants hold."""
        if not self.path.exists() or not self.meta.exists():
            raise RecipientBindingUnavailable("recipient binding is unavailable")
        try:
            metadata = json.loads(self.meta.read_text(encoding="utf-8"))
            current = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
        except Exception as error:
            raise RecipientBindingUnavailable("recipient binding DPAPI decrypt failed") from error
        recipient = current.get("recipient_id")
        fingerprint = current.get("sender_fingerprint")
        if (
            not isinstance(recipient, str)
            or fingerprint != expected_fingerprint
            or metadata.get("recipient_fingerprint") != expected_fingerprint
            or metadata.get("sender_fingerprint") != expected_fingerprint
            or self._fingerprint(recipient) != expected_fingerprint
        ):
            raise RecipientBindingUnavailable("recipient binding fingerprint validation failed")
        return recipient

    def describe_location(self):
        return {
            "directory": str(self.state_dir), "file": str(self.path), "metadata": str(self.meta),
            "plaintext": False, "scope": "CURRENT_USER",
        }
