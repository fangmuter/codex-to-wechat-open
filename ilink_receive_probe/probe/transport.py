from collections import deque
from .models import Batch

class FakeIlinkTransport:
    def __init__(self, batches=None, errors=None):
        self.batches=deque(batches or []); self.errors=deque(errors or []); self.calls=[]
    def login_qr(self): self.calls.append('get_bot_qrcode'); return {'state':'qr_ready','qrcode':'FAKE_QR'}
    def qr_status(self): self.calls.append('get_qrcode_status'); return {'status':'confirmed','bot_id':'fake-bot'}
    def getupdates(self, cursor):
        self.calls.append(('getupdates',cursor))
        if self.errors: raise self.errors.popleft()
        return self.batches.popleft() if self.batches else Batch((),cursor)
