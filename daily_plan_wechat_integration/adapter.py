class WechatAdapter:
    """Accepts already-whitelisted events only; it never polls or sends by itself."""
    def __init__(self, bridge): self.bridge=bridge
    def accept_inbound(self,event,now): return self.bridge.handle(event,now)
