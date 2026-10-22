from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import timezone
from uuid import uuid4
from .models import State, Draft, Destination
from .storage import Storage
from .renderer import render
from .scheduler import Scheduler

class MockTransport:
    def __init__(self, outcomes=None): self.outcomes=outcomes or {}; self.sent=[]
    def send(self,dest,key,text):
        self.sent.append((dest,key,text)); return self.outcomes.get(dest,True)

class DailyPlanService:
    def __init__(self, db_path=':memory:', transport=None):
        self.storage=Storage(db_path)
        try: self.tz=ZoneInfo('Asia/Shanghai')
        except ZoneInfoNotFoundError: self.tz=timezone(timedelta(hours=8), 'Asia/Shanghai')
        self.transport=transport or MockTransport(); self.disabled=self.storage.setting('wechat_gateway_enabled','true')=='false'; self.paused_until=self.storage.setting('paused_until') or None; self.scheduler=Scheduler(self)
    def trigger(self,now):
        target=(now.date()+timedelta(days=1)).isoformat(); sid=self.storage.insert_session(target,State.ASK_REVIEW.value,now.isoformat()); return State.ASK_REVIEW
    def pause(self,until): self.paused_until=until; self.storage.set_setting('paused_until',until); return State.PAUSED_UNTIL_DATE
    def resume(self): self.paused_until=None; self.storage.set_setting('paused_until',''); return State.IDLE
    def skip_tonight(self,now):
        s=self.storage.session();
        if s:self.storage.update_session(s['id'],state=State.SKIPPED_TONIGHT.value,now=now.isoformat())
        return State.SKIPPED_TONIGHT
    def emergency_disable(self): self.disabled=True; self.storage.set_setting('wechat_gateway_enabled','false'); s=self.storage.session();
    def enable(self): self.disabled=False; self.storage.set_setting('wechat_gateway_enabled','true')
    def receive(self,scope,mid,payload,now):
        if self.disabled:return State.EMERGENCY_DISABLED
        old=self.storage.get_message(scope,mid)
        if old:return State(old['result'])
        s=self.storage.session(); text=payload.get('text','') if isinstance(payload,dict) else str(payload); result=s['state'] if s else State.IDLE.value
        if s:
            state=State(s['state'])
            if isinstance(payload,dict) and payload.get('draft') is not None:
                self.make_draft(payload['draft'],now); result=State.DRAFT_READY.value
            elif state==State.ASK_REVIEW: result=State.WAIT_REVIEW.value
            elif state in (State.WAIT_REVIEW,State.WAITING_2230_REMINDER): result=State.ASK_REQUIRED_RESULT.value
            elif state==State.ASK_REQUIRED_RESULT: result=State.ASK_FIXED_EVENTS.value
            elif state==State.ASK_FIXED_EVENTS: result=State.ASK_CARRYOVER.value
            elif state==State.ASK_CARRYOVER: result=State.ASK_CAPACITY.value
            elif state==State.DRAFT_READY and text.strip()=='确认计划': result=self.confirm(now)
        if s and result in {x.value for x in State} and result not in (State.CONFIRMED.value, State.CONFIRMED_PARTIAL.value):
            self.storage.update_session(s['id'], state=result, now=now.isoformat())
        self.storage.put_message(scope,mid,now.isoformat(),payload,result); return State(result)
    def make_draft(self,data,now):
        s=self.storage.session(); target=s['target_date']; must=data.get('must_do',[]); overload=len(must)>3 or data.get('available_minutes',10**9)<sum(x.get('estimated_minutes',0) for x in must); draft=Draft(target,must[:3],data.get('fixed_events',[]),data.get('optional',[]),data.get('buffer_minutes',30),overload,'容量不足' if overload else ''); self.storage.update_session(s['id'],state=State.DRAFT_READY.value,draft_json=__import__('json').dumps(draft.to_dict(),ensure_ascii=False),now=now.isoformat()); return draft
    def confirm(self,now):
        s=self.storage.session();
        if not s:return State.IDLE.value
        if now.astimezone(self.tz).hour>=2 and now.astimezone(self.tz).hour<22:self.storage.update_session(s['id'],state=State.DRAFT_EXPIRED_AFTER_0200.value,now=now.isoformat()); return State.DRAFT_EXPIRED_AFTER_0200.value
        import json
        d=Draft(**json.loads(s['draft_json'])); pid=str(uuid4()); text=render(d); self.storage.add_plan(pid,s['target_date'],text,now.isoformat());
        for dest in Destination:self.storage.add_outbox(pid,dest.value)
        return self.deliver(pid)
    def confirm_local(self,now,canonical_override=None):
        s=self.storage.session()
        if not s:return State.IDLE.value,None,None
        local_now=now.astimezone(self.tz)
        if local_now.date().isoformat()==s['target_date'] and local_now.hour>=2:
            self.storage.update_session(s['id'],state=State.DRAFT_EXPIRED_AFTER_0200.value,now=now.isoformat())
            return State.DRAFT_EXPIRED_AFTER_0200.value,None,None
        import json
        draft=Draft(**json.loads(s['draft_json'])); pid=str(uuid4()); text=canonical_override if canonical_override is not None else render(draft)
        self.storage.add_plan(pid,s['target_date'],text,now.isoformat())
        self.storage.update_session(s['id'],state=State.CONFIRMED.value,now=now.isoformat())
        return State.CONFIRMED.value,pid,text
    def deliver(self,pid):
        failed=[]
        for row in self.storage.outbox(pid):
            if row['status']=='ACK':continue
            ok=True if row['destination']=='LOCAL' else self.transport.send(row['destination'],row['idempotency_key'],self.storage.plan(pid)['canonical_text'])
            self.storage.mark_outbox(row['id'],'ACK' if ok else 'FAILED',None if ok else 'mock failure'); failed.append(not ok)
        s=self.storage.session(); state=State.CONFIRMED_PARTIAL if any(failed) else State.CONFIRMED; self.storage.update_session(s['id'],state=state.value,now=datetime.now(self.tz).isoformat()); return state.value
