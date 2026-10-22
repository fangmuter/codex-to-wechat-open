import inspect
import tempfile
import unittest
from pathlib import Path

from ilink_receive_probe.probe.models import Batch, InboundEvent
from ilink_receive_probe.probe.receiver import Receiver
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.reply_transport import FakeReplyOnlyTransport, FIXED_REPLY_TEXT
from ilink_receive_probe.probe.replier import ACKED_API, ALREADY_REPLIED, CONTEXT_EXPIRED, FAILED, REJECTED, UNKNOWN, Replier
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.transport import FakeIlinkTransport

SECRET='CONTEXT_TOKEN_MUST_NOT_LEAK'
def encrypt(data): return b'FAKE-DPAPI:'+bytes(value ^ 0xA5 for value in data)
def decrypt(data): return bytes(value ^ 0xA5 for value in data[len(b'FAKE-DPAPI:'):])

class Phase13Tests(unittest.TestCase):
    def make(self,event=None,outcomes=None,ttl=300):
        root=Path(tempfile.mkdtemp()); storage=ProbeStorage(root/'probe.db'); self.addCleanup(storage.close)
        clock=[0.0]; context=ReplyContextStore(root,ttl,lambda:clock[0],encrypt,decrypt)
        event=event or InboundEvent('acct','m1','me','now','ignored','bot','one_to_one',SECRET,'raw-recipient')
        receiver=Receiver(FakeIlinkTransport([Batch((event,),'c1')]),storage,'me','bot',context_store=context)
        receiver.receive_once()
        transport=FakeReplyOnlyTransport(outcomes)
        return storage,context,transport,Replier(storage,context,transport,'me','bot'),event,clock
    def test_allowed_inbound_gets_one_fixed_reply(self):
        _,_,transport,replier,_,_=self.make(); self.assertEqual(replier.reply_to_inbound('acct','m1'),ACKED_API); self.assertEqual(len(transport.calls),1); self.assertEqual(FIXED_REPLY_TEXT,'Phase 1.3 回复测试成功')
    def test_unknown_sender_never_replies(self):
        event=InboundEvent('acct','m1','other','now','x','bot','one_to_one',SECRET,'raw'); _,_,transport,replier,_,_=self.make(event); self.assertEqual(replier.reply_to_inbound('acct','m1'),REJECTED); self.assertEqual(transport.calls,[])
    def test_wrong_bot_never_replies(self):
        event=InboundEvent('acct','m1','me','now','x','other','one_to_one',SECRET,'raw'); _,_,transport,replier,_,_=self.make(event); self.assertEqual(replier.reply_to_inbound('acct','m1'),REJECTED); self.assertEqual(transport.calls,[])
    def test_group_like_event_never_replies(self):
        event=InboundEvent('acct','m1','me','now','x','bot','group',SECRET,'raw'); _,_,transport,replier,_,_=self.make(event); self.assertEqual(replier.reply_to_inbound('acct','m1'),REJECTED); self.assertEqual(transport.calls,[])
    def test_missing_context_never_replies(self):
        event=InboundEvent('acct','m1','me','now','x','bot','one_to_one',None,None); _,_,transport,replier,_,_=self.make(event); self.assertEqual(replier.reply_to_inbound('acct','m1'),REJECTED); self.assertEqual(transport.calls,[])
    def test_expired_context_is_removed(self):
        _,context,transport,replier,event,clock=self.make(ttl=1); clock[0]=2; self.assertEqual(replier.reply_to_inbound('acct','m1'),CONTEXT_EXPIRED); self.assertEqual(transport.calls,[]); self.assertFalse(any(context.directory.iterdir()))
    def test_duplicate_call_only_sends_once(self):
        _,_,transport,replier,_,_=self.make(); self.assertEqual(replier.reply_to_inbound('acct','m1'),ACKED_API); self.assertEqual(replier.reply_to_inbound('acct','m1'),ALREADY_REPLIED); self.assertEqual(len(transport.calls),1)
    def test_definite_failure_has_bounded_retry(self):
        storage,_,transport,replier,_,_=self.make(outcomes=['FAILED','FAILED','FAILED']); self.assertEqual(replier.reply_to_inbound('acct','m1'),FAILED); self.assertEqual(len(transport.calls),3); self.assertEqual(storage.db.execute('select status from replies').fetchone()['status'],FAILED)
    def test_ambiguous_send_never_blind_retries(self):
        _,_,transport,replier,_,_=self.make(outcomes=['AMBIGUOUS','ACKED_API']); self.assertEqual(replier.reply_to_inbound('acct','m1'),UNKNOWN); self.assertEqual(replier.reply_to_inbound('acct','m1'),UNKNOWN); self.assertEqual(len(transport.calls),1)
    def test_emergency_disable_prevents_reply(self):
        storage,_,transport,replier,_,_=self.make(); storage.set_setting('wechat_gateway_enabled','false'); self.assertEqual(replier.reply_to_inbound('acct','m1'),REJECTED); self.assertEqual(transport.calls,[])
    def test_no_arbitrary_recipient_or_text_api(self):
        self.assertEqual(tuple(inspect.signature(Replier.reply_to_inbound).parameters),('self','account_scope','inbound_message_id'))
    def test_context_secret_is_not_plaintext_or_in_sqlite(self):
        storage,context,_,_,_,_=self.make(); encrypted=next(context.directory.glob('*.dat')).read_bytes(); self.assertNotIn(SECRET.encode(),encrypted); dump=' '.join(str(tuple(row)) for row in storage.db.execute('select * from inbox')); self.assertNotIn(SECRET,dump); self.assertNotIn(SECRET,repr(context))
    def test_consumed_context_is_deleted(self):
        _,context,_,replier,_,_=self.make(); self.assertEqual(replier.reply_to_inbound('acct','m1'),ACKED_API); self.assertFalse(any(context.directory.iterdir()))
    def test_no_typing_media_codex_shell_or_listener_surface(self):
        from ilink_receive_probe.probe.reply_transport import ReplyOnlyTransport
        forbidden=('typing','upload','download','media','codex','shell','listen','serve','exec')
        self.assertFalse(any(any(word in name.lower() for word in forbidden) for name in dir(ReplyOnlyTransport)))

if __name__=='__main__': unittest.main()
