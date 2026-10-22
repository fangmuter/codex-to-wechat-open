from datetime import datetime, timedelta
from pathlib import Path
import json
import time

from daily_plan_mock.planner.service import DailyPlanService, MockTransport
from daily_plan_proactive.runtime import build_real_scheduler
from daily_plan_proactive.scheduler import TZ
from daily_plan_wechat_integration.session_bridge import SessionBridge
from ilink_receive_probe.probe.credential_store import CredentialStore
from ilink_receive_probe.probe.real_transport import RealIlinkTransport
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.reply_transport import ReplyOnlyTransport
from ilink_receive_probe.probe.receiver import Receiver
from ilink_receive_probe.probe.storage import ProbeStorage
from daily_plan_orchestration.service import OrchestrationAdapter, Phase19Orchestrator
from daily_plan_orchestration.session_meta import SessionMetaStore
from .lock import ProductionLock
from .logging import build_logger
from .paths import ProductionPaths
from daily_plan_roadmap.storage import RoadmapProgressStore
from daily_plan_roadmap.task_source import RoadmapTaskSource
from daily_plan_roadmap.drive_review_inbox import FILENAME, ReviewInboxError, import_inbox_file
from daily_plan_roadmap.review_sync import ReviewSyncError


def process_review_inbox(inbox_dir, roadmap_store):
    """Import immutable review files before constructing the recommendation source.

    The inbox is an optional dependency: unavailable/unreadable roots are NOOPs,
    while an invalid individual review is rejected without aborting the run.
    Files are processed in stable lexical filename order and are never changed.
    """
    root=Path(inbox_dir)
    summary={'status':'NOOP','files_seen':0,'imported_count':0,'noop_count':0,'rejected_count':0,'results':[]}
    if not root.is_dir():
        summary['reason']='inbox_unavailable'
        return summary
    try:
        files=sorted((p for p in root.iterdir() if p.is_file() and FILENAME.match(p.name)), key=lambda p:p.name)
    except OSError:
        summary['reason']='inbox_unreadable'
        return summary
    summary['status']='OK'; summary['files_seen']=len(files)
    for path in files:
        try:
            result=import_inbox_file(path,root,roadmap_store)
            state=result['status']
            summary['imported_count'] += state == 'IMPORTED'
            summary['noop_count'] += state == 'NOOP'
            summary['results'].append({'name':path.name,'status':state})
        except (ReviewInboxError,ReviewSyncError,OSError):
            summary['rejected_count'] += 1
            summary['results'].append({'name':path.name,'status':'REJECTED'})
    return summary


def build_production_parts():
    paths=ProductionPaths.current_user(); paths.root.mkdir(parents=True,exist_ok=True)
    credential=CredentialStore(); token=credential.load()
    metadata=json.loads(credential.meta.read_text(encoding='utf-8')) if credential.meta.exists() else {}
    scheduler,proactive_transport,ledger=build_real_scheduler(paths.scheduler_db,ledger_path=paths.delivery_ledger_db,key_namespace='phase16')
    inbound=ProbeStorage(paths.inbound_db)
    planner=DailyPlanService(paths.planner_db,MockTransport())
    bridge=SessionBridge(planner,inbound,ReplyContextStore(),ReplyOnlyTransport(token),paths.exchange_db)
    meta=SessionMetaStore(paths.metadata_db)
    roadmap_store=RoadmapProgressStore(paths.roadmap_db)
    review_summary=process_review_inbox(paths.review_inbox_dir,roadmap_store)
    orchestrator=Phase19Orchestrator(meta,planning_window=None,roadmap_source=RoadmapTaskSource(roadmap_store))
    adapter=OrchestrationAdapter(bridge,orchestrator)
    receiver=Receiver(RealIlinkTransport(token=token),inbound,metadata.get('account_fingerprint'),metadata.get('bot_fingerprint'),context_store=ReplyContextStore())
    return {'paths':paths,'scheduler':scheduler,'transport':proactive_transport,'ledger':ledger,'inbound':inbound,'planner':planner,'bridge':bridge,'meta':meta,'adapter':adapter,'receiver':receiver,'roadmap_store':roadmap_store,'review_summary':review_summary,'lock':ProductionLock(paths.lock_file),'logger':build_logger(paths.logs_dir)}


def close_production_parts(parts):
    parts['bridge'].close(); parts['planner'].storage.close(); parts['meta'].close(); parts['scheduler'].close(); parts['ledger'].close(); parts['inbound'].close(); parts['roadmap_store'].close(); parts['lock'].release()


def run_production(max_hours=4,poll_seconds=60):
    # Keep the historical builders importable for old preparation tests, but
    # the installed production action now delivers only an already-confirmed
    # web plan through the independent Delivery Inbox chain.
    from .delivery_runtime import run_delivery_production

    return run_delivery_production(max_hours, poll_seconds)
