import sqlite3

SCHEMA='''PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), cursor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox(account_scope TEXT NOT NULL,message_id TEXT NOT NULL,sender_fingerprint TEXT,received_at TEXT,text TEXT,status TEXT NOT NULL,bot_fingerprint TEXT,conversation_type TEXT,context_ref TEXT,PRIMARY KEY(account_scope,message_id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replies(idempotency_key TEXT PRIMARY KEY,account_scope TEXT NOT NULL,message_id TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT);'''

class ProbeStorage:
    def __init__(self,path=':memory:'):
        self.db=sqlite3.connect(path); self.db.row_factory=sqlite3.Row; self.db.executescript(SCHEMA); self._migrate(); self.db.commit()
    def _migrate(self):
        existing={row['name'] for row in self.db.execute('PRAGMA table_info(inbox)')}
        for name in ('bot_fingerprint','conversation_type','context_ref'):
            if name not in existing: self.db.execute(f'ALTER TABLE inbox ADD COLUMN {name} TEXT')
    def close(self): self.db.close()
    def cursor(self):
        r=self.db.execute('SELECT cursor FROM checkpoint WHERE id=1').fetchone(); return r['cursor'] if r else ''
    def save_event(self,e,status):
        before=self.db.total_changes; self.db.execute('INSERT OR IGNORE INTO inbox(account_scope,message_id,sender_fingerprint,received_at,text,status,bot_fingerprint,conversation_type) VALUES(?,?,?,?,?,?,?,?)',(e.account_scope,e.message_id,e.sender_fingerprint,e.received_at,None,status,e.bot_id,e.conversation_type)); self.db.commit(); return self.db.total_changes>before
    def inbound(self,account_scope,message_id): return self.db.execute('SELECT * FROM inbox WHERE account_scope=? AND message_id=?',(account_scope,message_id)).fetchone()
    def event_status(self,account_scope,message_id):
        row=self.db.execute('SELECT status FROM inbox WHERE account_scope=? AND message_id=?',(account_scope,message_id)).fetchone()
        return row['status'] if row else None
    def set_event_status(self,account_scope,message_id,status):
        if not isinstance(status,str) or not status.strip(): raise ValueError('status is required')
        self.db.execute('UPDATE inbox SET status=? WHERE account_scope=? AND message_id=?',(status,account_scope,message_id)); self.db.commit()
    def set_context_ref(self,account_scope,message_id,context_ref): self.db.execute('UPDATE inbox SET context_ref=? WHERE account_scope=? AND message_id=?',(context_ref,account_scope,message_id)); self.db.commit()
    def reply(self,key): return self.db.execute('SELECT * FROM replies WHERE idempotency_key=?',(key,)).fetchone()
    def create_reply(self,key,account_scope,message_id): self.db.execute('INSERT OR IGNORE INTO replies(idempotency_key,account_scope,message_id,status) VALUES(?,?,?,?)',(key,account_scope,message_id,'PENDING')); self.db.commit(); return self.reply(key)
    def update_reply(self,key,status,error=None): self.db.execute('UPDATE replies SET status=?,attempts=attempts+1,last_error=? WHERE idempotency_key=?',(status,error,key)); self.db.commit(); return self.reply(key)
    def advance(self,cursor): self.db.execute('INSERT OR REPLACE INTO checkpoint VALUES(1,?)',(cursor,)); self.db.commit()
    def setting(self,k,default='true'):
        r=self.db.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone(); return r['value'] if r else default
    def set_setting(self,k,v): self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,str(v))); self.db.commit()
