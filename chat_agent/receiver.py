"""Chat Agent-specific inbound receiver with candidate-only admission."""

from dataclasses import replace

from ilink_receive_probe.probe.reply_context import ContextMissing


WECHAT_UNAVAILABLE = "WECHAT_UNAVAILABLE"
CHAT_AGENT_PROCESSED = "chat_agent_processed"


class ChatAgentReceiver:
    """Poll iLink without reusing Daily Plan's single-contact whitelist.

    Transport polling and cursor advancement stay here; contact authorization
    remains in ``ChatAgentRouter``. Unknown one-to-one text events are exposed
    to the adapter only for a manual pairing candidate window.
    """

    def __init__(
        self,
        transport,
        storage,
        *,
        context_store=None,
        account_scope="chat-agent",
        retry_limit=3,
        defer_cursor=False,
    ):
        if not isinstance(account_scope, str) or not account_scope.strip():
            raise ValueError("account_scope is required")
        if not isinstance(retry_limit, int) or retry_limit < 0:
            raise ValueError("retry_limit must be a non-negative integer")
        self.transport = transport
        self.storage = storage
        self.context_store = context_store
        self.account_scope = account_scope
        self.retry_limit = retry_limit
        self.defer_cursor = bool(defer_cursor)
        self.state = "IDLE"
        self.last_events = ()
        # Redacted, per-event rejection codes make receive-only diagnostics
        # useful without retaining or printing message text or identifiers.
        self.last_rejection_reasons = ()
        self._pending_cursor = None

    @staticmethod
    def _support_reason(event):
        if getattr(event, "conversation_type", None) != "one_to_one":
            return "CONVERSATION_TYPE"
        if getattr(event, "is_forwarded", False):
            return "FORWARDED"
        sender = getattr(event, "sender_fingerprint", None)
        if not isinstance(sender, str) or not sender.strip():
            return "MISSING_SENDER"
        bot = getattr(event, "bot_id", None)
        if not isinstance(bot, str) or not bot.strip():
            return "MISSING_BOT"
        text = getattr(event, "text", None)
        if not isinstance(text, str) or not text.strip():
            return "EMPTY_TEXT"
        return None

    @classmethod
    def _supported(cls, event):
        return cls._support_reason(event) is None

    def receive_once(self):
        if self.defer_cursor and self._pending_cursor is not None:
            raise RuntimeError("pending Chat Agent batch must be acknowledged")
        cursor = self.storage.cursor()
        attempts = 0
        while attempts <= self.retry_limit:
            try:
                batch = self.transport.getupdates(cursor)
                self.state = "IDLE"
                break
            except (TimeoutError, ConnectionError, OSError):
                attempts += 1
                if attempts > self.retry_limit:
                    self.state = WECHAT_UNAVAILABLE
                    self.last_events = ()
                    self.last_rejection_reasons = ()
                    return []

        accepted = []
        normalized = []
        rejection_reasons = []
        for event in batch.messages:
            if event.account_scope != self.account_scope:
                event = replace(event, account_scope=self.account_scope)
            normalized.append(event)
            reason = self._support_reason(event)
            supported = reason is None
            if reason is not None:
                rejection_reasons.append(reason)
            self.storage.save_event(event, "candidate" if supported else "rejected")
            status = self.storage.event_status(event.account_scope, event.message_id)
            if not supported or status == CHAT_AGENT_PROCESSED:
                continue
            if supported and self.context_store and event.context_token and event.reply_to_user_id:
                try:
                    row = self.storage.inbound(event.account_scope, event.message_id)
                    existing_ref = row["context_ref"] if row else None
                    if not existing_ref:
                        context_ref = self.context_store.store(event)
                        self.storage.set_context_ref(event.account_scope, event.message_id, context_ref)
                except (ContextMissing, OSError, ValueError):
                    # The message can still be paired and reviewed, but it
                    # must become NEEDS_USER_INPUT rather than being sent.
                    pass
            if supported:
                accepted.append(event)
        self.last_events = tuple(normalized)
        self.last_rejection_reasons = tuple(rejection_reasons)
        if self.defer_cursor:
            self._pending_cursor = batch.cursor
        else:
            self.storage.advance(batch.cursor)
        return accepted

    def acknowledge(self, events=()):
        """Mark processed Chat Agent events and commit a deferred cursor."""
        if not self.defer_cursor:
            return
        for event in events:
            self.storage.set_event_status(
                event.account_scope,
                event.message_id,
                CHAT_AGENT_PROCESSED,
            )
        if self._pending_cursor is not None:
            self.storage.advance(self._pending_cursor)
            self._pending_cursor = None
