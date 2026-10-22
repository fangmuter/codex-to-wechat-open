"""Small deterministic risk screen for the REVIEW boundary.

The screen is deliberately conservative and explainable.  It does not decide
whether a message is safe to send by itself; it adds redacted, stable flags to
the local draft so a human reviewer sees why extra care may be appropriate.
"""

from dataclasses import dataclass
import re


LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    flags: tuple[str, ...] = ()


# Keep these patterns short and category-oriented.  Do not persist the matched
# text: the resulting flag is enough for the review UI and telemetry.
_HIGH_PATTERNS = (
    ("MONEY", r"转账|汇款|借钱|借款|还钱|红包|付款|支付|银行卡|银行账户|支付宝|微信支付|money|transfer|loan|password|passwd|密码|验证码|token|身份证|护照|社保|账号密码"),
    ("LEGAL", r"律师|起诉|诉讼|合同|法律|法院|法务|legal|lawsuit|contract"),
    ("MEDICAL", r"医疗|医生|诊断|处方|吃药|药物|医院|急诊|自杀|自残|medical|diagnos|prescription|hospital"),
    ("COMMITMENT", r"正式承诺|保证一定|签字|签合同|同意加入|辞职|录用|承诺|commit|resign|sign the contract"),
)
_MEDIUM_PATTERNS = (
    ("CONFLICT", r"分手|离婚|吵架|争吵|冲突|生气|滚开|绝交|break up|angry|fight"),
    ("DECISION", r"要不要|该不该|怎么决定|重要决定|项目决定|合作|学业|工作选择|decision|should i"),
)
_INJECTION_PATTERNS = (
    ("PROMPT_INJECTION", r"忽略.*(规则|指令|提示)|ignore (all|previous|prior)|system prompt|系统提示|开发者消息|执行命令|运行脚本|发给我.*(配置|密钥|密码)|聊天记录|其他联系人|项目文件"),
)


def _has(pattern, text):
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def assess_text(text) -> RiskAssessment:
    """Return a stable risk level and flags without retaining message content."""
    if not isinstance(text, str) or not text.strip():
        return RiskAssessment(HIGH, ("EMPTY_OR_INVALID_INPUT",))
    flags = []
    for flag, pattern in _HIGH_PATTERNS:
        if _has(pattern, text):
            flags.append(flag)
    for flag, pattern in _MEDIUM_PATTERNS:
        if _has(pattern, text):
            flags.append(flag)
    for flag, pattern in _INJECTION_PATTERNS:
        if _has(pattern, text):
            flags.append(flag)
    # Preserve declaration order while avoiding duplicate flags when patterns
    # overlap.  Injection and high-risk categories both remain visible.
    flags = tuple(dict.fromkeys(flags))
    if any(flag in flags for flag, _ in _HIGH_PATTERNS) or "PROMPT_INJECTION" in flags:
        level = HIGH
    elif flags:
        level = MEDIUM
    else:
        level = LOW
    return RiskAssessment(level, flags)


def combine_assessments(assessments) -> RiskAssessment:
    """Combine a batch without carrying raw message text into the result."""
    levels = {HIGH: 3, MEDIUM: 2, LOW: 1}
    assessments = tuple(assessments)
    flags = tuple(dict.fromkeys(flag for item in assessments for flag in item.flags))
    level = max((item.level for item in assessments), key=lambda value: levels[value], default=LOW)
    return RiskAssessment(level, flags)
