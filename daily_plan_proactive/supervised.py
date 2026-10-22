"""Small helpers for an explicitly supervised Phase 1.6C 22:00 pilot."""
from .scheduler import TZ


def in_2200_pilot_window(now):
    local = now.astimezone(TZ)
    return local.hour == 22 and local.minute < 30


def in_2230_reminder_window(now):
    local = now.astimezone(TZ)
    return local.hour == 22 and local.minute >= 30


def reminder_preconditions(scheduler, now):
    local=now.astimezone(TZ); row=scheduler._row(local.date().isoformat())
    if not row or row["status"] != "proactive_sent": return False
    if row["proactive_sent_at"] is None or row["reminder_sent_at"] is not None: return False
    if row["user_replied_at"] is not None: return False
    if scheduler._paused(local.date().isoformat()): return False
    return True
