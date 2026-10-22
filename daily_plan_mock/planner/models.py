from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class State(str, Enum):
    IDLE='IDLE'; ASK_REVIEW='ASK_REVIEW'; WAIT_REVIEW='WAIT_REVIEW'; ASK_REQUIRED_RESULT='ASK_REQUIRED_RESULT'; ASK_FIXED_EVENTS='ASK_FIXED_EVENTS'; ASK_CARRYOVER='ASK_CARRYOVER'; ASK_CAPACITY='ASK_CAPACITY'; DRAFT_READY='DRAFT_READY'; CONFIRMED='CONFIRMED'; CONFIRMED_PARTIAL='CONFIRMED_PARTIAL'; SKIPPED_TONIGHT='SKIPPED_TONIGHT'; PAUSED_UNTIL_DATE='PAUSED_UNTIL_DATE'; WAITING_2230_REMINDER='WAITING_2230_REMINDER'; SESSION_EXPIRED='SESSION_EXPIRED'; WECHAT_UNAVAILABLE='WECHAT_UNAVAILABLE'; DRAFT_EXPIRED_AFTER_0200='DRAFT_EXPIRED_AFTER_0200'; EMERGENCY_DISABLED='EMERGENCY_DISABLED'
class Destination(str, Enum):
    WECHAT='WECHAT'; CODEX='CODEX'; LOCAL='LOCAL'

@dataclass
class Draft:
    target_date: str
    must_do: list[dict[str, Any]] = field(default_factory=list)
    fixed_events: list[dict[str, Any]] = field(default_factory=list)
    optional: list[dict[str, Any]] = field(default_factory=list)
    buffer_minutes: int = 30
    overload: bool = False
    overload_warning: str = ''

    def to_dict(self):
        return self.__dict__.copy()
