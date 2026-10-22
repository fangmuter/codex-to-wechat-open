import json
from .models import Draft

def render(draft: Draft) -> str:
    lines=[f"日期：{draft.target_date}", "必须完成："]
    for i,t in enumerate(draft.must_do[:3],1): lines.append(f"{i}. {t.get('title','')}（{t.get('estimated_minutes',0)}分钟）")
    lines.append("固定安排：" + ("；".join(str(x.get('title','')) for x in draft.fixed_events) or "无"))
    lines.append("可选事项：" + ("；".join(str(x.get('title','')) for x in draft.optional) or "无"))
    lines.append(f"缓冲：{draft.buffer_minutes}分钟")
    if draft.overload: lines.append("超载提示：容量不足，建议删减、延后或缩小任务（由用户决定）")
    return "\n".join(lines)

def canonical_json(draft): return json.dumps(draft.to_dict(), ensure_ascii=False, sort_keys=True, separators=(',',':'))
