"""Priority-preserving, capacity-safe merge around the Phase 1.7 engine."""
import re
from collections import Counter
from dataclasses import replace

from daily_plan_recommendation.engine import RecommendationEngine
from daily_plan_recommendation.models import Commitment, RecommendationRequest
from .models import PlanDraft, PlanItem, SOURCE_PRIORITY, Source


def _minutes(value):
    match=re.fullmatch(r"(\d\d):(\d\d)\s*[–-]\s*(\d\d):(\d\d)",value or "")
    if not match: return None
    start=int(match[1])*60+int(match[2]); end=int(match[3])*60+int(match[4])
    return (start,end) if end>start else None


class FusionEngine:
    @staticmethod
    def _commitments(items):
        return tuple(Commitment(x.title,x.estimated_minutes,x.kind,x.consumes_available_capacity,x.capacity_key) for x in items if x.source != Source.SYSTEM_RECOMMENDED)

    @staticmethod
    def _reserved(commitments):
        return RecommendationEngine._reserved_minutes(commitments)

    @staticmethod
    def _fixed_conflicts(items):
        timed=sorted((x for x in items if x.source==Source.USER_FIXED and _minutes(x.fixed_time)),key=lambda x:(x.fixed_time,x.title))
        conflicts=[]
        for index,left in enumerate(timed):
            a,b=_minutes(left.fixed_time)
            for right in timed[index+1:]:
                c,d=_minutes(right.fixed_time)
                if max(a,c)<min(b,d): conflicts.append(f"固定时间冲突：{left.title} 与 {right.title}")
        return tuple(conflicts)

    def fuse(self, request):
        user_items=tuple(x for x in request.items if x.source != Source.SYSTEM_RECOMMENDED)
        commitments=self._commitments(user_items); committed=self._reserved(commitments)
        remaining=max(0,request.available_minutes-committed); overload=committed>request.available_minutes
        warnings=[]
        if overload: warnings.append("用户事项占用已超过净可用时间；未加入系统推荐")
        planned=[PlanItem(x.title,x.source,x.estimated_minutes,SOURCE_PRIORITY[x.source],x.capacity_key,x.fixed_time,x.preferred_period,None,None,None,None,None,False,x.unavailable_until,x.consumes_available_capacity) for x in user_items]
        # Preserve every user item, while marking the lower-priority items that
        # cannot fit.  They remain visible under an explicit deferred section;
        # no user commitment is silently deleted.
        if overload:
            budget=request.available_minutes; seen=set(); marked=[]
            for item in planned:
                if not item.consumes_available_capacity:
                    marked.append(item); continue
                key=item.capacity_key if item.capacity_key is not None else ("unkeyed",len(marked))
                if key in seen or item.estimated_minutes <= budget:
                    if key not in seen:
                        seen.add(key); budget=max(0,budget-item.estimated_minutes)
                    marked.append(item)
                else:
                    marked.append(replace(item,deferred=True))
            planned=marked
        recommendations=()
        reading_item=None
        if not overload:
            rec_request=RecommendationRequest(request.date,request.available_minutes,request.energy,commitments,request.project_signals,request.progress,request.recently_recommended_domains,request.roadmap_candidates)
            recommendations=RecommendationEngine().recommend(rec_request).recommendations
            for rec in recommendations:
                planned.append(PlanItem(rec.title,Source.SYSTEM_RECOMMENDED,rec.estimated_minutes,rec.priority,None,None,None,rec.reason,rec.recommendation_origin,rec.roadmap_task_id,rec.roadmap_stage,rec.roadmap_milestone,False,None,True))
            # Reading is intentionally outside Phase 1.7 scoring.  It is an
            # optional light item and can only consume capacity left after all
            # scored recommendations; it never displaces user work.
            if request.reading_source is not None:
                total_scored=sum(x.estimated_minutes for x in recommendations)
                reading_budget=max(0,remaining-total_scored)
                reading_candidates=request.reading_source.candidates(reading_budget,limit=1)
                if reading_candidates:
                    book=reading_candidates[0]
                    reading_item=PlanItem(
                        f"继续阅读{book.title}", Source.SYSTEM_RECOMMENDED,
                        book.daily_minutes, 0, f"reading:{book.book_id}", None, None,
                        f"当前阅读书籍；每日阅读 {book.daily_minutes}min",
                        'READING', None, None, None, False, None, True,
                    )
                    planned.append(reading_item)
        total_recommended=sum(x.estimated_minutes for x in recommendations)
        if reading_item is not None:
            total_recommended+=reading_item.estimated_minutes
        # Stable user-source ordering preserves user input within an equal source priority.
        planned=tuple(sorted(enumerate(planned),key=lambda value:(SOURCE_PRIORITY[value[1].source],value[1].fixed_time or "",value[1].title,value[1].capacity_key or "",-value[1].priority,value[0])))
        planned=tuple(value for _,value in planned)
        buffer=max(0,request.available_minutes-committed-total_recommended)
        if total_recommended>remaining:
            raise AssertionError("system recommendations exceeded net capacity")
        breakdown=tuple(sorted(Counter(x.source.value for x in planned).items()))
        deferred=tuple(x for x in planned if x.deferred)
        return PlanDraft(request.date,request.planning_window,request.available_minutes,request.energy,planned,committed,total_recommended,buffer,overload,self._fixed_conflicts(user_items),tuple(warnings),breakdown,True,"DRAFT_READY",deferred)
