import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daily_plan_mock.planner.models import State
from daily_plan_mock.planner.service import DailyPlanService, MockTransport
from daily_plan_orchestration.service import OrchestrationAdapter, Phase19Orchestrator
from daily_plan_orchestration.session_meta import SessionMetaStore
from daily_plan_orchestration.formatter import format_draft
from daily_plan_fusion.engine import FusionEngine
from daily_plan_fusion.models import FusionItemInput, FusionRequest, Source
from daily_plan_proactive.wechat_bridge import ProactiveInboundBridge
from daily_plan_proactive.scheduler import FakeProactiveTransport, ProactiveScheduler
from daily_plan_wechat_integration.session_bridge import SessionBridge
from ilink_receive_probe.probe.models import Batch, InboundEvent
from ilink_receive_probe.probe.receiver import Receiver
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.reply_transport import FakeReplyOnlyTransport
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.transport import FakeIlinkTransport
from daily_plan_recommendation.models import Energy
from daily_plan_roadmap.storage import RoadmapProgressStore
from daily_plan_roadmap.task_source import RoadmapTaskSource
from daily_plan_reading.source import ReadingRecommendationSource


TZ = timezone(timedelta(hours=8))
READING_CATALOG = Path(__file__).resolve().parents[2] / 'examples' / 'reading_catalog.example.json'


class UxHarness:
    def __init__(self, reading=False):
        self.root = Path(tempfile.mkdtemp())
        self.probe = ProbeStorage(self.root / 'probe.db')
        self.context = ReplyContextStore(self.root, 300, lambda: 0,
                                         lambda value: b'T:' + value[::-1],
                                         lambda value: value[2:][::-1])
        self.planner = DailyPlanService(self.root / 'planner.db', MockTransport())
        self.reply = FakeReplyOnlyTransport()
        self.bridge = SessionBridge(self.planner, self.probe, self.context,
                                     self.reply, self.root / 'bridge.db')
        self.meta = SessionMetaStore(self.root / 'meta.db')
        self.scheduler = ProactiveScheduler(self.root / 'reminder.db', FakeProactiveTransport())
        self.reading_source = ReadingRecommendationSource(READING_CATALOG) if reading else None
        self.orchestrator = Phase19Orchestrator(self.meta, '19:00-23:00', reading_source=self.reading_source)
        self.adapter = OrchestrationAdapter(self.bridge, self.orchestrator,
                                            ProactiveInboundBridge(self.scheduler, 'me', 'bot'))
        self.number = 0

    def close(self):
        self.bridge.close(); self.meta.close(); self.scheduler.close()
        self.planner.storage.close(); self.probe.close()

    def deliver(self, text):
        self.number += 1
        event = InboundEvent('acct', f'ux-{self.number}', 'me', 'now', text,
                             'bot', 'one_to_one', f'context-{self.number}',
                             f'recipient-{self.number}')
        receiver = Receiver(FakeIlinkTransport([Batch((event,), str(self.number))]),
                            self.probe, 'me', 'bot', context_store=self.context)
        receiver.receive_once()
        return self.adapter.accept_inbound(event, datetime(2026, 9, 7, 22, 10, tzinfo=TZ))


class UxRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.h = UxHarness(); self.addCleanup(self.h.close)

    def test_pending_start_accepts_begin_and_one_message_path(self):
        self.h.bridge.pending_start_context = True
        status = self.h.deliver('开始吧')
        self.assertEqual(status, 'ACKED_API')
        self.assertEqual(self.h.planner.storage.session()['state'], State.ASK_REVIEW.value)
        self.assertIn('先复盘今天', self.h.reply.calls[-1][1])

        self.h = UxHarness(); self.addCleanup(self.h.close)
        self.h.bridge.pending_start_context = True
        status = self.h.deliver('今天示例课程学到示例方法。明天上午满课，下午的课到四点，晚上大概4小时，精力一般。想继续往后学示例课程，Programming推进一点，Programming复习一下，还有一项普通作业。')
        self.assertEqual(status, 'ACKED_API')
        self.assertEqual(self.h.planner.storage.session()['state'], State.DRAFT_READY.value)
        bridge = self.h.bridge.store.session()
        self.assertEqual(json.loads(bridge['capacity_json'])['available_minutes'], 240)
        self.assertEqual(json.loads(bridge['capacity_json'])['energy'], '中')
        self.assertGreaterEqual(len(json.loads(bridge['arranged_json'])), 3)
        draft = self.h.orchestrator.last_draft
        self.assertTrue(any(item.source.value == 'USER_REQUIRED' for item in draft.items))
        self.assertTrue(any(item.source.value == 'USER_ARRANGED' for item in draft.items))
        self.assertIn('16:00', self.h.reply.calls[-1][1])

    def test_no_pending_start_context_does_not_start_on_begin_words(self):
        for text in ('开始吧', '开始', '来吧', '好'):
            harness = UxHarness(); self.addCleanup(harness.close)
            status = harness.deliver(text)
            self.assertEqual(status, 'IGNORED_NO_ACTIVE_SESSION')
            self.assertIsNone(harness.planner.storage.session())

    def test_reading_source_flows_through_existing_planner_and_confirmation(self):
        harness = UxHarness(reading=True); self.addCleanup(harness.close)
        harness.bridge.pending_start_context = True
        self.assertEqual(harness.deliver('开始吧'), 'ACKED_API')
        self.assertEqual(harness.deliver('今天主要学了示例课程。明天晚上大概6小时，精力一般。'), 'ACKED_API')
        session = harness.planner.storage.session()
        self.assertEqual(session['state'], State.DRAFT_READY.value)
        reading = next(item for item in harness.orchestrator.last_draft.items
                       if item.recommendation_origin == 'READING')
        self.assertEqual(reading.title, '继续阅读《示例书籍》')
        self.assertEqual(reading.estimated_minutes, 25)
        self.assertIn('【阅读】', harness.reply.calls[-1][1])
        self.assertIn('继续阅读《示例书籍》 25min', harness.reply.calls[-1][1])
        self.assertEqual(harness.reading_source.current_book_id, 'example-book-001')
        self.assertEqual(harness.reading_source.books()[0].status.value, 'READING')

    def test_reading_and_roadmap_coexist_in_natural_fused_draft(self):
        harness = UxHarness(reading=True); self.addCleanup(harness.close)
        roadmap = RoadmapProgressStore(harness.root / 'roadmap.db'); self.addCleanup(roadmap.close)
        roadmap_before = tuple(tuple(row) for row in roadmap.db.execute(
            'SELECT entity_type,entity_id,status,last_progress_at,started_at,completed_at FROM progress ORDER BY entity_type,entity_id'
        ).fetchall())
        harness.orchestrator = Phase19Orchestrator(
            harness.meta, '19:00-23:00',
            roadmap_source=RoadmapTaskSource(roadmap),
            reading_source=harness.reading_source,
        )
        harness.adapter = OrchestrationAdapter(harness.bridge, harness.orchestrator)
        harness.bridge.pending_start_context = True
        self.assertEqual(harness.deliver('开始吧'), 'ACKED_API')
        natural = ('今天主要学了示例课程。明天下午有示例课程，晚上大概8小时，'
                   '精力一般，想继续学示例课程，Programming推进一点，Programming复习一下，'
                   '还有一项普通作业。')
        self.assertEqual(harness.deliver(natural), 'ACKED_API')
        session = harness.planner.storage.session()
        draft = harness.orchestrator.last_draft
        self.assertEqual(harness.planner.storage.session()['state'], State.DRAFT_READY.value)
        self.assertTrue(any(item.source == Source.USER_REQUIRED for item in draft.items))
        self.assertTrue(any(item.source == Source.USER_ARRANGED for item in draft.items))
        self.assertTrue(any(item.recommendation_origin == 'ROADMAP' for item in draft.items))
        self.assertTrue(any(item.recommendation_origin == 'READING' for item in draft.items))
        self.assertIn('【路线推荐】', harness.reply.calls[-1][1])
        self.assertIn('【阅读】', harness.reply.calls[-1][1])
        self.assertIn('继续阅读《示例书籍》 25min', harness.reply.calls[-1][1])
        self.assertLessEqual(draft.total_committed_minutes + draft.total_recommended_minutes,
                             draft.available_minutes)
        roadmap_after = tuple(tuple(row) for row in roadmap.db.execute(
            'SELECT entity_type,entity_id,status,last_progress_at,started_at,completed_at FROM progress ORDER BY entity_type,entity_id'
        ).fetchall())
        self.assertEqual(roadmap_before, roadmap_after)
        draft_record = harness.meta.get(session['id'])
        self.assertEqual(harness.deliver('确认计划'), 'ACKED_API')
        self.assertEqual(harness.planner.storage.session()['state'], State.CONFIRMED.value)
        confirmed_record = harness.meta.get(session['id'])
        self.assertEqual(json.loads(draft_record['fused_json'])['items'],
                         json.loads(confirmed_record['confirmed_fused_json'])['items'])
        from daily_plan_shared_state.exporter import SharedStateExporter
        shared = harness.root / 'reading-shared-state'; shared.mkdir()
        exported = SharedStateExporter(shared).export_confirmed_fusion(
            harness.planner.storage, harness.orchestrator.last_draft,
            revision=confirmed_record['revision'], generated_at='fixed')
        shared_plan = json.loads(Path(exported['json']).read_text(encoding='utf-8'))['latest_confirmed_plan']
        self.assertTrue(any(item['title'] == '继续阅读《示例书籍》'
                            for item in shared_plan['system_recommendations']))
        self.assertEqual(harness.reading_source.books()[0].status.value, 'READING')

    def test_review_and_partial_natural_fields_use_existing_states(self):
        self.h.deliver('开始明日计划')
        self.h.deliver('今天学习了示例课程，学到示例方法；Programming 没推进。')
        self.assertEqual(self.h.planner.storage.session()['state'], State.ASK_CAPACITY.value)
        self.assertNotIn('当前步骤要求的明确格式', self.h.reply.calls[-1][1])
        self.h.deliver('晚上大概4小时')
        self.assertEqual(self.h.planner.storage.session()['state'], State.ASK_CAPACITY.value)
        self.h.deliver('感觉明天没那么有精力')
        self.assertEqual(self.h.planner.storage.session()['state'], State.DRAFT_READY.value)

    def test_complete_natural_sequence_never_requires_colon_commands(self):
        self.h.deliver('开始明日计划')
        self.h.deliver('今天学习了示例课程，学到示例方法；Programming 没推进。')
        self.h.deliver('明天示例课程继续往后看，Programming推进一点，Programming也复习一下，还有一项普通作业。')
        self.h.deliver('明天上午满课，下午的课到四点。')
        self.h.deliver('晚上大概4小时')
        self.h.deliver('状态一般')
        self.assertEqual(self.h.planner.storage.session()['state'], State.DRAFT_READY.value)
        self.assertTrue(any('示例课程继续' in item.title for item in self.h.orchestrator.last_draft.items))
        self.assertNotIn('当前步骤要求的明确格式', '\n'.join(text for _, text in self.h.reply.calls))

    def test_draft_and_confirmed_use_same_fused_snapshot(self):
        self.h.deliver('开始明日计划')
        self.h.deliver('完成：x\n顺延：复盘')
        self.h.deliver('必须：完成作业')
        self.h.deliver('固定：09:00 示例课程')
        self.h.deliver('可用时间：4小时\n精力：中')
        session = self.h.planner.storage.session(); sid = session['id']
        record = self.h.meta.get(sid)
        draft_payload = json.loads(record['fused_json'])
        draft_text = self.h.reply.calls[-1][1]
        self.assertEqual(draft_payload['state'], 'DRAFT_READY')
        self.assertTrue(record['fusion_hash'])
        self.h.deliver('确认计划')
        confirmed = self.h.meta.get(sid)
        confirmed_payload = json.loads(confirmed['confirmed_fused_json'])
        self.assertEqual(self.h.planner.storage.session()['state'], State.CONFIRMED.value)
        self.assertEqual({k: v for k, v in draft_payload.items() if k != 'state'},
                         {k: v for k, v in confirmed_payload.items() if k != 'state'})
        self.assertIn('已确认', self.h.reply.calls[-1][1])
        plan = self.h.planner.storage.db.execute('SELECT canonical_text FROM plans').fetchone()
        self.assertEqual(plan['canonical_text'], self.h.reply.calls[-1][1])

    def test_snapshot_hash_invalidates_after_input_change(self):
        self.h.deliver('开始明日计划'); self.h.deliver('完成：x'); self.h.deliver('必须：作业')
        self.h.deliver('固定：无'); self.h.deliver('可用时间：4小时\n精力：中')
        sid = self.h.planner.storage.session()['id']; old = self.h.meta.get(sid)['fusion_hash']
        self.h.deliver('修改可用时间：2小时\n精力：低')
        record = self.h.meta.get(sid)
        self.assertNotEqual(old, record['fusion_hash'])
        self.assertIsNone(record['confirmed_fused_json'])
        self.assertTrue(self.h.orchestrator.can_confirm(self.h.planner.storage.session(), self.h.bridge.store.session()))

    def test_overload_keeps_user_items_and_marks_deferred(self):
        request = FusionRequest('2026-09-08', '19:00-23:00', 90, Energy.NORMAL, (
            FusionItemInput('必须作业', Source.USER_REQUIRED, 60, kind='must_do'),
            FusionItemInput('示例课程继续', Source.USER_ARRANGED, 60, kind='self_arranged'),
        ))
        draft = FusionEngine().fuse(request)
        self.assertTrue(draft.overload)
        self.assertEqual(draft.total_recommended_minutes, 0)
        self.assertEqual({item.title for item in draft.deferred_items}, {'示例课程继续'})
        self.assertIn('未排入 / 后移', format_draft(draft).text)

    def test_same_natural_input_is_deterministic(self):
        text = '明天下午有示例课程，晚上大概3小时，今天示例课程还有两道题没做，状态一般。'
        from daily_plan_friendly_input import parse_friendly
        self.assertEqual(parse_friendly(text), parse_friendly(text))

    def test_confirmed_fused_snapshot_exports_to_shared_state(self):
        self.h.deliver('开始明日计划'); self.h.deliver('完成：x'); self.h.deliver('必须：作业')
        self.h.deliver('固定：无'); self.h.deliver('可用时间：4小时\n精力：中'); self.h.deliver('确认计划')
        from daily_plan_shared_state.exporter import SharedStateExporter
        out = self.h.root / 'shared-state'
        out.mkdir()
        record = self.h.meta.get(self.h.planner.storage.session()['id'])
        draft = self.h.orchestrator.confirmed_snapshot(self.h.planner.storage.session())
        result = SharedStateExporter(out).export_confirmed_fusion(
            self.h.planner.storage, draft, revision=record['revision'], generated_at='fixed')
        payload = json.loads(Path(result['json']).read_text(encoding='utf-8'))
        plan = payload['latest_confirmed_plan']
        self.assertEqual(plan['available_minutes'], 240)
        self.assertEqual(plan['total_recommendation_minutes'], draft.total_recommended_minutes)
        self.assertIn('固定事项：', Path(result['markdown']).read_text(encoding='utf-8'))

    def test_roadmap_metadata_survives_orchestration_confirmation(self):
        roadmap = RoadmapProgressStore(self.h.root / 'roadmap.db')
        self.addCleanup(roadmap.close)
        self.h.orchestrator = Phase19Orchestrator(self.h.meta, '19:00-23:00',
                                                   roadmap_source=RoadmapTaskSource(roadmap))
        self.h.adapter = OrchestrationAdapter(self.h.bridge, self.h.orchestrator)
        self.h.deliver('开始明日计划'); self.h.deliver('完成：x'); self.h.deliver('必须：作业')
        self.h.deliver('固定：无'); self.h.deliver('可用时间：4小时\n精力：中')
        draft_text = self.h.reply.calls[-1][1]
        self.assertIn('路线推荐', draft_text)
        route = next(item for item in self.h.orchestrator.last_draft.items
                     if item.recommendation_origin == 'ROADMAP')
        self.assertTrue(route.roadmap_task_id and route.roadmap_stage and route.roadmap_milestone)
        self.h.deliver('确认计划')
        self.assertIn('路线推荐', self.h.reply.calls[-1][1])
        self.assertIn(route.title, self.h.reply.calls[-1][1])

    def test_non_exact_confirmation_does_not_change_phase14(self):
        self.h.deliver('开始明日计划'); self.h.deliver('完成：x'); self.h.deliver('必须：作业')
        self.h.deliver('固定：无'); self.h.deliver('可用时间：4小时\n精力：中')
        self.h.deliver('确认')
        self.assertEqual(self.h.planner.storage.session()['state'], State.DRAFT_READY.value)


if __name__ == '__main__':
    unittest.main()
