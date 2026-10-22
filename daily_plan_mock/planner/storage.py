import json, sqlite3
from pathlib import Path

SCHEMA='''PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS planning_sessions(id INTEGER PRIMARY KEY,target_date TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,review_done INTEGER DEFAULT 0,required_done INTEGER DEFAULT 0,fixed_done INTEGER DEFAULT 0,carryover_done INTEGER DEFAULT 0,capacity_done INTEGER DEFAULT 0,draft_json TEXT,reminder_sent INTEGER DEFAULT 0,paused_until TEXT);
CREATE TABLE IF NOT EXISTS plans(plan_id TEXT PRIMARY KEY,target_date TEXT NOT NULL,canonical_text TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,plan_id TEXT,target_date TEXT,title TEXT,estimated_minutes INTEGER,kind TEXT,status TEXT,FOREIGN KEY(plan_id) REFERENCES plans(plan_id));
CREATE TABLE IF NOT EXISTS inbound_messages(account_scope TEXT NOT NULL,message_id TEXT NOT NULL,received_at TEXT NOT NULL,payload TEXT NOT NULL,result TEXT,PRIMARY KEY(account_scope,message_id));
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL,destination TEXT NOT NULL,status TEXT NOT NULL,idempotency_key TEXT UNIQUE,attempts INTEGER DEFAULT 0,last_error TEXT,ack_at TEXT,UNIQUE(plan_id,destination));
CREATE TABLE IF NOT EXISTS delivery_attempts(id INTEGER PRIMARY KEY,outbox_id INTEGER,attempted_at TEXT,status TEXT,error TEXT);
'''

class Storage:
    def __init__(self,path=':memory:'):
        self.path=path; self.db=sqlite3.connect(path); self.db.row_factory=sqlite3.Row; self.db.execute('PRAGMA foreign_keys=ON'); self.db.executescript(SCHEMA); self.db.commit()
    def close(self): self.db.close()
    def setting(self,k,default=None):
        r=self.db.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone(); return default if not r else r['value']
    def set_setting(self,k,v): self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,str(v))); self.db.commit()
    def session(self): return self.db.execute('SELECT * FROM planning_sessions ORDER BY id DESC LIMIT 1').fetchone()
    def insert_session(self,target,state,now):
        c=self.db.execute('INSERT INTO planning_sessions(target_date,state,created_at,updated_at) VALUES(?,?,?,?)',(target,state,now,now)); self.db.commit(); return c.lastrowid
    def update_session(self,sid,**kw):
        if not kw:return
        kw['updated_at']=kw.get('updated_at') or kw.get('now') or ''
        self.db.execute('UPDATE planning_sessions SET '+','.join(f'{k}=?' for k in kw if k!='now')+' WHERE id=?',[v for k,v in kw.items() if k!='now']+[sid]); self.db.commit()
    def get_message(self,scope,mid): return self.db.execute('SELECT * FROM inbound_messages WHERE account_scope=? AND message_id=?',(scope,mid)).fetchone()
    def put_message(self,scope,mid,received,payload,result):
        self.db.execute('INSERT OR IGNORE INTO inbound_messages VALUES(?,?,?,?,?)',(scope,mid,received,json.dumps(payload,ensure_ascii=False),result)); self.db.commit()
    def add_plan(self,pid,target,text,now): self.db.execute('INSERT INTO plans VALUES(?,?,?,?)',(pid,target,text,now)); self.db.commit()
    def plan(self,pid): return self.db.execute('SELECT * FROM plans WHERE plan_id=?',(pid,)).fetchone()
    def add_outbox(self,pid,dest):
        key=f'{pid}:{dest}'; self.db.execute('INSERT OR IGNORE INTO outbox(plan_id,destination,status,idempotency_key) VALUES(?,?,?,?)',(pid,dest,'PENDING',key)); self.db.commit()
    def outbox(self,pid=None): return self.db.execute('SELECT * FROM outbox'+(' WHERE plan_id=?' if pid else ''),((pid,) if pid else ())).fetchall()
    def mark_outbox(self,oid,status,error=None): self.db.execute('UPDATE outbox SET status=?,attempts=attempts+1,last_error=?,ack_at=CASE WHEN ?="ACK" THEN CURRENT_TIMESTAMP ELSE ack_at END WHERE id=?',(status,error,status,oid)); self.db.commit()
    def cleanup(self,cutoff):
        self.db.execute('DELETE FROM plans WHERE target_date < ?', (cutoff,)); self.db.execute('DELETE FROM inbound_messages WHERE received_at < ?', (cutoff,)); self.db.commit()
