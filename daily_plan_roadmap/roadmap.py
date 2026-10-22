from .models import RoadmapMilestone, RoadmapStage, RoadmapTask, Status


def load_entities(profile):
    stages=tuple(RoadmapStage(x['id'],x['title'],x['order'],tuple(x.get('prerequisites',())),Status(x.get('status','NOT_STARTED'))) for x in profile['stages'])
    milestones=tuple(RoadmapMilestone(x['id'],x['stage_id'],x['title'],tuple(x.get('prerequisite_ids',())),Status(x.get('status','NOT_STARTED')),tuple(x.get('recommended_task_ids',()))) for x in profile['milestones'])
    tasks=tuple(RoadmapTask(x['id'],x['milestone_id'],x['domain'],x['title'],int(x['estimated_minutes']),x['intensity'],tuple(x.get('prerequisite_task_ids',())),x.get('completion_criterion',''),tuple(x.get('tags',())),tuple(x.get('background_prerequisites',()))) for x in profile['tasks'])
    return stages,milestones,tasks
