"""Explainable deterministic scoring.  Inputs are explicit, no network or AI."""
from .models import Energy, Recommendation, RecommendationRequest, RecommendationResult

PRIORITY_ORDER=("Coursework","Foundations","Projects","Programming","Modeling","Research","Review")

CATALOG={
    "Coursework": (("Review a short set of course notes",30,"LIGHT"),("Complete a focused course review",90,"DEEP")),
    "Foundations": (("Practice a small set of fundamentals",30,"LIGHT"),("Study one foundational topic",90,"DEEP")),
    "Projects": (("Clarify the next project step",30,"LIGHT"),("Complete one project milestone",90,"DEEP")),
    "Programming": (("Complete a short programming exercise",30,"LIGHT"),("Implement a focused programming task",90,"DEEP")),
    "Modeling": (("Reproduce a small model example",30,"LIGHT"),("Complete a focused modeling exercise",90,"DEEP")),
    "Research": (("Read and summarize a short technical section",30,"LIGHT"),("Compare approaches to a technical question",90,"DEEP")),
    "Review": (("Review recent notes",30,"LIGHT"),("Consolidate a set of recent findings",90,"DEEP")),
}


class RecommendationEngine:
    @staticmethod
    def _reserved_minutes(commitments):
        """Deduct only work inside the net available window, once per capacity key."""
        reserved=0; seen=set()
        for index,item in enumerate(commitments):
            if not item.consumes_available_capacity: continue
            key=item.capacity_key if item.capacity_key is not None else ("unkeyed",index)
            if key in seen: continue
            seen.add(key); reserved+=max(0,item.estimated_minutes)
        return reserved

    def _score(self, domain, request):
        rank=PRIORITY_ORDER.index(domain)
        signals={x.domain:x for x in request.project_signals}; progress={x.domain:x for x in request.progress}
        signal=signals.get(domain); stale=min(progress.get(domain).days_since_progress if domain in progress else 0,30)
        score=(len(PRIORITY_ORDER)-rank)*20 + stale*3
        if signal: score += signal.urgency*12 + (10 if signal.course_related else 0)
        if domain in request.recently_recommended_domains: score -= 55
        return score, stale, signal

    def _reason(self, domain, rank, stale, signal, candidate=None):
        pieces=[f"长期优先级第{rank+1}"]
        if stale: pieces.append(f"已 {stale} 天未推进")
        if signal and signal.urgency: pieces.append(f"当前项目紧迫度 {signal.urgency}")
        if signal and signal.course_related: pieces.append("与当前课程直接相关")
        if candidate: pieces.insert(0,f"路线阶段 {candidate.roadmap_stage} / milestone {candidate.roadmap_milestone} 的下一可执行任务")
        return "；".join(pieces)

    def recommend(self, request: RecommendationRequest):
        reserved=self._reserved_minutes(request.commitments)
        remaining=max(0,request.available_minutes-reserved)
        if remaining<30: return RecommendationResult(reserved,remaining)
        ranked=[]
        duplicate_ids={getattr(x,'canonical_task_id',None) or x.task_id for x in request.roadmap_candidates}
        for rank,domain in enumerate(PRIORITY_ORDER):
            if f'static:{domain}:DEEP' in duplicate_ids: continue
            score,stale,signal=self._score(domain,request)
            ranked.append((score,rank,domain,stale,signal,None))
        # Roadmap candidates enter the same score stream; a roadmap item for a
        # domain replaces that domain's static catalog item (stable identity).
        for candidate in request.roadmap_candidates:
            domain=candidate.domain; rank=PRIORITY_ORDER.index(domain) if domain in PRIORITY_ORDER else len(PRIORITY_ORDER)
            stale=candidate.days_since_progress if candidate.days_since_progress is not None else 2
            signal={x.domain:x for x in request.project_signals}.get(domain)
            score=(len(PRIORITY_ORDER)-rank)*20 + min(stale,30)*3 + 6
            if signal: score += signal.urgency*12 + (10 if signal.course_related else 0)
            if domain in request.recently_recommended_domains: score -= 55
            ranked.append((score,rank,domain,stale,signal,candidate))
        ranked.sort(key=lambda value:(-value[0],value[1],value[2],getattr(value[-1],'task_id','')))
        chosen=[]; budget=remaining
        # 30--60 minutes never gets a deep task; low energy also never gets one.
        allow_deep=request.energy != Energy.LOW and remaining>=90
        focus_limit=2 if remaining>=120 and request.energy != Energy.LOW else (1 if allow_deep else 0)
        for entry in ranked:
            score,rank,domain,stale,signal,candidate=entry
            if len([x for x in chosen if x.intensity=="DEEP"])>=focus_limit: break
            title,minutes,intensity=((candidate.title,candidate.estimated_minutes,candidate.intensity) if candidate else CATALOG[domain][1])
            if minutes<=budget:
                chosen.append(Recommendation(title,domain,minutes,score,self._reason(domain,rank,stale,signal,candidate),intensity=intensity,recommendation_origin='ROADMAP' if candidate else 'STATIC',roadmap_task_id=getattr(candidate,'task_id',None),roadmap_stage=getattr(candidate,'roadmap_stage',None),roadmap_milestone=getattr(candidate,'roadmap_milestone',None))); budget-=minutes
        # Add at most one light task, and never from a selected focus domain.
        if budget>=30 and len(chosen)<3:
            for entry in ranked:
                score,rank,domain,stale,signal,candidate=entry
                if any(x.domain==domain for x in chosen): continue
                title,minutes,intensity=((candidate.title,min(candidate.estimated_minutes,30),'LIGHT') if candidate else CATALOG[domain][0])
                if minutes<=budget:
                    chosen.append(Recommendation(title,domain,minutes,score,self._reason(domain,rank,stale,signal,candidate),intensity=intensity,recommendation_origin='ROADMAP' if candidate else 'STATIC',roadmap_task_id=getattr(candidate,'task_id',None),roadmap_stage=getattr(candidate,'roadmap_stage',None),roadmap_milestone=getattr(candidate,'roadmap_milestone',None))); break
        # With 30--60 capacity this yields exactly one light task at most.
        return RecommendationResult(reserved,remaining,tuple(chosen))
