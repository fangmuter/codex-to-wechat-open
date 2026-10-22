import json
from datetime import datetime, timezone
from .models import SharedStateDocument
from .storage import SharedStateStorage
from .formatter import format_summary

FORBIDDEN={'recipient','recipient_id','credential','cookie','token','context_token','reply_context','ciphertext','raw_message','chat_history'}

def _assert_safe(value):
    if isinstance(value,dict):
        for key,val in value.items():
            if any(part in key.lower() for part in FORBIDDEN): raise ValueError('forbidden shared-state field')
            _assert_safe(val)
    elif isinstance(value,(list,tuple)):
        for item in value: _assert_safe(item)

def roadmap_summary_from_store(store):
    """Read-only projection of the canonical roadmap store."""
    active_stages=[r['title'] for r in store.db.execute("SELECT s.title FROM stages s JOIN progress p ON p.entity_id=s.id AND p.entity_type='stage' WHERE p.status='IN_PROGRESS' ORDER BY s.sort_order")]
    active_milestones=[r['title'] for r in store.db.execute("SELECT m.title FROM milestones m JOIN progress p ON p.entity_id=m.id AND p.entity_type='milestone' WHERE p.status='IN_PROGRESS' ORDER BY m.id")]
    active=[r['title'] for r in store.db.execute("SELECT t.title FROM tasks t JOIN progress p ON p.entity_id=t.id AND p.entity_type='task' WHERE p.status='IN_PROGRESS' ORDER BY t.id")]
    done=[r['title'] for r in store.db.execute("SELECT t.title FROM tasks t JOIN progress p ON p.entity_id=t.id AND p.entity_type='task' WHERE p.status='DONE' ORDER BY t.id")]
    return {'active_stages':active_stages,'active_milestones':active_milestones,'recently_completed_tasks':done[-5:],'active_tasks':active,'next_eligible_tasks':[x.title for x in store.next_tasks(limit=4)]}

class SharedStateExporter:
    def __init__(self, destination): self.storage=SharedStateStorage(destination)
    def export(self, document):
        payload=document.to_dict() if isinstance(document,SharedStateDocument) else dict(document)
        _assert_safe(payload)
        json_path=self.storage.write_json(payload)
        md_path=self.storage.write_atomic('DAILY_PLAN_SHARED_SUMMARY.md',format_summary(payload))
        return {'json':str(json_path),'markdown':str(md_path),'document':payload}

    def export_confirmed_planner(self, planner_storage, *, revision=None, planning_window=None,
                                 available_minutes=None, energy=None, history=(),
                                 review_summary=None, roadmap_summary=None, generated_at=None):
        """Narrow hook: only a persisted Phase 1.4 CONFIRMED session is exportable."""
        session=planner_storage.session()
        if not session or session['state'] != 'CONFIRMED':
            raise ValueError('only CONFIRMED planner sessions may be exported')
        draft=json.loads(session['draft_json'] or '{}')
        plan={'plan_date':session['target_date'],'confirmed_at':session['updated_at'],
              'revision':revision,'planning_window':planning_window,
              'available_minutes':available_minutes,'energy':energy,
              'user_fixed':draft.get('fixed_events',[]),'user_required':draft.get('must_do',[]),
              'carryover':draft.get('optional',[]),'user_arranged':[],
              'system_recommendations':[],'roadmap_recommendations':[],
              'total_user_minutes':sum(x.get('estimated_minutes',0) for x in draft.get('must_do',[])),
              'total_recommendation_minutes':0,'buffer_minutes':draft.get('buffer_minutes',0)}
        return self.export(SharedStateDocument(1,generated_at or datetime.now(timezone.utc).isoformat(),plan,tuple(history),review_summary,roadmap_summary or {}))

    def export_confirmed_fusion(self, planner_storage, fusion_draft, *, revision=None,
                                review_summary=None, roadmap_summary=None, history=(), generated_at=None):
        """Export a confirmed Phase 1.9 fusion draft without reimplementing fusion."""
        if not planner_storage.session() or planner_storage.session()['state'] != 'CONFIRMED':
            raise ValueError('only CONFIRMED planner sessions may be exported')
        def item(x):
            return {'title':x.title,'minutes':x.estimated_minutes,'priority':x.priority,
                    **({'deferred':True} if getattr(x,'deferred',False) else {}),
                    **({'reason':x.reason} if x.reason else {}),
                    **({'roadmap_task_id':x.roadmap_task_id,'roadmap_stage':x.roadmap_stage,
                        'roadmap_milestone':x.roadmap_milestone,'deterministic_reason':x.reason}
                       if x.recommendation_origin=='ROADMAP' else {})}
        plan={'plan_date':fusion_draft.date,'confirmed_at':planner_storage.session()['updated_at'],
              'revision':revision,'planning_window':fusion_draft.planning_window,
              'available_minutes':fusion_draft.available_minutes,'energy':fusion_draft.energy.value,
              'user_fixed':[item(x) for x in fusion_draft.items if x.source.value=='USER_FIXED'],
              'user_required':[item(x) for x in fusion_draft.items if x.source.value=='USER_REQUIRED'],
              'carryover':[item(x) for x in fusion_draft.items if x.source.value=='CARRYOVER'],
              'user_arranged':[item(x) for x in fusion_draft.items if x.source.value=='USER_ARRANGED'],
              'system_recommendations':[item(x) for x in fusion_draft.items if x.source.value=='SYSTEM_RECOMMENDED' and x.recommendation_origin!='ROADMAP'],
              'roadmap_recommendations':[item(x) for x in fusion_draft.items if x.recommendation_origin=='ROADMAP'],
              'deferred_items':[item(x) for x in getattr(fusion_draft,'deferred_items',())],
              'total_user_minutes':fusion_draft.total_committed_minutes,
              'total_recommendation_minutes':fusion_draft.total_recommended_minutes,
              'buffer_minutes':fusion_draft.remaining_buffer_minutes}
        return self.export(SharedStateDocument(1,generated_at or datetime.now(timezone.utc).isoformat(),plan,tuple(history),review_summary,roadmap_summary or {}))

def export_shared_state(destination_dir, *, confirmed_plan=None, history=(), review_summary=None, roadmap_summary=None, generated_at=None):
    """Export only caller-supplied structured facts; no chat text is accepted."""
    document=SharedStateDocument(1,generated_at or datetime.now(timezone.utc).isoformat(),confirmed_plan,tuple(history),review_summary,roadmap_summary or {})
    return SharedStateExporter(destination_dir).export(document)
