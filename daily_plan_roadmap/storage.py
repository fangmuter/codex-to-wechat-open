import json
import sqlite3
from datetime import date
from pathlib import Path

from .models import Status
from .profile import load_profile
from .roadmap import load_entities


SCHEMA='''
CREATE TABLE IF NOT EXISTS stages(id TEXT PRIMARY KEY,title TEXT NOT NULL,sort_order INTEGER NOT NULL,prerequisites_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS milestones(id TEXT PRIMARY KEY,stage_id TEXT NOT NULL,title TEXT NOT NULL,prerequisites_json TEXT NOT NULL,recommended_task_ids_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,milestone_id TEXT NOT NULL,domain TEXT NOT NULL,title TEXT NOT NULL,estimated_minutes INTEGER NOT NULL,intensity TEXT NOT NULL,prerequisites_json TEXT NOT NULL,completion_criterion TEXT NOT NULL,tags_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS progress(entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,status TEXT NOT NULL,last_progress_at TEXT,started_at TEXT,completed_at TEXT,blocked_reason TEXT,short_note TEXT,source_of_update TEXT NOT NULL,PRIMARY KEY(entity_type,entity_id));
CREATE TABLE IF NOT EXISTS profile_meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS imported_reviews(review_id TEXT PRIMARY KEY,review_date TEXT NOT NULL,payload_hash TEXT NOT NULL,imported_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS imported_reviews_date_hash ON imported_reviews(review_date,payload_hash);
'''


