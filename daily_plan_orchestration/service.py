import hashlib
import json
from dataclasses import replace

from daily_plan_fusion.engine import FusionEngine
from daily_plan_fusion.models import plan_draft_from_dict, plan_draft_to_dict
from daily_plan_fusion.planner_adapter import fusion_request_from_phase14
from daily_plan_recommendation.models import Energy
from .formatter import format_draft


ENERGY={'高':Energy.HIGH,'中':Energy.NORMAL,'低':Energy.LOW}


class Phase19Orchestrator:
    def __init__(self,meta_store,planning_window=None,user_arranged=(),roadmap_source=None,reading_source=None):
        self.meta=meta_store; self.planning_window=planning_window; self.user_arranged=tuple(user_arranged); self.roadmap_source=roadmap_source; self.reading_source=reading_source; self.last_draft=None
    def _revision(self,session,bridge):
        arranged=json.loads(bridge['arranged_json'] or '[]') if bridge and 'arranged_json' in bridge.keys() else list(self.user_arranged)
        payload={'draft_json':session['draft_json'],'capacity_json':bridge['capacity_json'],'planning_window':self.planning_window,'user_arranged':arranged}
        return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
    def _request(self,session,planning_window,available_minutes,energy,user_arranged):
        roadmap=tuple(self.roadmap_source.candidates() if self.roadmap_source else ())
        return fusion_request_from_phase14(session,planning_window=planning_window or '未指定（用户声明净可用时间）',available_minutes=int(available_minutes),energy=ENERGY[energy],user_arranged=tuple(user_arranged),roadmap_candidates=roadmap,reading_source=self.reading_source)
    def draft_reply(self,session,bridge):
        capacity=json.loads(bridge['capacity_json'] or '{}')
        if session['state'] != 'DRAFT_READY' or 'available_minutes' not in capacity or capacity.get('energy') not in ENERGY: raise ValueError('structured draft and capacity metadata are required')
        arranged=json.loads(bridge['arranged_json'] or '[]') if bridge and 'arranged_json' in bridge.keys() else list(self.user_arranged)
        request=self._request(session,self.planning_window,capacity['available_minutes'],capacity['energy'],arranged)
        draft=FusionEngine().fuse(request); revision=self._revision(session,bridge)
        snapshot=plan_draft_to_dict(draft)
        snapshot_json=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        fusion_hash=hashlib.sha256(snapshot_json.encode()).hexdigest()
        rendered=format_draft(draft)
        if draft.state != 'DRAFT_READY' or not draft.confirmation_required: raise ValueError('fusion draft is not confirmable')
        self.meta.put(session['id'],session['target_date'],request.planning_window,capacity['available_minutes'],capacity['energy'],json.dumps(arranged,ensure_ascii=False,default=str),revision,rendered.text,snapshot_json,fusion_hash)
        self.last_draft=draft
        return rendered
    def fusion_request_from_metadata(self,session):
        """Rebuild only from persisted, non-sensitive orchestration metadata."""
        record=self.meta.get(session['id'])
        if session['state'] != 'DRAFT_READY' or not record or record['energy'] not in ENERGY: raise ValueError('current fusion metadata is required')
        return self._request(session,record['planning_window'],record['available_minutes'],record['energy'],json.loads(record['user_arranged_json']))
    def can_confirm(self,session,bridge):
        if session['state'] != 'DRAFT_READY': return False
        record=self.meta.get(session['id'])
        if not record or record['revision'] != self._revision(session,bridge): return False
        try:
            draft=FusionEngine().fuse(self.fusion_request_from_metadata(session))
            snapshot=json.dumps(plan_draft_to_dict(draft),ensure_ascii=False,sort_keys=True,separators=(',',':'))
            expected=hashlib.sha256(snapshot.encode()).hexdigest()
        except (ValueError,KeyError,json.JSONDecodeError):
            return False
        return (draft.state == 'DRAFT_READY' and draft.confirmation_required
                and bool(record['fused_json']) and record['fusion_hash']==expected)

    def confirmation_payload(self,session,bridge):
        """Return the exact fused snapshot text that was shown to the user."""
        if not self.can_confirm(session,bridge):
            raise ValueError('current fused snapshot is not confirmable')
        record=self.meta.get(session['id'])
        draft=plan_draft_from_dict(json.loads(record['fused_json']))
        self.last_draft=draft
        return format_draft(draft,state='CONFIRMED').text

    def commit_confirmed(self,session,now):
        record=self.meta.get(session['id'])
        if not record or not record['fused_json']:
            raise ValueError('fused snapshot is required before confirmation')
        draft=plan_draft_from_dict(json.loads(record['fused_json']))
        confirmed=replace(draft,state='CONFIRMED')
        payload=json.dumps(plan_draft_to_dict(confirmed),ensure_ascii=False,sort_keys=True,separators=(',',':'))
        self.meta.mark_confirmed(session['id'],payload,now.isoformat())
        self.last_draft=confirmed

    def confirmed_snapshot(self,session):
        record=self.meta.get(session['id']) if session else None
        if not record: return None
        payload=record['confirmed_fused_json'] or record['fused_json']
        return plan_draft_from_dict(json.loads(payload)) if payload else None


class OrchestrationAdapter:
    """Routes a validated inbound first to reminder suppression, then existing bridge."""
    def __init__(self,bridge,orchestrator,reminder_bridge=None):
        self.bridge=bridge; self.orchestrator=orchestrator; self.reminder_bridge=reminder_bridge
        self.reminder_suppression_invocations=0
        self.bridge.draft_reply_builder=self.orchestrator.draft_reply
        self.bridge.confirmation_guard=lambda session:self.orchestrator.can_confirm(session,self.bridge.store.session())
        self.bridge.confirmed_snapshot_builder=self.orchestrator.confirmation_payload
        self.bridge.confirmed_snapshot_committer=self.orchestrator.commit_confirmed
    def accept_inbound(self,event,now):
        if self.reminder_bridge and self.bridge._allowed(event):
            self.reminder_suppression_invocations+=1
            self.reminder_bridge.accept(event,now)
        return self.bridge.handle(event,now)
