class Whitelist:
    def __init__(self, sender_id=None, bot_id=None): self.sender_id=sender_id; self.bot_id=bot_id
    def check(self,event):
        return bool(self.sender_id and self.bot_id and event.sender_fingerprint==self.sender_id and event.bot_id==self.bot_id and event.conversation_type=='one_to_one' and not event.is_forwarded)
