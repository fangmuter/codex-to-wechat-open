import re

_FIXED_TIME=re.compile(r'^(\d\d:\d\d\s*[–-]\s*\d\d:\d\d)\s+(.+)$')

def _items(value):
    return [part.strip().replace('\n',' ')[:120] for part in value.replace('；',';').split(';') if part.strip()][:3]

def review(text):
    values={}
    for line in text.splitlines():
        if '：' in line:
            key,value=line.split('：',1); values[key.strip()]=_items(value)
    return {'completed':values.get('完成',[]),'unfinished':values.get('未完成',[]),'carryover':values.get('顺延',[])}

def must(text):
    if not text.startswith('必须：'): return None
    return [{'title':item,'estimated_minutes':60} for item in _items(text[3:])]

def fixed(text):
    if text.strip()=='无': return []
    if not text.startswith('固定：'): return None
    values=[]
    for item in _items(text[3:]):
        match=_FIXED_TIME.match(item)
        values.append({'title':match.group(2),'fixed_time':match.group(1).replace('–','-')} if match else {'title':item})
    return values

def capacity(text):
    hours=re.search(r'可用时间：\s*(\d+)\s*小时',text)
    minutes=re.search(r'可用时间：\s*(\d+)\s*分钟',text)
    energy=re.search(r'精力：\s*(高|中|低)',text)
    if not (hours or minutes) or not energy: return None
    return {'available_minutes':int(hours.group(1))*60 if hours else int(minutes.group(1)),'energy':energy.group(1)}

def modification(text):
    if text.startswith('修改必须：'): return 'must',must('必须：'+text[5:])
    if text.startswith('修改固定：'): return 'fixed',fixed('固定：'+text[5:])
    if text.startswith('修改可用时间：'): return 'capacity',capacity('可用时间：'+text[7:])
    return None
