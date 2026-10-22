"""Durable Phase 1.6A scheduler; deliberately fake-transport only."""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try: TZ=ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError: TZ=timezone(timedelta(hours=8), "Asia/Shanghai")
PROMPT="开始准备明日计划了吗？"
REMINDER="提醒一下：开始准备明日计划了吗？"

class ProactiveTransport(Protocol):
    def send(self, text: str, idempotency_key: str, now: datetime): ...

class FakeProactiveTransport:
    def __init__(self, available=True): self.available=available; self.sent=[]
    def send(self,text,key,now=None):
        if not self.available: return False
        self.sent.append((text,key)); return True

@dataclass(frozen=True)
class TickResult:
    status: str
    sent: bool=False
    submitted: bool=False

class ProactiveScheduler:
    """One bound-recipient scheduler.  A real sender is intentionally absent."""
    def __init__(self,path=":memory:",transport:ProactiveTransport|None=None,proactive_due=None,reminder_enabled=True,key_namespace="phase16"):
        self.db=sqlite3.connect(path); self.db.row_factory=sqlite3.Row; self.transport=transport or FakeProactiveTransport(); self.proactive_due=proactive_due; self.reminder_enabled=reminder_enabled; self.key_namespace=key_namespace
        self.db.executescript("""CREATE TABLE IF NOT EXISTS proactive_days(
          day TEXT PRIMARY KEY,status TEXT NOT NULL,proactive_key TEXT UNIQUE,reminder_key TEXT UNIQUE,
          proactive_sent_at TEXT,reminder_sent_at TEXT,user_replied_at TEXT,pending_kind TEXT,updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS proactive_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);"""); self.db.commit()
    def close(self): self.db.close()
    def _local(self,now): return now.astimezone(TZ)
    def _row(self,day): return self.db.execute("SELECT * FROM proactive_days WHERE day=?",(day,)).fetchone()
    def _upsert(self,day,status,now,**values):
        row=self._row(day); values["updated_at"]=now.isoformat()
        if row:
            self.db.execute("UPDATE proactive_days SET status=?,"+",".join(f"{k}=?" for k in values)+" WHERE day=?",[status,*values.values(),day])
        else:
            self.db.execute("INSERT INTO proactive_days(day,status,updated_at) VALUES(?,?,?)",(day,status,values["updated_at"]))
        self.db.commit()
    def _setting(self,key,default=""):
        row=self.db.execute("SELECT value FROM proactive_settings WHERE key=?",(key,)).fetchone(); return row["value"] if row else default
    def skip_tonight(self,now): self._upsert(self._local(now).date().isoformat(),"skipped",self._local(now)); return "skipped"
    def pause_until(self,date_text): self.db.execute("INSERT OR REPLACE INTO proactive_settings VALUES('paused_until',?)",(date_text,)); self.db.commit()
    def resume(self): self.db.execute("DELETE FROM proactive_settings WHERE key='paused_until'"); self.db.commit()
    def record_user_reply(self,now):
        local=self._local(now); day=local.date().isoformat(); self._upsert(day,"user_replied",local,user_replied_at=local.isoformat()); return "user_replied"
    def _expired(self,local,day): return local >= datetime.fromisoformat(day+"T02:00:00").replace(tzinfo=TZ)+timedelta(days=1)
    def _paused(self,day): return bool(self._setting("paused_until")) and day <= self._setting("paused_until")
    def _proactive_is_due(self,local):
        if self.proactive_due is None: return local.hour>=22
        return local >= self._local(self.proactive_due)
    def _send(self,day,kind,now):
        key=f"{self.key_namespace}:{day}:{kind}"; text=PROMPT if kind=="proactive" else REMINDER
        result=self.transport.send(text,key,now)
        if result is False: self._upsert(day,"unavailable",now,pending_kind=kind); return TickResult("unavailable")
        state=getattr(result,"state","ACKED_API")
        submitted=getattr(result,"submitted",True)
        if state=="UNAVAILABLE": self._upsert(day,"unavailable",now,pending_kind=kind); return TickResult("unavailable",False,submitted)
        if state=="UNKNOWN": self._upsert(day,"ambiguous",now,**{kind+"_key":key}); return TickResult("ambiguous",False,submitted)
        if state=="FAILED": self._upsert(day,"failed",now,**{kind+"_key":key}); return TickResult("failed",False,submitted)
        values={kind+"_key":key,kind+"_sent_at":now.isoformat()}; status=kind+"_sent"
        self._upsert(day,status,now,**values); return TickResult(status,True,submitted)
    def tick(self,now):
        local=self._local(now); day=local.date().isoformat(); row=self._row(day)
        previous=(local.date()-timedelta(days=1)).isoformat(); pending=self._row(previous)
        if pending and pending["status"]=="unavailable" and local.hour>=2:
            self._upsert(previous,"skipped",local)
            return TickResult("skipped")
        if self._paused(day):
            if not row or row["status"] not in ("user_replied","skipped"): self._upsert(day,"paused",local)
            return TickResult("paused")
        if row and row["status"] in ("user_replied","skipped","reminder_sent","ambiguous","failed"): return TickResult(row["status"])
        # A pending/unavailable current evening can recover until 02:00 next day.
        if row and row["status"]=="unavailable":
            if self._expired(local,day): self._upsert(day,"skipped",local); return TickResult("skipped")
            kind=row["pending_kind"] or "proactive"; self._upsert(day,kind+"_due",local,pending_kind=kind); return self._send(day,kind,local)
        if not row and self._proactive_is_due(local):
            self._upsert(day,"proactive_due",local,pending_kind="proactive"); return self._send(day,"proactive",local)
        if row and row["status"]=="proactive_due": return self._send(day,"proactive",local)
        if self.reminder_enabled and row and row["status"]=="proactive_sent" and (local.hour>22 or (local.hour==22 and local.minute>=30)):
            self._upsert(day,"reminder_due",local,pending_kind="reminder"); return self._send(day,"reminder",local)
        if row and row["status"]=="reminder_due": return self._send(day,"reminder",local)
        return TickResult(row["status"] if row else "proactive_due")
