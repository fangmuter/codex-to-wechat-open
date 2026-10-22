"""Fail-safe adapter from an explicitly configured local Review Inbox."""
import re
from pathlib import Path
from .review_sync import import_review, ReviewSyncError

FILENAME=re.compile(r'^ROADMAP_REVIEW_UPDATE_[^/\\]+\.json$')

class ReviewInboxError(ValueError): pass

def _rooted(path, root):
    try: Path(path).resolve().relative_to(Path(root).resolve()); return True
    except ValueError: return False

def import_inbox_file(path, inbox_root, store):
    root=Path(inbox_root).resolve(); candidate=Path(path).resolve()
    if not root.is_dir(): return {'status':'NOOP','reason':'inbox_unavailable'}
    if not _rooted(candidate,root) or not candidate.is_file() or not FILENAME.match(candidate.name): raise ReviewInboxError('invalid inbox path')
    try: result=import_review(candidate,store)
    except ReviewSyncError: raise
    return {'status':'IMPORTED' if result['imported'] else 'NOOP','review_id':result['review_id'],'accepted_count':result['update_count']}

def process_inbox(inbox_root, store):
    """Process immutable event files under the configured root only."""
    root=Path(inbox_root)
    if not root.is_dir(): return {'status':'NOOP','reason':'inbox_unavailable','processed':0}
    results=[]
    for path in sorted(root.iterdir(),key=lambda p:p.name):
        if path.is_file() and FILENAME.match(path.name): results.append(import_inbox_file(path,root,store))
    return {'status':'OK','processed':len(results),'results':results}