class RoadmapProgressStore:
    def __init__(self,path=':memory:',profile_path=None):
        self.db=sqlite3.connect(path); self.db.row_factory=sqlite3.Row; self.db.executescript(SCHEMA); self.db.commit()
        if not self.db.execute('SELECT 1 FROM stages LIMIT 1').fetchone(): self.initialize(profile_path)
    def close(self): self.db.close()
    def initialize(self,profile_path=None):
        profile=load_profile(profile_path) if profile_path else load_profile()
        stages,milestones,tasks=load_entities(profile)
        self.db.executemany('INSERT INTO stages VALUES(?,?,?,?)',[(x.id,x.title,x.order,json.dumps(x.prerequisites,ensure_ascii=False)) for x in stages])
        self.db.executemany('INSERT INTO milestones VALUES(?,?,?,?,?)',[(x.id,x.stage_id,x.title,json.dumps(x.prerequisite_ids,ensure_ascii=False),json.dumps(x.recommended_task_ids,ensure_ascii=False)) for x in milestones])
        self.db.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)',[(x.id,x.milestone_id,x.domain,x.title,x.estimated_minutes,x.intensity,json.dumps(x.prerequisite_task_ids),x.completion_criterion,json.dumps(x.tags,ensure_ascii=False)) for x in tasks])
        self.db.executemany('INSERT OR REPLACE INTO progress(entity_type,entity_id,status,source_of_update) VALUES(?,?,?,?)', [('stage',x['id'],x.get('status','NOT_STARTED'),'PROFILE_INIT') for x in profile['stages']]+[('milestone',x['id'],x.get('status','NOT_STARTED'),'PROFILE_INIT') for x in profile['milestones']])
        for key,value in profile.get('background_progress',{}).items(): self.db.execute('INSERT OR REPLACE INTO profile_meta VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
        self.db.commit()
    def _known(self,entity_type,entity_id): return self.db.execute('SELECT 1 FROM '+entity_type+'s WHERE id=?',(entity_id,)).fetchone() if entity_type in ('stage','milestone','task') else None
    def _status(self,entity_type,entity_id):
        row=self.db.execute('SELECT status FROM progress WHERE entity_type=? AND entity_id=?',(entity_type,entity_id)).fetchone(); return Status(row['status']) if row else Status.NOT_STARTED
    def status(self,entity_type,entity_id):
        if not self._known(entity_type,entity_id): raise KeyError(f'unknown {entity_type}: {entity_id}')
        return self._status(entity_type,entity_id)
    def update_status(self,entity_type,entity_id,status,*,when=None,blocked_reason=None,short_note=None,source_of_update='EXPLICIT',_commit=True):
        if not self._known(entity_type,entity_id): raise KeyError(f'unknown {entity_type}: {entity_id}')
        status=Status(status); stamp=when.isoformat() if hasattr(when,'isoformat') else (str(when) if when else None)
        old=self.db.execute('SELECT started_at,completed_at FROM progress WHERE entity_type=? AND entity_id=?',(entity_type,entity_id)).fetchone()
        started=old['started_at'] if old else None; completed=old['completed_at'] if old else None
        if status==Status.IN_PROGRESS and not started: started=stamp
        if status==Status.DONE: completed=stamp or completed
        old_status=self._status(entity_type,entity_id)
        if old_status==Status.DONE and status != Status.DONE: raise ValueError('DONE cannot regress without explicit reopen')
        if old_status in (Status.IN_PROGRESS,Status.BLOCKED) and status==Status.NOT_STARTED: raise ValueError('progress cannot regress to NOT_STARTED')
        if status==Status.IN_PROGRESS: blocked_reason=None
        self.db.execute('''INSERT INTO progress(entity_type,entity_id,status,last_progress_at,started_at,completed_at,blocked_reason,short_note,source_of_update)
          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id) DO UPDATE SET status=excluded.status,last_progress_at=excluded.last_progress_at,started_at=excluded.started_at,completed_at=excluded.completed_at,blocked_reason=excluded.blocked_reason,short_note=excluded.short_note,source_of_update=excluded.source_of_update''',
          (entity_type,entity_id,status.value,stamp,started,completed,blocked_reason,short_note,source_of_update));
        if _commit: self.db.commit()

    def apply_review_updates(self, review_id, review_date, payload_hash, updates, milestone_updates=()):
        existing=self.db.execute('SELECT payload_hash FROM imported_reviews WHERE review_id=?',(review_id,)).fetchone()
        if existing:
            if existing['payload_hash']!=payload_hash: raise ValueError('review_id conflict')
            return False
        same_content=self.db.execute('SELECT 1 FROM imported_reviews WHERE review_date=? AND payload_hash=?',(review_date.isoformat(),payload_hash)).fetchone()
        if same_content: return False
        try:
            self.db.execute('BEGIN')
            for item in updates:
                self.update_status('task',item['task_id'],Status(item['status']),when=review_date,blocked_reason=item.get('blocked_reason'),short_note=item.get('note'),source_of_update='WEB_REVIEW',_commit=False)
            allowed={Status.NOT_STARTED:{Status.IN_PROGRESS,Status.DONE},Status.IN_PROGRESS:{Status.DONE},Status.BLOCKED:{Status.IN_PROGRESS,Status.DONE},Status.DONE:set()}
            for item in milestone_updates:
                old=self._status('milestone',item['milestone_id']); new=Status(item['status'])
                if new not in allowed[old]: raise ValueError('illegal milestone transition')
                self.update_status('milestone',item['milestone_id'],new,when=review_date,blocked_reason=item.get('blocked_reason'),short_note=item.get('note'),source_of_update='WEB_REVIEW',_commit=False)
            self.db.execute('INSERT INTO imported_reviews VALUES(?,?,?,datetime(\'now\'))',(review_id,review_date.isoformat(),payload_hash))
            self.db.commit(); return True
        except Exception:
            self.db.rollback(); raise
    def task_status(self,task_id): return self.status('task',task_id)
    def milestone_status(self,milestone_id): return self.status('milestone',milestone_id)
    def stage_status(self,stage_id): return self.status('stage',stage_id)
    def _prereqs_done(self,entity_type,ids): return all(self._status(entity_type,x)==Status.DONE for x in ids)
    def _background_ok(self, requirements):
        for requirement in requirements:
            domain, marker = requirement.split('=', 1)
            row=self.db.execute('SELECT value_json FROM profile_meta WHERE key=?',(domain,)).fetchone()
            if not row or marker not in json.loads(row['value_json']): return False
        return True
    def _active_stage(self):
        rows=self.db.execute('SELECT * FROM stages ORDER BY sort_order').fetchall()
        for row in rows:
            if self._status('stage',row['id'])==Status.IN_PROGRESS and self._prereqs_done('stage',json.loads(row['prerequisites_json'])): return row
        for row in rows:
            if self._status('stage',row['id'])==Status.NOT_STARTED and self._prereqs_done('stage',json.loads(row['prerequisites_json'])): return row
        return None
    def _active_milestone(self,stage_id):
        rows=self.db.execute('SELECT * FROM milestones WHERE stage_id=? ORDER BY id',(stage_id,)).fetchall()
        for row in rows:
            if self._status('milestone',row['id'])==Status.IN_PROGRESS and self._prereqs_done('milestone',json.loads(row['prerequisites_json'])): return row
        for row in rows:
            if self._status('milestone',row['id'])==Status.NOT_STARTED and self._prereqs_done('milestone',json.loads(row['prerequisites_json'])): return row
        return None
    def next_tasks(self,today=None,limit=4):
        today=today or date.today()
        active_stages={r['id'] for r in self.db.execute('SELECT * FROM stages WHERE 1 ORDER BY sort_order') if self._status('stage',r['id'])==Status.IN_PROGRESS}
        milestones=self.db.execute('SELECT * FROM milestones ORDER BY id').fetchall()
        milestones.sort(key=lambda r:(0 if r['stage_id'] in active_stages and self._status('milestone',r['id'])==Status.IN_PROGRESS else 1 if r['stage_id'] in active_stages else 2,r['id']))
        candidates=[]
        for milestone in milestones:
            if self._status('milestone',milestone['id']) in (Status.DONE,Status.BLOCKED): continue
            if not self._prereqs_done('milestone',json.loads(milestone['prerequisites_json'])): continue
            order={task_id:index for index,task_id in enumerate(json.loads(milestone['recommended_task_ids_json']))}
            for row in self.db.execute('SELECT * FROM tasks WHERE milestone_id=?',(milestone['id'],)).fetchall():
                if self._status('task',row['id']) in (Status.DONE,Status.BLOCKED) or not self._prereqs_done('task',json.loads(row['prerequisites_json'])): continue
                progress=self.db.execute('SELECT last_progress_at FROM progress WHERE entity_type="task" AND entity_id=?',(row['id'],)).fetchone(); days=None
                if progress and progress['last_progress_at']:
                    try: days=max(0,(today-date.fromisoformat(progress['last_progress_at'][:10])).days)
                    except ValueError: days=None
                from .models import RoadmapCandidate
                candidates.append((0 if milestone['stage_id'] in active_stages else 1,milestone['id'],order.get(row['id'],999),row['id'],RoadmapCandidate(row['id'],row['title'],row['domain'],row['estimated_minutes'],row['intensity'],milestone['stage_id'],milestone['id'],days)))
        candidates.sort(key=lambda value:value[:4]); return tuple(value[4] for value in candidates[:max(1,min(limit,4))])
