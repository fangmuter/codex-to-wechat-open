from datetime import datetime, timezone, timedelta
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from daily_plan_mock.planner.service import DailyPlanService

try: tz=ZoneInfo('Asia/Shanghai')
except ZoneInfoNotFoundError: tz=timezone(timedelta(hours=8),'Asia/Shanghai')
s=DailyPlanService(); now=datetime(2026,9,2,22,0,tzinfo=tz)
print(s.scheduler.tick(now));
for i,text in enumerate(['复盘','明天完成报告','10点会议','顺延报告','可用4小时'],1): print(s.receive('me',str(i),{'text':text},now))
s.receive('me','6',{'text':'草稿','draft':{'must_do':[{'title':'报告','estimated_minutes':60}], 'available_minutes':240}},now)
print(s.receive('me','7',{'text':'确认计划'},now)); print('partial retry:', s.deliver(s.storage.plan(s.storage.session()['id'])['plan_id']) if False else 'see tests')
