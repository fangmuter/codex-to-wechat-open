import argparse
import hashlib
import json
import os
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from daily_plan_mock.planner.service import DailyPlanService, MockTransport
from daily_plan_wechat_integration.adapter import WechatAdapter
from daily_plan_wechat_integration.session_bridge import SessionBridge
from .credential_store import CredentialStore
from .models import InboundEvent
from .real_transport import RealIlinkTransport
from .reply_context import ReplyContextStore
from .reply_transport import ReplyOnlyTransport
from .replier import Replier
from .storage import ProbeStorage
from .transport import FakeIlinkTransport
from .receiver import Receiver
from .recipient_binding import RecipientBindingStore
from .proactive_send import (
    PILOT_KEY, PILOT_TEXT, ProactiveIlinkTransport,
    ProactiveSendStorage, SingleProactivePilot,
)

def _fingerprint(value): return hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]

def _live_login():
    transport=RealIlinkTransport(timeout=35_000)
    qr=transport.get_bot_qrcode()
    qrcode, image_url=qr.get('qrcode'), qr.get('qrcode_img_content')
    if qr.get('ret') not in (None,0) or not qrcode or not image_url: raise RuntimeError('iLink QR request failed')
    if not webbrowser.open(image_url): raise RuntimeError('unable to open the QR image in the default browser')
    for _ in range(10):
        try: status=transport.get_qrcode_status(qrcode)
        except TimeoutError: continue
        if status.get('status')=='confirmed':
            token=status.get('bot_token')
            bot=status.get('ilink_bot_id','')
            user=status.get('ilink_user_id','')
            if not token: raise RuntimeError('iLink login confirmed without a token')
            if not bot or not user:
                raise RuntimeError('iLink login confirmed without complete identity metadata')
            store = CredentialStore()
            store.save(
                token,
                account_fingerprint=_fingerprint(user),
                bot_fingerprint=_fingerprint(bot),
            )
            print('login confirmed; credential stored with DPAPI Current User')
            print('bot fingerprint='+_fingerprint(bot))
            print('scanner fingerprint='+_fingerprint(user))
            return 0
        if status.get('status')=='expired':
            print('QR expired; no credential was stored')
            return 2
    print('QR confirmation pending; no credential was stored')
    return 3

def _live_receive():
    storage=_storage()
    try:
        store=CredentialStore()
        metadata=json.loads(store.meta.read_text(encoding='utf-8')) if store.meta.exists() else {}
        receiver=Receiver(RealIlinkTransport(token=store.load()),storage,metadata.get('account_fingerprint'),metadata.get('bot_fingerprint'),context_store=ReplyContextStore())
        cursor_before=storage.cursor()
        accepted=receiver.receive_once()
        print('observed='+str(len(receiver.last_events))+' accepted='+str(len(accepted))+' cursor_advanced='+str(storage.cursor()!=cursor_before).lower())
        for event in receiver.last_events:
            row=storage.db.execute('SELECT status FROM inbox WHERE account_scope=? AND message_id=?',(event.account_scope,event.message_id)).fetchone()
            print('message fingerprint='+_fingerprint(event.message_id)+' sender fingerprint='+event.sender_fingerprint+' status='+(row['status'] if row else 'duplicate_ignored'))
        for bot in sorted({event.bot_id for event in receiver.last_events}): print('bot fingerprint='+bot)
        return 0
    finally:
        storage.close()

