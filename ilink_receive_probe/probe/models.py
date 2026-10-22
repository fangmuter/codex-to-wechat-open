from dataclasses import dataclass, field

@dataclass(frozen=True)
class InboundEvent:
    account_scope: str
    message_id: str
    sender_fingerprint: str
    received_at: str
    text: str
    bot_id: str
    conversation_type: str = 'one_to_one'
    context_token: str | None = field(default=None, repr=False)
    reply_to_user_id: str | None = field(default=None, repr=False)
    is_forwarded: bool = False

@dataclass(frozen=True)
class Batch:
    messages: tuple[InboundEvent, ...]
    cursor: str
    error: str | None = None
