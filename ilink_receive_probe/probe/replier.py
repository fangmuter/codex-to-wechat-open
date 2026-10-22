import hashlib
from .reply_context import ContextExpired, ContextMissing
from .reply_transport import AmbiguousSendError, DefiniteSendError, UnconfirmedSendError

ALREADY_REPLIED='ALREADY_REPLIED'
REJECTED='REJECTED'
UNKNOWN='UNKNOWN'
CONTEXT_EXPIRED='CONTEXT_EXPIRED'
ACKED_API='ACKED_API'
DELIVERY_UNCONFIRMED='DELIVERY_UNCONFIRMED'
FAILED='FAILED'

class Replier:
    """Replies only to a durable, accepted inbound; recipient/text are never parameters."""
    def __init__(self, storage, context_store, transport, allowed_sender, allowed_bot, retry_limit=2):
        self.storage=storage; self.context_store=context_store; self.transport=transport; self.allowed_sender=allowed_sender; self.allowed_bot=allowed_bot; self.retry_limit=retry_limit
    def _key(self, account_scope, message_id): return hashlib.sha256(f'{account_scope}\0{message_id}\0phase13-fixed-reply'.encode()).hexdigest()
    def reply_to_inbound(self, account_scope, inbound_message_id):
        if self.storage.setting('wechat_gateway_enabled','true')!='true': return REJECTED
        inbound=self.storage.inbound(account_scope,inbound_message_id)
        if not inbound or inbound['status']!='accepted' or inbound['sender_fingerprint']!=self.allowed_sender or inbound['bot_fingerprint']!=self.allowed_bot or inbound['conversation_type']!='one_to_one' or not inbound['context_ref']: return REJECTED
        key=self._key(account_scope,inbound_message_id); existing=self.storage.reply(key)
        if existing and existing['status'] in (ACKED_API,UNKNOWN,DELIVERY_UNCONFIRMED,CONTEXT_EXPIRED): return ALREADY_REPLIED if existing['status']==ACKED_API else existing['status']
        self.storage.create_reply(key,account_scope,inbound_message_id)
        try: context=self.context_store.load(inbound['context_ref'])
        except ContextExpired:
            self.storage.update_reply(key,CONTEXT_EXPIRED); return CONTEXT_EXPIRED
        except ContextMissing: return REJECTED
        for _ in range(self.retry_limit+1):
            self.storage.update_reply(key,'SENDING')
            try:
                self.transport.reply_bound_context(context,key)
            except AmbiguousSendError:
                self.storage.update_reply(key,UNKNOWN,'ambiguous'); return UNKNOWN
            except UnconfirmedSendError:
                self.storage.update_reply(key,DELIVERY_UNCONFIRMED,'delivery receipt missing'); return DELIVERY_UNCONFIRMED
            except DefiniteSendError:
                continue
            self.storage.update_reply(key,ACKED_API)
            self.context_store.consume(inbound['context_ref'])
            return ACKED_API
        self.storage.update_reply(key,FAILED,'definite failure')
        return FAILED
