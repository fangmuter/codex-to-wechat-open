"""Fail-closed contact router for the Chat Agent C2 stage."""

from dataclasses import dataclass

from .contacts import ContactPairingStore
from .inbox import ChatInboxStore


MATCHED_CONTACT = "MATCHED_CONTACT"
IGNORED_CONTACT = "IGNORED_CONTACT"
IGNORED_UNSUPPORTED = "IGNORED_UNSUPPORTED"
DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
CHAT_AGENT_DISABLED = "CHAT_AGENT_DISABLED"


@dataclass(frozen=True)
class RouterDecision:
    route: str
    contact_alias: str | None = None
    mode: str | None = None
    claim_key: str | None = None


class ChatAgentRouter:
    """Whitelist-only admission; model and transport are intentionally absent."""

    def __init__(self, contacts=None, inbox=None, *, enabled=False):
        self.contacts = contacts or ContactPairingStore()
        self.inbox = inbox or ChatInboxStore()
        self.enabled = enabled

    def close(self):
        self.contacts.close()
        self.inbox.close()

    @staticmethod
    def _supported(event):
        text = getattr(event, "text", "")
        return (
            getattr(event, "conversation_type", None) == "one_to_one"
            and not getattr(event, "is_forwarded", False)
            and isinstance(getattr(event, "sender_fingerprint", None), str)
            and bool(event.sender_fingerprint.strip())
            and isinstance(getattr(event, "bot_id", None), str)
            and bool(event.bot_id.strip())
            and isinstance(text, str)
            and bool(text.strip())
        )

    def pair_candidate(self, event, *, observed_at=None):
        if not self._supported(event):
            return False
        self.contacts.observe_candidate(
            event.account_scope,
            event.sender_fingerprint,
            event.bot_id,
            event.message_id,
            observed_at=observed_at,
            preview_text=event.text,
        )
        return True

    def route(self, event, *, now=None):
        if not self.enabled:
            return RouterDecision(CHAT_AGENT_DISABLED)
        if not self._supported(event):
            return RouterDecision(IGNORED_UNSUPPORTED)
        contact = self.contacts.get(
            event.account_scope, event.sender_fingerprint, event.bot_id,
        )
        if not contact or not self.contacts.is_allowed(
            event.account_scope, event.sender_fingerprint, event.bot_id,
        ):
            return RouterDecision(IGNORED_CONTACT)
        claim = self.inbox.claim(event.account_scope, event, now=now)
        if claim.duplicate:
            return RouterDecision(DUPLICATE_IGNORED, contact.alias, contact.mode, claim.key)
        return RouterDecision(MATCHED_CONTACT, contact.alias, contact.mode, claim.key)
