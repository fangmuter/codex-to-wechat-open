import json, sys, tempfile, unittest
from urllib.parse import urlsplit
from ilink_receive_probe.probe.models import InboundEvent, Batch
from ilink_receive_probe.probe.transport import FakeIlinkTransport
from ilink_receive_probe.probe.receiver import Receiver, WECHAT_UNAVAILABLE
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.credential_store import CredentialStore
from ilink_receive_probe.probe.real_transport import RealIlinkTransport
from ilink_receive_probe.probe.protocol import parse_batch
from ilink_receive_probe.probe.recipient_binding import RecipientBindingCollision, RecipientBindingStore

def ev(mid='m1',sender='me',bot='bot',recipient='me',forwarded=False): return InboundEvent('acct',mid,sender,'2026-09-03T22:00:00+08:00','ping',bot,'one_to_one',None,recipient,forwarded)
def fake_protect(value): return b'F'+value[::-1]
def fake_unprotect(value):
    if not value.startswith(b'F'): raise RuntimeError('bad test blob')
    return value[1:][::-1]
class ProbeTests(unittest.TestCase):
    def dpapi_store(self, secret):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = CredentialStore(temporary.name)
        try:
            store.save(secret)
        except RuntimeError as error:
            # Some isolated Windows runners do not expose a usable Current
            # User DPAPI profile. Keep these integration checks visible as
            # skipped instead of reporting a product assertion failure.
            if 'DPAPI operation failed: WinError 2' in str(error):
                self.skipTest('Current User DPAPI is unavailable in this runner')
            raise
        return store

    def make(self,batches=None,errors=None):
        path=tempfile.mktemp('.db'); st=ProbeStorage(path); self.addCleanup(st.close); tr=FakeIlinkTransport(batches,errors); return Receiver(tr,st,'me','bot'),tr,st
    def test_login_qr_mock(self):
        r,_,_=self.make(); self.assertEqual(r.login_mock()[0]['state'],'qr_ready')
    def test_qr_status_mock(self):
        r,_,_=self.make(); self.assertEqual(r.login_mock()[1]['status'],'confirmed')
    def test_no_real_network_transport(self):
        r,t,_=self.make(); r.login_mock(); self.assertNotIn('http',type(t).__name__.lower())
    def test_allowed_sender(self):
        r,_,_=self.make([Batch((ev(),),'c1')]); self.assertEqual(len(r.receive_once()),1)
    def test_unknown_reject(self):
        r,_,st=self.make([Batch((ev(sender='x'),),'c1')]); self.assertEqual(r.receive_once(),[]); self.assertEqual(st.db.execute("select status from inbox").fetchone()[0],'rejected')
    def test_wrong_bot_reject(self):
        r,_,_=self.make([Batch((ev(bot='other'),),'c1')]); self.assertEqual(r.receive_once(),[])
    def test_duplicate_message_id(self):
        r,_,st=self.make([Batch((ev(),),'c1'),Batch((ev(),),'c1')]); r.receive_once(); r.receive_once(); self.assertEqual(st.db.execute('select count(*) from inbox').fetchone()[0],1)
    def test_cursor_after_durable_write(self):
        r,_,st=self.make([Batch((ev(),),'c1')]); r.receive_once(); self.assertEqual(st.cursor(),'c1')
    def test_restart_after_checkpoint(self):
        r,_,st=self.make([Batch((ev(),),'c1')]); r.receive_once(); path=st.db.execute("pragma database_list").fetchone()[2]; st.close(); st2=ProbeStorage(path); self.addCleanup(st2.close); self.assertEqual(st2.cursor(),'c1')
    def test_restart_before_checkpoint(self):
        r,_,st=self.make([Batch((ev(),),'c1')]); r.receive_once(); self.assertEqual(st.cursor(),'c1')
    def test_empty_batch(self):
        r,_,_=self.make([Batch((),'c0')]); self.assertEqual(r.receive_once(),[])
    def test_bounded_error(self):
        r,t,_=self.make(errors=[ConnectionError(),ConnectionError(),ConnectionError(),ConnectionError()]); self.assertEqual(r.receive_once(),[]); self.assertEqual(len(t.calls),4)
    def test_timeout_state(self):
        r,_,_=self.make(errors=[TimeoutError()]*4); self.assertEqual(r.receive_once(),[]); self.assertEqual(r.state,WECHAT_UNAVAILABLE)
    def test_state_returns_to_idle_after_recovery(self):
        r,_,_=self.make([Batch((),'recovered')], errors=[TimeoutError()]*4)
        r.receive_once()
        self.assertEqual(r.state, WECHAT_UNAVAILABLE)
        self.assertEqual(r.receive_once(), [])
        self.assertEqual(r.state, 'IDLE')
    def test_emergency_disable(self):
        r,t,_=self.make([Batch((ev(),),'c1')]); r.disable(); self.assertEqual(r.receive_once(),[]); self.assertEqual(t.calls,[])
    def test_secret_repr_redacted(self): self.assertNotIn('FAKE_SECRET',repr(CredentialStore()))
    def test_secret_not_business_db(self):
        r,_,st=self.make(); r.receive_once(); self.assertNotIn('credential', [x['name'] for x in st.db.execute("select name from sqlite_master where type='table'")])
    def test_credential_outside_repo(self): self.assertNotIn('New project',CredentialStore().describe_location()['directory'])
    def test_no_business_outbound_api(self):
        r,_,_=self.make(); self.assertEqual(r.outbound_business_calls,0); self.assertFalse(hasattr(r.transport,'send'))
    def test_no_media_or_command_surface(self): self.assertFalse(any(hasattr(FakeIlinkTransport(),x) for x in ['send','typing','download','upload','execute']))
    def test_no_codex_surface(self): self.assertFalse(any('codex' in x.lower() for x in dir(FakeIlinkTransport())))
    def test_no_listener(self): self.assertFalse(any(x in dir(FakeIlinkTransport()) for x in ['listen','serve_forever']))
    def test_event_contract(self): self.assertEqual(set(InboundEvent.__dataclass_fields__),{'account_scope','message_id','sender_fingerprint','received_at','text','bot_id','conversation_type','context_token','reply_to_user_id','is_forwarded'})
    def test_protocol_reads_nested_text_item(self): self.assertEqual(parse_batch({'msgs':[{'message_id':1,'from_user_id':'me','to_user_id':'bot','item_list':[{'text_item':{'text':'开始明日计划'}}]}]}) .messages[0].text,'开始明日计划')
    def test_protocol_reads_standard_text_item_metadata(self):
        event = parse_batch({'msgs': [{
            'message_id': 1, 'from_user_id': 'me', 'to_user_id': 'bot',
            'item_list': [{'type': 1, 'create_time_ms': 1,
                           'is_completed': True,
                           'ref_msg': {'title': 'quoted'},
                           'server_metadata': {'sequence': 1},
                           'text_item': {'text': '标准文本'}}],
        }]}).messages[0]
        self.assertEqual(event.text, '标准文本')
    def test_protocol_rejects_media_and_mixed_payloads(self):
        media=parse_batch({'msgs':[{'message_id':2,'from_user_id':'me','to_user_id':'bot','item_list':[{'image_item':{'url':'x'}}]}]}).messages[0]
        mixed=parse_batch({'msgs':[{'message_id':3,'from_user_id':'me','to_user_id':'bot','item_list':[{'text_item':{'text':'看图'}},{'image_item':{'url':'x'}}]}]}).messages[0]
        self.assertEqual(media.text,'')
        self.assertEqual(mixed.text,'')
    def test_protocol_drops_messages_without_stable_message_id(self):
        batch = parse_batch({'msgs':[{'from_user_id':'me','to_user_id':'bot','text':'no id'}]})
        self.assertEqual(batch.messages, ())

    def test_protocol_does_not_hash_missing_identity(self):
        event = parse_batch({'msgs':[{'message_id':'m1','text':'hello'}]}).messages[0]
        self.assertEqual(event.sender_fingerprint, '')
        self.assertEqual(event.bot_id, '')
    @unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI is required')
    def test_dpapi_roundtrip(self):
        c=self.dpapi_store('FAKE_SECRET'); self.assertEqual(c.load(),'FAKE_SECRET'); self.assertNotEqual(c.path.read_bytes(),b'FAKE_SECRET')
    @unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI is required')
    def test_dpapi_delete(self):
        c=self.dpapi_store('x'); c.delete(); self.assertFalse(c.path.exists())
    @unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI is required')
    def test_dpapi_corrupt_fails(self):
        c=self.dpapi_store('x'); c.path.write_bytes(b'bad'); self.assertRaises(RuntimeError,c.load)
    @unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI is required')
    def test_dpapi_metadata_no_secret(self):
        c=self.dpapi_store('FAKE_SECRET'); self.assertNotIn('FAKE_SECRET',c.meta.read_text())
    def test_real_transport_endpoint_allowlist(self): self.assertEqual(set(RealIlinkTransport.ALLOWED),{'get_bot_qrcode','get_qrcode_status','getupdates','notify_start'})
    def test_real_transport_no_business_method(self): self.assertFalse(any(x in dir(RealIlinkTransport) for x in ['send','reply','typing','upload']))
    def test_real_transport_request_deferred(self): self.assertEqual(RealIlinkTransport(token='fake').calls,[])

    def test_real_transport_account_scope_is_explicit_and_default_compatible(self):
        self.assertEqual(RealIlinkTransport(token='fake').account_scope, 'default')
        self.assertEqual(
            RealIlinkTransport(token='fake', account_scope='chat-agent').account_scope,
            'chat-agent',
        )
        with self.assertRaises(ValueError):
            RealIlinkTransport(token='fake', account_scope='')

    def test_real_transport_getupdates_parses_scoped_plain_text_with_injected_opener(self):
        calls = []

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self):
                return json.dumps({
                    'msgs': [{
                        'message_id': 'm1', 'from_user_id': 'sender',
                        'to_user_id': 'bot', 'conversation_type': 'one_to_one',
                        'item_list': [{'text_item': {'text': '你好'}}],
                    }],
                    'get_updates_buf': 'cursor-1',
                }).encode()

        def opener(request, timeout):
            calls.append((request, timeout))
            return Response()

        transport = RealIlinkTransport(
            token='fake', base_url='http://example.test', account_scope='chat-agent',
            opener=opener,
        )
        batch = transport.getupdates('cursor-0')
        self.assertEqual(batch.cursor, 'cursor-1')
        self.assertEqual(batch.messages[0].account_scope, 'chat-agent')
        self.assertEqual(batch.messages[0].text, '你好')
        self.assertEqual(urlsplit(calls[0][0].full_url).path, '/ilink/bot/getupdates')
        request_body = json.loads(calls[0][0].data.decode())
        self.assertEqual(request_body['get_updates_buf'], 'cursor-0')
        self.assertEqual(request_body['base_info']['channel_version'], '2.4.6')
        self.assertEqual(calls[0][0].get_header('Ilink-app-clientversion'), str((2 << 16) | (4 << 8) | 6))
        self.assertEqual(calls[0][1], 45.0)

    def test_real_transport_notify_start_has_no_message_payload(self):
        calls = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"ret":0}'

        def opener(request, timeout):
            calls.append((request, timeout))
            return Response()

        transport = RealIlinkTransport(token='fake', base_url='http://example.test', opener=opener)
        response = transport.notify_start()
        self.assertEqual(response['ret'], 0)
        self.assertEqual(urlsplit(calls[0][0].full_url).path, '/ilink/bot/msg/notifystart')
        body = json.loads(calls[0][0].data.decode())
        self.assertEqual(set(body), {'base_info'})
        self.assertEqual(calls[0][1], 45.0)
    def test_credential_location_repo_external(self): self.assertNotIn('New project',CredentialStore().describe_location()['directory'])
    def test_disabled_does_not_advance_cursor(self):
        r,_,st=self.make([Batch((ev(),),'c1')]); r.disable(); r.receive_once(); self.assertEqual(st.cursor(),'')
    def test_recipient_binding_dpapi_contract_and_no_sqlite_raw_id(self):
        state=tempfile.mkdtemp(); recipient='me'; sender=RecipientBindingStore._fingerprint(recipient)
        binding=RecipientBindingStore(state,fake_protect,fake_unprotect); st=ProbeStorage(tempfile.mktemp('.db')); self.addCleanup(st.close)
        r=Receiver(FakeIlinkTransport([Batch((ev(sender=sender,recipient=recipient),),'c1')]),st,sender,'bot',recipient_store=binding)
        self.assertEqual(len(r.receive_once()),1); self.assertEqual(r.last_recipient_binding,'BOUND')
        self.assertNotIn(recipient,binding.path.read_bytes().decode('latin1')); self.assertNotIn(recipient,' '.join(str(x) for x in st.db.execute('SELECT * FROM inbox').fetchone()))
        self.assertEqual(json.loads(binding.meta.read_text())['recipient_fingerprint'],sender)
    def test_recipient_binding_reuses_only_same_raw_recipient_and_revokes(self):
        state=tempfile.mkdtemp(); recipient='me'; sender=RecipientBindingStore._fingerprint(recipient); binding=RecipientBindingStore(state,fake_protect,fake_unprotect)
        event=ev(sender=sender,recipient=recipient); self.assertEqual(binding.bind(event),'BOUND'); self.assertEqual(binding.bind(event),'REUSED')
        with self.assertRaises(RecipientBindingCollision): binding.bind(ev(sender=RecipientBindingStore._fingerprint('other'),recipient='other'))
        binding.delete(); self.assertFalse(binding.path.exists())
    def test_recipient_binding_rejects_forwarded_or_fingerprint_mismatch(self):
        binding=RecipientBindingStore(tempfile.mkdtemp(),fake_protect,fake_unprotect)
        with self.assertRaises(RuntimeError): binding.bind(ev(sender='wrong',recipient='me'))
        with self.assertRaises(RuntimeError): binding.bind(ev(sender=RecipientBindingStore._fingerprint('me'),recipient='me',forwarded=True))
    def test_forwarded_event_is_not_allowlisted(self):
        r,_,st=self.make([Batch((ev(forwarded=True),),'c1')]); self.assertEqual(r.receive_once(),[]); self.assertEqual(st.db.execute("select status from inbox").fetchone()[0],'rejected')

if __name__=='__main__': unittest.main()
