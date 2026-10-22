from datetime import timedelta
from .models import State

class Scheduler:
    def __init__(self, service): self.service=service
    def tick(self, now):
        local=now.astimezone(self.service.tz); s=self.service.storage.session()
        if self.service.disabled: return State.EMERGENCY_DISABLED
        if self.service.paused_until and local.date().isoformat() < self.service.paused_until: return State.PAUSED_UNTIL_DATE
        if local.hour==22 and local.minute==0 and not s: return self.service.trigger(local)
        if local.hour==22 and local.minute==30 and s and s['state'] in (State.ASK_REVIEW.value,State.WAIT_REVIEW.value) and not s['reminder_sent']:
            self.service.storage.update_session(s['id'],state=State.WAITING_2230_REMINDER.value,reminder_sent=1,now=local.isoformat()); return State.WAITING_2230_REMINDER
        if s and s['state'] in (State.DRAFT_READY.value,State.WAIT_REVIEW.value,State.WAITING_2230_REMINDER.value) and (local.hour<2): return State(s['state'])
        if s and s['state']==State.DRAFT_READY.value and (local.hour>=2 and local.hour<22):
            self.service.storage.update_session(s['id'],state=State.DRAFT_EXPIRED_AFTER_0200.value,now=local.isoformat()); return State.DRAFT_EXPIRED_AFTER_0200
        return State(s['state']) if s else State.IDLE
