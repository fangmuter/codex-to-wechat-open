from dataclasses import replace
from datetime import datetime, timezone
from .models import InboundEvent, Batch
from .context_utils import as_datetime
from .storage import ProbeStorage
from .whitelist import Whitelist

WECHAT_UNAVAILABLE='WECHAT_UNAVAILABLE'

class Receiver:
    def __init__(self,transport,storage=None,allowed_sender_id=None,allowed_bot_id=None,retry_limit=3,context_store=None,recipient_store=None,clock=None):
        self.transport=transport; self.storage=storage or ProbeStorage(); self.whitelist=Whitelist(allowed_sender_id,allowed_bot_id); self.retry_limit=retry_limit; self.context_store=context_store; self.recipient_store=recipient_store; self.clock=clock or (lambda: datetime.now(timezone.utc)); self.state='IDLE'; self.outbound_business_calls=0; self.last_events=(); self.last_recipient_binding=None
    def _ingestion_timestamp(self):
        value=self.clock()
        if isinstance(value,datetime):
            timestamp=value
        elif isinstance(value,(int,float)) and not isinstance(value,bool):
            timestamp=datetime.fromtimestamp(float(value),timezone.utc)
        elif isinstance(value,str):
            text=value.strip()
            if text.endswith('Z'): text=text[:-1]+'+00:00'
            timestamp=datetime.fromisoformat(text)
        else:
            raise ValueError('ingestion clock must return datetime, timestamp, or ISO string')
        if timestamp.tzinfo is None: timestamp=timestamp.replace(tzinfo=timezone.utc)
        else: timestamp=timestamp.astimezone(timezone.utc)
        return timestamp.isoformat()
    def login_mock(self): return self.transport.login_qr(), self.transport.qr_status()
    def disable(self): self.storage.set_setting('wechat_gateway_enabled','false'); self.state='EMERGENCY_DISABLED'
    def enable(self): self.storage.set_setting('wechat_gateway_enabled','true'); self.state='IDLE'
    def receive_once(self):
        if self.storage.setting('wechat_gateway_enabled','true')=='false': self.state='EMERGENCY_DISABLED'; return []
        cursor=self.storage.cursor(); attempts=0
        while attempts<=self.retry_limit:
            try:
                batch=self.transport.getupdates(cursor)
                self.state='IDLE'
                break
            except TimeoutError:
                attempts+=1
                if attempts>self.retry_limit: self.state=WECHAT_UNAVAILABLE; return []
            except ConnectionError:
                attempts+=1
                if attempts>self.retry_limit: self.state=WECHAT_UNAVAILABLE; return []
        accepted=[]
        timestamped_events=[]
        for raw_event in batch.messages:
            # The protocol's remote payload may omit a message timestamp.  A
            # local, timezone-aware ingestion timestamp is authoritative for
            # proving that this event arrived after a runner started.  Use it
            # only when the wire value is absent/invalid: a valid upstream
            # timestamp retains its protocol meaning.  It is written once at
            # the first persistence boundary; legacy rows are never backfilled.
            event = raw_event
            if as_datetime(raw_event.received_at) is None:
                event = replace(raw_event, received_at=self._ingestion_timestamp())
            timestamped_events.append(event)
            if not self.storage.save_event(event,'accepted' if self.whitelist.check(event) else 'rejected'): continue
            if self.whitelist.check(event):
                accepted.append(event)
                if self.recipient_store and self.last_recipient_binding is None:
                    self.last_recipient_binding=self.recipient_store.bind(event)
                if self.context_store and event.context_token:
                    context_ref=self.context_store.store(event)
                    self.storage.set_context_ref(event.account_scope,event.message_id,context_ref)
        self.last_events=tuple(timestamped_events)
        self.storage.advance(batch.cursor)
        return accepted
