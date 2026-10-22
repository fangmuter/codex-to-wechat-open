"""Strict, typed JSON import from a web review into RoadmapProgressStore."""
import hashlib, json
from datetime import date
from pathlib import Path

MAX_NOTE=500
ALLOWED={'review_id','review_date','updates','milestone_updates','schema_version','source'}
STATUSES={'NOT_STARTED','IN_PROGRESS','BLOCKED','DONE'}

class ReviewSyncError(ValueError): pass

def _normalize(payload):
    if not isinstance(payload,dict) or not set(payload).issubset(ALLOWED) or not {'review_id','review_date','updates'}.issubset(payload): raise ReviewSyncError('malformed review fields')
    if payload.get('schema_version',1)!=1 or payload.get('source','chatgpt_weekly_review')!='chatgpt_weekly_review': raise ReviewSyncError('unsupported review schema/source')
    if not isinstance(payload['review_id'],str) or not payload['review_id'] or len(payload['review_id'])>128: raise ReviewSyncError('invalid review_id')
    try: review_date=date.fromisoformat(payload['review_date'])
    except Exception: raise ReviewSyncError('invalid review_date')
    updates=payload['updates']; milestone_updates=payload.get('milestone_updates',[])
    if not isinstance(updates,list): raise ReviewSyncError('updates must be list')
    normalized=[]; seen=set()
    for item in updates:
        if not isinstance(item,dict) or not set(item).issubset({'task_id','status','note','blocked_reason'}): raise ReviewSyncError('malformed update')
        task_id,status=item.get('task_id'),item.get('status')
        if not isinstance(task_id,str) or not task_id or task_id in seen: raise ReviewSyncError('invalid or duplicate task_id')
        if status not in STATUSES: raise ReviewSyncError('invalid status')
        note=item.get('note'); reason=item.get('blocked_reason')
        if note is not None and (not isinstance(note,str) or len(note)>MAX_NOTE): raise ReviewSyncError('note too long or invalid')
        if reason is not None and (not isinstance(reason,str) or len(reason)>MAX_NOTE): raise ReviewSyncError('blocked_reason too long or invalid')
        seen.add(task_id); normalized.append({'task_id':task_id,'status':status,**({'note':note} if note is not None else {}),**({'blocked_reason':reason} if reason is not None else {})})
    normalized_milestones=[]
    if not isinstance(milestone_updates,list): raise ReviewSyncError('milestone_updates must be list')
    for item in milestone_updates:
        if not isinstance(item,dict) or not set(item).issubset({'milestone_id','status','note','blocked_reason'}): raise ReviewSyncError('malformed milestone update')
        mid,status=item.get('milestone_id'),item.get('status')
        if not isinstance(mid,str) or not mid or status not in STATUSES: raise ReviewSyncError('invalid milestone update')
        normalized_milestones.append({'milestone_id':mid,'status':status})
    if not updates and not normalized_milestones: raise ReviewSyncError('review has no updates')
    canonical={'review_id':payload['review_id'],'review_date':review_date.isoformat(),'updates':sorted(normalized,key=lambda x:x['task_id']),'milestone_updates':sorted(normalized_milestones,key=lambda x:x['milestone_id'])}
    # Content identity intentionally excludes review_id: same dated updates
    # must not refresh progress under a second envelope ID.
    content={'review_date':canonical['review_date'],'updates':canonical['updates'],'milestone_updates':canonical['milestone_updates']}
    encoded=json.dumps(content,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode()
    return canonical,review_date,hashlib.sha256(encoded).hexdigest()

def import_review(path, store):
    """Read only the caller-provided JSON path and atomically import it."""
    try: payload=json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception as exc: raise ReviewSyncError('invalid JSON') from exc
    canonical,review_date,payload_hash=_normalize(payload)
    for item in canonical['updates']:
        if not store._known('task',item['task_id']): raise ReviewSyncError('unknown task_id')
    for item in canonical['milestone_updates']:
        if not store._known('milestone',item['milestone_id']): raise ReviewSyncError('unknown milestone_id')
    try: changed=store.apply_review_updates(canonical['review_id'],review_date,payload_hash,canonical['updates'],canonical['milestone_updates'])
    except ValueError as exc: raise ReviewSyncError(str(exc)) from exc
    return {'review_id':canonical['review_id'],'imported':changed,'update_count':len(canonical['updates']),'payload_hash':payload_hash}
