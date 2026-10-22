"""Narrow inbound bridge: a valid user reply only suppresses the reminder."""
class ProactiveInboundBridge:
    def __init__(self, scheduler, sender_fingerprint, bot_fingerprint):
        self.scheduler=scheduler; self.sender_fingerprint=sender_fingerprint; self.bot_fingerprint=bot_fingerprint
    def accept(self,event,now):
        if (event.sender_fingerprint != self.sender_fingerprint or event.bot_id != self.bot_fingerprint
                or event.conversation_type != 'one_to_one' or event.is_forwarded):
            return False
        self.scheduler.record_user_reply(now)
        return True
