import base64
import json
import secrets
from dataclasses import dataclass
from urllib.request import Request, urlopen
from .send_result import (
    DEFINITE_FAILED,
    DELIVERY_UNCONFIRMED,
    INVALID_RESPONSE,
    classify_send_response,
)

FIXED_REPLY_TEXT='Phase 1.3 回复测试成功'

@dataclass(frozen=True)
class RenderedReply:
    text: str

class DefiniteSendError(RuntimeError): pass
class AmbiguousSendError(RuntimeError): pass
class UnconfirmedSendError(RuntimeError): pass

class ReplyOnlyTransport:
    """The sole Phase 1.3 network capability: a context-bound fixed-text reply."""
    ALLOWED=('sendmessage',)
    BASE='https://ilinkai.weixin.qq.com'
    def __init__(self, token, base_url=None, timeout=15_000, channel_version='1.0.0', require_receipt=False):
        self.token=token; self.base_url=(base_url or self.BASE).rstrip('/'); self.timeout=timeout/1000; self.channel_version=channel_version; self.require_receipt=require_receipt; self.calls=[]
    def reply_bound_context(self, context, idempotency_key):
        return self.reply_bound_rendered_context(context,idempotency_key,RenderedReply(FIXED_REPLY_TEXT))
    def reply_bound_rendered_context(self, context, idempotency_key, rendered_reply):
        if not isinstance(rendered_reply,RenderedReply): raise TypeError('reply text must come from a renderer')
        body={'msg':{'from_user_id':'','to_user_id':context['recipient'],'client_id':idempotency_key,'message_type':2,'message_state':2,'item_list':[{'type':1,'text_item':{'text':rendered_reply.text}}],'context_token':context['context_token']},'base_info':{'channel_version':self.channel_version,'bot_agent':'ilink_receive_probe/phase13'}}
        uin=base64.b64encode(str(secrets.randbits(32)).encode()).decode()
        request=Request(self.base_url+'/ilink/bot/sendmessage',data=json.dumps(body).encode(),method='POST',headers={'Content-Type':'application/json','AuthorizationType':'ilink_bot_token','Authorization':'Bearer '+self.token,'X-WECHAT-UIN':uin,'iLink-App-Id':'bot','iLink-App-ClientVersion':self.channel_version})
        self.calls.append('sendmessage')
        try:
            with urlopen(request,timeout=self.timeout) as response: result=json.loads(response.read().decode())
        except TimeoutError as error: raise AmbiguousSendError('send result is unknown') from error
        if self.require_receipt:
            classified = classify_send_response(result)
            if classified.state == DELIVERY_UNCONFIRMED:
                raise UnconfirmedSendError('sendmessage accepted without delivery receipt')
            if classified.state in (DEFINITE_FAILED, INVALID_RESPONSE):
                raise DefiniteSendError('sendmessage response was not a confirmed success')
        elif result.get('ret') not in (None,0):
            raise DefiniteSendError('sendmessage rejected')
        return result

class FakeReplyOnlyTransport:
    def __init__(self,outcomes=None, require_receipt=False): self.outcomes=list(outcomes or ['ACKED_API']); self.require_receipt=require_receipt; self.calls=[]
    def reply_bound_context(self, context, idempotency_key):
        self.calls.append(idempotency_key)
        outcome=self.outcomes.pop(0) if self.outcomes else 'ACKED_API'
        if outcome=='AMBIGUOUS': raise AmbiguousSendError('send result is unknown')
        if outcome=='FAILED': raise DefiniteSendError('sendmessage rejected')
        if outcome=='UNCONFIRMED': raise UnconfirmedSendError('sendmessage accepted without delivery receipt')
        return {'ret':0,'message_id':'fake-receipt'} if self.require_receipt else {'ret':0}
    def reply_bound_rendered_context(self, context, idempotency_key, rendered_reply):
        if not isinstance(rendered_reply,RenderedReply): raise TypeError('reply text must come from a renderer')
        self.calls.append((idempotency_key,rendered_reply.text))
        outcome=self.outcomes.pop(0) if self.outcomes else 'ACKED_API'
        if outcome=='AMBIGUOUS': raise AmbiguousSendError('send result is unknown')
        if outcome=='FAILED': raise DefiniteSendError('sendmessage rejected')
        if outcome=='UNCONFIRMED': raise UnconfirmedSendError('sendmessage accepted without delivery receipt')
        return {'ret':0,'message_id':'fake-receipt'} if self.require_receipt else {'ret':0}
