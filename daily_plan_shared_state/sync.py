"""Allowlisted, local mirror sync for an already-mounted Drive folder."""
import hashlib, os, shutil, tempfile
from pathlib import Path

ALLOWLIST=('DAILY_PLAN_SHARED_STATE.json','DAILY_PLAN_SHARED_SUMMARY.md')

class SyncError(ValueError): pass

def _under(path, root):
    try: Path(path).resolve().relative_to(Path(root).resolve()); return True
    except ValueError: return False

def sync_shared_state(source_dir, destination_dir, *, drive_root=None):
    source=Path(source_dir).resolve(); destination=Path(destination_dir).resolve()
    if not source.is_dir(): raise SyncError('canonical source directory unavailable')
    if not destination.is_dir(): raise SyncError('destination must already exist')
    root=drive_root or os.environ.get('DAILY_PLAN_SHARED_DRIVE_ROOT')
    if not root or not _under(destination,root): raise SyncError('destination is outside detected Drive root')
    results={}
    for name in ALLOWLIST:
        src=source/name
        if not src.is_file(): raise SyncError(f'missing canonical export: {name}')
        fd,tmp=tempfile.mkstemp(prefix=name+'.',suffix='.tmp',dir=destination)
        try:
            with os.fdopen(fd,'wb') as out, src.open('rb') as inp:
                shutil.copyfileobj(inp,out); out.flush(); os.fsync(out.fileno())
            os.replace(tmp,destination/name)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        results[name]=hashlib.sha256(src.read_bytes()).hexdigest()
    return {'files':tuple(ALLOWLIST),'hashes':results}
