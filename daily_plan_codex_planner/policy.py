"""Hard limits owned by local code, never by WeChat input or a model."""

MAX_MODEL_CALLS_PER_SESSION = 4
MAX_CURRENT_USER_MESSAGE_CHARS = 2000
MAX_MODEL_INPUT_CHARS = 8000
MAX_MODEL_OUTPUT_CHARS = 3000
REASONING_EFFORT = "low"

# Stage A intentionally has no model allowlisted.  Stage B must replace this
# with one model explicitly approved by the user after the preflight.
ALLOWED_MODEL: str | None = None

LOCAL_COMMANDS = (
    "开始明日计划", "确认计划", "今晚跳过", "恢复计划", "暂停到 ",
    "修改必须：", "修改固定：", "修改可用时间：", "必须：", "固定：", "可用时间：",
)
