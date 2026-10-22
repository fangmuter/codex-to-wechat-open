from daily_plan_fusion.models import Source
from ilink_receive_probe.probe.reply_transport import RenderedReply

LABELS={Source.USER_FIXED:'【固定】',Source.USER_REQUIRED:'【必须完成】',Source.CARRYOVER:'【顺延的】',Source.USER_ARRANGED:'【你安排的】',Source.SYSTEM_RECOMMENDED:'【系统推荐的】'}

def _label(item):
    if item.source == Source.SYSTEM_RECOMMENDED and item.recommendation_origin == 'ROADMAP':
        return '【路线推荐】'
    if item.source == Source.SYSTEM_RECOMMENDED and item.recommendation_origin == 'READING':
        return '【阅读】'
    return LABELS[item.source]


def _groups(items):
    """Return stable display sections without interleaving source labels."""
    groups = []
    for source in (Source.USER_FIXED, Source.USER_REQUIRED,
                   Source.CARRYOVER, Source.USER_ARRANGED):
        values = [item for item in items if item.source == source]
        if values:
            groups.append((LABELS[source], values))

    recommendations = [item for item in items if item.source == Source.SYSTEM_RECOMMENDED]
    for origin, label in (("STATIC", "【系统推荐的】"),
                          ("ROADMAP", "【路线推荐】"),
                          ("READING", "【阅读】")):
        values = [item for item in recommendations
                  if (item.recommendation_origin or "STATIC") == origin]
        if values:
            groups.append((label, values))
    return groups


def format_draft(draft, state=None):
    lines=['明日计划草稿','']
    active_items = [item for item in draft.items if not item.deferred]
    for label, values in _groups(active_items):
        if lines[-1] != '':
            lines.append('')
        lines.append(label)
        for item in values:
            suffix=f' {item.estimated_minutes}min' if item.estimated_minutes else ''
            fixed=f'（{item.fixed_time}）' if item.fixed_time else ''
            unavailable=f'（{item.unavailable_until} 前不可安排）' if item.unavailable_until else ''
            lines.append(f'- {item.title}{suffix}{fixed}{unavailable}')
            if item.source==Source.SYSTEM_RECOMMENDED and item.reason:
                lines.append('  原因：'+item.reason)
    if draft.deferred_items:
        lines.extend(['','【未排入 / 后移】'])
        for item in draft.deferred_items:
            lines.append(f'- {item.title} {item.estimated_minutes}min（容量不足）')
    work=[x for x in draft.items if not x.deferred and x.source != Source.USER_FIXED]
    if work:
        lines.extend(['','执行顺序：'])
        for index,item in enumerate(work,1):
            period=item.preferred_period or (draft.planning_window or '规划窗口')
            lines.append(f'- 第{index}块（{period}）：{item.title} {item.estimated_minutes}min')
    lines.extend(['','容量：',f'- 净可用：{draft.available_minutes}min',f'- 用户事项占用：{draft.total_committed_minutes}min',f'- 系统推荐：{draft.total_recommended_minutes}min',f'- 缓冲：{draft.remaining_buffer_minutes}min'])
    for value in (*draft.conflicts,*draft.warnings): lines.append('警告：'+value)
    display_state=state or draft.state
    display_state={'CONFIRMED':'已确认','DRAFT_READY':'待确认'}.get(display_state, display_state)
    lines.extend(['',f'状态：{display_state}'])
    if display_state == '待确认':
        lines.extend(['如无问题，请回复：','确认计划'])
    return RenderedReply('\n'.join(lines))
