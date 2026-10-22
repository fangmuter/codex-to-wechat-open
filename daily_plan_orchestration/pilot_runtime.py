"""Finite Phase 1.9 supervised-pilot orchestration; no proactive sending."""
from dataclasses import dataclass
from datetime import datetime
import time


@dataclass
class PilotEvidence:
    start_prompt_api_state: str = "NOT_SENT"
    accepted_inbound_count: int = 0
    rejected_inbound_count: int = 0
    reminder_suppression_hook_invoked: bool = False
    reached_draft_ready: bool = False
    available_minutes_persisted: bool = False
    energy_persisted: bool = False
    fusion_invoked: bool = False
    recommendation_invoked: bool = False
    system_recommended_count: int = 0
    reading_recommendation_count: int = 0
    reading_metadata: tuple = ()
    formatted_draft_api_state: str = "NOT_REACHED"
    exact_confirmation_received: bool = False
    revision_matched: bool = False
    confirmed: bool = False
    confirmation_reply_api_state: str = "NOT_REACHED"
    duplicate_reply_attempted: bool = False
    roadmap_metadata: tuple = ()


class SupervisedPilot:
    """Processes fresh getupdates batches until confirmation, timeout, or Ctrl+C."""
    def __init__(self, receiver, adapter, planner, meta_store, now, sleep=time.sleep, on_confirmed=None):
        self.receiver=receiver; self.adapter=adapter; self.planner=planner; self.meta_store=meta_store
        self.now=now; self.sleep=sleep; self.evidence=PilotEvidence()
        self.on_confirmed=on_confirmed; self.exported=False

    def process_once(self):
        accepted=self.receiver.receive_once()
        self.evidence.accepted_inbound_count+=len(accepted)
        self.evidence.rejected_inbound_count+=sum(1 for event in self.receiver.last_events if not self.receiver.whitelist.check(event))
        for event in accepted:
            before=self.planner.storage.session()
            if event.text.strip()=='确认计划':
                self.evidence.exact_confirmation_received=True
                self.evidence.revision_matched=self.adapter.orchestrator.can_confirm(before,self.adapter.bridge.store.session()) if before else False
            status=self.adapter.accept_inbound(event,self.now())
            if status=='DUPLICATE_IGNORED': self.evidence.duplicate_reply_attempted=True
            after=self.planner.storage.session()
            if after and after['state']=='DRAFT_READY' and (not before or before['state']!='DRAFT_READY'):
                record=self.meta_store.get(after['id'])
                self.evidence.reached_draft_ready=True
                self.evidence.available_minutes_persisted=bool(record and record['available_minutes'] is not None)
                self.evidence.energy_persisted=bool(record and record['energy'])
                self.evidence.fusion_invoked=True
                self.evidence.recommendation_invoked=True
                self.evidence.system_recommended_count=len([item for item in self.adapter.orchestrator.last_draft.items if item.source.value=='SYSTEM_RECOMMENDED'])
                self.evidence.roadmap_metadata=tuple({'origin':item.recommendation_origin,'roadmap_task_id':item.roadmap_task_id,'roadmap_stage':item.roadmap_stage,'roadmap_milestone':item.roadmap_milestone} for item in self.adapter.orchestrator.last_draft.items if item.source.value=='SYSTEM_RECOMMENDED')
                readings=[item for item in self.adapter.orchestrator.last_draft.items if item.recommendation_origin=='READING']
                self.evidence.reading_recommendation_count=len(readings)
                self.evidence.reading_metadata=tuple({'origin':item.recommendation_origin,'title':item.title,'minutes':item.estimated_minutes,'capacity_key':item.capacity_key} for item in readings)
                self.evidence.formatted_draft_api_state=status
            if event.text.strip()=='确认计划':
                self.evidence.confirmed=bool(after and after['state']=='CONFIRMED')
                self.evidence.confirmation_reply_api_state=status
                if self.evidence.confirmed and self.on_confirmed and not self.exported:
                    snapshot=self.adapter.orchestrator.confirmed_snapshot(after) or self.adapter.orchestrator.last_draft
                    self.on_confirmed(snapshot, self.adapter.orchestrator, after)
                    self.exported=True
        self.evidence.reminder_suppression_hook_invoked=bool(self.adapter.reminder_suppression_invocations)
        return self.evidence.confirmed

    def run(self, timeout_seconds, poll_seconds=2):
        deadline=time.monotonic()+timeout_seconds
        while time.monotonic()<deadline:
            if self.process_once(): return 'CONFIRMED'
            self.sleep(poll_seconds)
        return 'TIMEOUT'
