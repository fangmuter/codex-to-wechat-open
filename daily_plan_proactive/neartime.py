"""Isolation checks for the explicitly approved near-time scheduler pilot."""
import sqlite3

from .scheduler import TZ


def production_2200_state(path, now):
    """Read production state without creating or mutating its database."""
    if not path.exists():
        return {"proactive_sent": False, "idempotency_consumed": False, "reminder_modified": False, "eligible": True}
    try:
        db=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True); db.row_factory=sqlite3.Row
        day=now.astimezone(TZ).date().isoformat()
        row=db.execute("SELECT proactive_key,proactive_sent_at,reminder_key,reminder_sent_at FROM proactive_days WHERE day=?",(day,)).fetchone(); db.close()
    except sqlite3.Error:
        return {"proactive_sent": False, "idempotency_consumed": False, "reminder_modified": False, "eligible": True}
    if not row: return {"proactive_sent": False, "idempotency_consumed": False, "reminder_modified": False, "eligible": True}
    sent=bool(row["proactive_sent_at"])
    return {"proactive_sent": sent, "idempotency_consumed": bool(row["proactive_key"]), "reminder_modified": bool(row["reminder_key"] or row["reminder_sent_at"]), "eligible": not sent}
