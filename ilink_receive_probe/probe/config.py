from pathlib import Path
import os

def default_state_dir() -> Path:
    configured = os.environ.get('ILINK_STATE_DIR')
    if configured:
        return Path(configured)
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'ilink-receive-probe'

class ProbeConfig:
    def __init__(self, state_dir=None, allowed_sender_id=None, allowed_bot_id=None):
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.allowed_sender_id = allowed_sender_id
        self.allowed_bot_id = allowed_bot_id
        self.enabled = True
