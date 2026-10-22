import hashlib
import json
import time
from pathlib import Path
from .credential_store import CredentialStore

class ContextExpired(RuntimeError): pass
class ContextMissing(RuntimeError): pass

class ReplyContextStore:
    """Short-lived, DPAPI-protected routing/context records outside SQLite."""
    def __init__(self, state_dir=None, ttl_seconds=300, now=time.time, protect=None, unprotect=None):
        base=Path(state_dir) if state_dir else CredentialStore().state_dir
        self.directory=base/'reply-context'; self.ttl_seconds=ttl_seconds; self.now=now
        credential=CredentialStore(base)
        self._protect=protect or credential._protect
        self._unprotect=unprotect or (lambda data: credential._protect(data,True))
    def _ref(self,event): return hashlib.sha256(f'{event.account_scope}\0{event.message_id}'.encode()).hexdigest()
    def _paths(self,ref): return self.directory/f'{ref}.dat', self.directory/f'{ref}.meta.json'
    def store(self,event):
        if not event.context_token or not event.reply_to_user_id: raise ContextMissing('accepted inbound has no reply context')
        ref=self._ref(event); created=self.now(); expires=created+self.ttl_seconds
        payload={'account_scope':event.account_scope,'message_id':event.message_id,'recipient':event.reply_to_user_id,'context_token':event.context_token,'sender_fingerprint':event.sender_fingerprint,'bot_fingerprint':event.bot_id,'conversation_type':event.conversation_type,'expires_at':expires}
        metadata={'created_at':created,'expires_at':expires,'used':False,'sender_fingerprint':event.sender_fingerprint,'bot_fingerprint':event.bot_id}
        data_path,meta_path=self._paths(ref); self.directory.mkdir(parents=True,exist_ok=True)
        data_path.write_bytes(self._protect(json.dumps(payload,separators=(',',':')).encode()))
        meta_path.write_text(json.dumps(metadata,separators=(',',':')),encoding='utf-8')
        return ref
    def load(self,ref):
        data_path,meta_path=self._paths(ref)
        if not data_path.exists() or not meta_path.exists(): raise ContextMissing('reply context is unavailable')
        metadata=json.loads(meta_path.read_text(encoding='utf-8'))
        if metadata['used'] or self.now()>=metadata['expires_at']:
            self.consume(ref)
            raise ContextExpired('reply context expired or consumed')
        return json.loads(self._unprotect(data_path.read_bytes()).decode())
    def consume(self,ref):
        for path in self._paths(ref):
            try: path.unlink()
            except FileNotFoundError: pass
