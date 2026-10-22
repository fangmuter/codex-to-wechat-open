def format_summary(document):
    plan=document.get('latest_confirmed_plan') or {}
    review=document.get('latest_review_summary') or {}
    roadmap=document.get('roadmap_summary') or {}
    lines=['# Daily Plan Shared Summary','', '## 最新确认计划']
    if not plan: lines.append('- 无已确认计划')
    else:
        lines += [f"- 日期：{plan.get('plan_date','未知')}",f"- 可用时间：{plan.get('available_minutes','未知')}min",f"- 精力：{plan.get('energy','未知')}"]
        for label,key in [('固定事项','user_fixed'),('必须事项','user_required'),('顺延事项','carryover'),('自主安排','user_arranged'),('路线推荐','roadmap_recommendations'),('系统推荐','system_recommendations'),('未排入/后移','deferred_items')]:
            lines.append(f'- {label}：'+('；'.join(x.get('title','') for x in plan.get(key,[])) or '无'))
    lines += ['', '## 最近复盘']
    for label,key in [('完成','completed_tasks'),('未完成','in_progress_tasks'),('阻塞','blocked_tasks'),('顺延','carryover_tasks')]: lines.append(f'- {label}：'+('；'.join(review.get(key,[])) or '无'))
    lines += ['', '## Roadmap']
    for label,key in [('活跃阶段','active_stages'),('活跃 milestone','active_milestones'),('最近完成','recently_completed_tasks'),('当前进行','active_tasks'),('下一可执行任务','next_eligible_tasks')]: lines.append(f'- {label}：'+('；'.join(roadmap.get(key,[])) or '无'))
    lines += ['', f"## 更新时间\n- {document.get('generated_at','未知')}"]
    return '\n'.join(lines)+'\n'