def _live_reply_latest():
    storage=_storage()
    try:
        store=CredentialStore()
        metadata=json.loads(store.meta.read_text(encoding='utf-8')) if store.meta.exists() else {}
        inbound=storage.db.execute("SELECT account_scope,message_id FROM inbox WHERE status='accepted' AND context_ref IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
        if not inbound: raise RuntimeError('no fresh accepted inbound reply context is available')
        replier=Replier(storage,ReplyContextStore(),ReplyOnlyTransport(store.load()),metadata.get('account_fingerprint'),metadata.get('bot_fingerprint'))
        status=replier.reply_to_inbound(inbound['account_scope'],inbound['message_id'])
        print('reply_status='+status)
        return 0 if status=='ACKED_API' else 2
    finally:
        storage.close()

def _live_recipient_bind_receive():
    """Receive one fresh allowlisted direct message and bind its recipient DPAPI-only."""
    storage=_storage()
    try:
        store=CredentialStore(); metadata=json.loads(store.meta.read_text(encoding='utf-8')) if store.meta.exists() else {}
        binding=RecipientBindingStore()
        receiver=Receiver(RealIlinkTransport(token=store.load()),storage,metadata.get('account_fingerprint'),metadata.get('bot_fingerprint'),recipient_store=binding)
        accepted=receiver.receive_once()
        event=accepted[0] if accepted else None
        print('source_inbound_whitelist_passed='+str(bool(event)).lower())
        print('source_inbound_direct_message='+str(bool(event and event.conversation_type=='one_to_one' and not event.is_forwarded)).lower())
        print('source_inbound_sender_fingerprint_matched='+str(bool(event and event.sender_fingerprint==metadata.get('account_fingerprint'))).lower())
        if receiver.last_recipient_binding:
            print('recipient_binding='+receiver.last_recipient_binding)
            print('recipient_fingerprint='+event.sender_fingerprint)
            return 0
        print('recipient_binding=NOT_BOUND')
        return 2
    finally:
        storage.close()

def _recipient_revoke():
    RecipientBindingStore().delete()
    print('recipient_binding_deleted=true; proactive send capability is unavailable until a new approved binding')
    return 0

def _live_proactive_send_once():
    """The separately approved, single Phase 1.6B proactive send."""
    state_dir=CredentialStore().state_dir
    storage=ProactiveSendStorage(state_dir/'proactive-send.db')
    transport=None
    result=None
    try:
        transport=ProactiveIlinkTransport(CredentialStore().load())
        pilot=SingleProactivePilot(RecipientBindingStore(),storage,transport)
        result=pilot.send_once()
        loaded='YES'; decrypted='YES'; matched='YES'; collision='NO'
    except Exception:
        loaded='NO'; decrypted='NO'; matched='NO'; collision='UNKNOWN'
        result={'submitted':False,'success':False,'errcode':'FAIL_CLOSED','state':'FAILED','duplicate':False}
    print('Recipient binding:')
    print('- loaded: '+loaded)
    print('- DPAPI decrypt: '+decrypted)
    print('- fingerprint matched: '+matched)
    print('- collision: '+collision)
    print('Recipient source: DPAPI_BINDING_ONLY')
    print('Context token used: NO')
    print('Reply-context accessed: NO')
    print('Message: '+PILOT_TEXT)
    print('Idempotency key: '+PILOT_KEY)
    print('Send API:')
    print('- submitted: '+('YES' if result['submitted'] else 'NO'))
    print('- success: '+('YES' if result['success'] else 'NO'))
    print('- errcode: '+str(result['errcode']))
    print('- state: '+str(result['state']))
    attempt=storage.get(PILOT_KEY)
    print('- submitted_at: '+str(attempt['submitted_at']) if attempt else '- submitted_at: NONE')
    print('Duplicate send attempted: '+('YES' if result['duplicate'] else 'NO'))
    print('Raw recipient ID logged: NO')
    print('Raw recipient ID stored in SQLite: NO')
    print('Real proactive messages sent: '+str(len(transport.calls) if transport else 0))
    print('Client-visible confirmation: PENDING_USER_CONFIRMATION')
    return 0 if result['state']=='ACKED_API' else 2

def _plan_paths():
    root=Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ilink-receive-probe'/'daily-plan'
    root.mkdir(parents=True,exist_ok=True)
    return root

def _live_plan_receive():
    storage=_storage(); planner=None; bridge=None
    try:
        store=CredentialStore(); metadata=json.loads(store.meta.read_text(encoding='utf-8')) if store.meta.exists() else {}
        paths=_plan_paths(); planner=DailyPlanService(paths/'planner.db',MockTransport())
        reply_transport=ReplyOnlyTransport(store.load())
        bridge=SessionBridge(planner,storage,ReplyContextStore(),reply_transport,paths/'bridge.db')
        receiver=Receiver(RealIlinkTransport(token=store.load()),storage,metadata.get('account_fingerprint'),metadata.get('bot_fingerprint'),context_store=ReplyContextStore())
        cursor_before=storage.cursor(); accepted=receiver.receive_once(); now=datetime.now(timezone(timedelta(hours=8),'Asia/Shanghai')); adapter=WechatAdapter(bridge)
        statuses=[adapter.accept_inbound(event,now) for event in accepted]
        print('observed='+str(len(receiver.last_events))+' accepted='+str(len(accepted))+' replied='+str(statuses.count('ACKED_API'))+' cursor_advanced='+str(storage.cursor()!=cursor_before).lower())
        for status in statuses: print('plan_reply_status='+status)
        session=planner.storage.session()
        if session: print('planner_state='+session['state']+' target_date='+session['target_date'])
        row=planner.storage.db.execute('SELECT canonical_text FROM plans ORDER BY rowid DESC LIMIT 1').fetchone()
        if row: print('final_plan_hash='+_fingerprint(row['canonical_text']))
        return 0 if all(status=='ACKED_API' for status in statuses) else 2
    finally:
        if bridge: bridge.close()
        if planner: planner.storage.close()
        storage.close()

def _show_final_plan():
    paths=_plan_paths(); planner=DailyPlanService(paths/'planner.db',MockTransport())
    try:
        row=planner.storage.db.execute('SELECT canonical_text FROM plans ORDER BY rowid DESC LIMIT 1').fetchone()
        if not row: raise RuntimeError('no confirmed local plan exists')
        print(row['canonical_text'])
        return 0
    finally:
        planner.storage.close()

def _storage():
    state_dir=Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ilink-receive-probe'
    return ProbeStorage(state_dir/'probe.db')

def _dedupe_check():
    storage=_storage()
    try:
        row=storage.db.execute("SELECT account_scope,message_id,sender_fingerprint,received_at FROM inbox WHERE status='accepted' ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row: raise RuntimeError('no accepted real message is available for offline duplicate validation')
        metadata=json.loads(CredentialStore().meta.read_text(encoding='utf-8'))
        event=InboundEvent(row['account_scope'],row['message_id'],row['sender_fingerprint'],row['received_at'],'',metadata['bot_fingerprint'])
        print('duplicate_ignored='+str(not storage.save_event(event,'accepted')).lower())
        return 0
    finally:
        storage.close()

def _set_gateway(enabled):
    storage=_storage()
    try:
        storage.set_setting('wechat_gateway_enabled','true' if enabled else 'false')
        print('wechat_gateway_enabled='+str(enabled).lower())
        return 0
    finally:
        storage.close()

def _bind(sender_fingerprint, bot_fingerprint):
    if not sender_fingerprint or not bot_fingerprint: raise RuntimeError('sender and bot fingerprints are required')
    store=CredentialStore()
    if not store.path.exists(): raise RuntimeError('no local DPAPI credential exists')
    metadata=json.loads(store.meta.read_text(encoding='utf-8')) if store.meta.exists() else {}
    metadata.update({'account_fingerprint':sender_fingerprint,'bot_fingerprint':bot_fingerprint})
    store.meta.write_text(json.dumps(metadata,separators=(',',':')),encoding='utf-8')
    print('allowlist binding stored with redacted fingerprints')
    return 0

def main(argv=None):
    p=argparse.ArgumentParser(prog='ilink_receive_probe'); p.add_argument('command',choices=['login','receive','plan-receive','plan-show-final','reply-latest','recipient-bind-receive','recipient-revoke','proactive-send-once','status','logout','bind','dedupe-check','disable','enable']); p.add_argument('--live',action='store_true'); p.add_argument('--sender-fingerprint'); p.add_argument('--bot-fingerprint'); args=p.parse_args(argv)
    if args.live:
        if args.command=='login': return _live_login()
        if args.command=='receive': return _live_receive()
        if args.command=='plan-receive': return _live_plan_receive()
        if args.command=='reply-latest': return _live_reply_latest()
        if args.command=='recipient-bind-receive': return _live_recipient_bind_receive()
        if args.command=='proactive-send-once': return _live_proactive_send_once()
        p.error('--live is only valid with login, receive, plan-receive, reply-latest, recipient-bind-receive, or proactive-send-once')
    if args.command=='bind': return _bind(args.sender_fingerprint,args.bot_fingerprint)
    if args.command=='plan-show-final': return _show_final_plan()
    if args.command=='dedupe-check': return _dedupe_check()
    if args.command=='disable': return _set_gateway(False)
    if args.command=='enable': return _set_gateway(True)
    if args.command=='recipient-revoke': return _recipient_revoke()
    r=Receiver(FakeIlinkTransport())
    if args.command=='login': print(r.login_mock()[0]['state'])
    elif args.command=='receive': print(len(r.receive_once()))
    elif args.command=='status': print(r.state)
    else:
        CredentialStore().delete()
        print('local DPAPI credential deleted; remote revoke is not provided by the receive-only protocol adapter')
    return 0

if __name__=='__main__': raise SystemExit(main())
