from ilink_receive_probe.probe.reply_transport import RenderedReply

QUESTIONS={
    'review':'先复盘今天：今天有哪些事情完成了，哪些没有完成？需要顺延的事项请明确写“顺延”。',
    'must':'明天最必须完成的结果是什么？最多写 3 项。',
    'fixed':'明天有哪些固定行程或截止时间？没有请回复“无”。',
    'capacity':'明天实际可用时间和精力怎么样？',
    'invalid':'请使用当前步骤要求的明确格式。',
    'clarify_capacity':'可用时间还不明确，请告诉我明天大约有多少分钟或几小时。',
    'clarify_energy':'精力还不明确，请告诉我明天是低、一般还是高。',
    'capacity_time':'还差可用时间：请告诉我明天大约有多少分钟或几小时。',
    'capacity_energy':'还差精力：请告诉我明天是低、一般还是高。',
    'expired':'本轮草稿已在 02:00 后过期，请重新发送“开始明日计划”。',
    'skipped':'今晚已跳过；不会发送任何主动提醒。',
    'paused':'计划已暂停；不会发送任何主动提醒。',
    'resumed':'计划已恢复；请发送“开始明日计划”开始新一轮。',
}

class DailyPlanResponseRenderer:
    def question(self,name): return RenderedReply(QUESTIONS[name])
    def draft(self,canonical): return RenderedReply(canonical+'\n\n如无修改，请精确回复“确认计划”。')
    def final(self,canonical): return RenderedReply(canonical)
