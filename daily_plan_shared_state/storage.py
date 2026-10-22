import json, os, tempfile
from pathlib import Path

class SharedStateStorage:
    def __init__(self, destination): self.destination=Path(destination)
    def write_atomic(self, name, content):
        if not self.destination.is_dir():
            raise OSError('shared-state destination must be an existing directory')
        target=self.destination/name
        fd,tmp=tempfile.mkstemp(prefix=target.name+'.',suffix='.tmp',dir=self.destination)
        try:
            with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as handle:
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp,target)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return target
    def write_json(self, document):
        text=json.dumps(document,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        return self.write_atomic('DAILY_PLAN_SHARED_STATE.json',text)
