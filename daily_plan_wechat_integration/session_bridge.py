import hashlib
import json
import sqlite3

from daily_plan_mock.planner.models import State
from ilink_receive_probe.probe.reply_context import ContextExpired, ContextMissing
from ilink_receive_probe.probe.reply_transport import AmbiguousSendError, DefiniteSendError, UnconfirmedSendError
from . import command_parser as parser
from .response_renderer import DailyPlanResponseRenderer
from daily_plan_friendly_input import parse_friendly, to_phase14_operations


class BridgeStorage:
    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS bridge_session(
          id INTEGER PRIMARY KEY CHECK(id=1),active INTEGER NOT NULL,
          planner_session_id INTEGER,review_json TEXT,must_json TEXT,
          fixed_json TEXT,capacity_json TEXT,last_inbound_hash TEXT,
          arranged_json TEXT);
        CREATE TABLE IF NOT EXISTS exchanges(
          account_scope TEXT NOT NULL,message_id TEXT NOT NULL,
          state_before TEXT,state_after TEXT,reply_key TEXT,
          reply_api_status TEXT,PRIMARY KEY(account_scope,message_id));''')
        if 'arranged_json' not in {row['name'] for row in self.db.execute('PRAGMA table_info(bridge_session)')}:
            self.db.execute('ALTER TABLE bridge_session ADD COLUMN arranged_json TEXT')
        self.db.commit()

    def close(self):
        self.db.close()

    def session(self):
        return self.db.execute('SELECT * FROM bridge_session WHERE id=1').fetchone()

    def start(self, planner_id):
        self.db.execute('''INSERT OR REPLACE INTO bridge_session(
          id,active,planner_session_id,review_json,must_json,fixed_json,
          capacity_json,last_inbound_hash,arranged_json)
          VALUES(1,1,?,NULL,NULL,NULL,NULL,NULL,NULL)''', (planner_id,))
        self.db.commit()

    def update(self, **values):
        if not values:
            return
        self.db.execute('UPDATE bridge_session SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE id=1', list(values.values()))
        self.db.commit()

    def end(self):
        self.update(active=0)

    def exchange(self, scope, message_id):
        return self.db.execute('SELECT * FROM exchanges WHERE account_scope=? AND message_id=?', (scope, message_id)).fetchone()

    def add_exchange(self, scope, message_id, before, after, key, status):
        self.db.execute('INSERT INTO exchanges VALUES(?,?,?,?,?,?)', (scope, message_id, before, after, key, status))
        self.db.commit()

    def update_exchange(self, scope, message_id, status):
        self.db.execute('UPDATE exchanges SET reply_api_status=? WHERE account_scope=? AND message_id=?', (status, scope, message_id))
        self.db.commit()


class SessionBridge:
    def __init__(self, planner, probe_storage, context_store, reply_transport,
                 bridge_path=':memory:', renderer=None, draft_reply_builder=None,
                 confirmation_guard=None):
        self.planner = planner
        self.probe_storage = probe_storage
        self.context_store = context_store
        self.reply_transport = reply_transport
        self.store = BridgeStorage(bridge_path)
        self.renderer = renderer or DailyPlanResponseRenderer()
        self.draft_reply_builder = draft_reply_builder
        self.confirmation_guard = confirmation_guard
        self.confirmed_snapshot_builder = None
        self.confirmed_snapshot_committer = None
        # Production wiring sets this only after a valid proactive tick.  It
        # is intentionally false by default so free text cannot start a session.
        self.pending_start_context = False
        self.pending_start_checker = None

    def close(self):
        self.store.close()

    def _key(self, event):
        return hashlib.sha256(f'{event.account_scope}\0{event.message_id}\0phase14'.encode()).hexdigest()

    def _allowed(self, event):
        row = self.probe_storage.inbound(event.account_scope, event.message_id)
        return bool(row and row['status'] == 'accepted' and row['context_ref'])

    def _send(self, event, response, before, after):
        key = self._key(event)
        self.store.add_exchange(event.account_scope, event.message_id, before, after, key, 'PENDING')
        inbound = self.probe_storage.inbound(event.account_scope, event.message_id)
        try:
            context = self.context_store.load(inbound['context_ref'])
        except ContextExpired:
            self.store.update_exchange(event.account_scope, event.message_id, 'CONTEXT_EXPIRED')
            return 'CONTEXT_EXPIRED'
        except ContextMissing:
            self.store.update_exchange(event.account_scope, event.message_id, 'REJECTED')
            return 'REJECTED'
        try:
            self.reply_transport.reply_bound_rendered_context(context, key, response)
        except AmbiguousSendError:
            self.store.update_exchange(event.account_scope, event.message_id, 'UNKNOWN')
            return 'UNKNOWN'
        except UnconfirmedSendError:
            self.store.update_exchange(event.account_scope, event.message_id, 'DELIVERY_UNCONFIRMED')
            return 'DELIVERY_UNCONFIRMED'
        except DefiniteSendError:
            self.store.update_exchange(event.account_scope, event.message_id, 'FAILED')
            return 'FAILED'
        self.context_store.consume(inbound['context_ref'])
        self.store.update_exchange(event.account_scope, event.message_id, 'ACKED_API')
        return 'ACKED_API'

    def _draft_response(self):
        if self.draft_reply_builder:
            return self.draft_reply_builder(self.planner.storage.session(), self.store.session())
        result = json.loads(self.planner.storage.session()['draft_json'])
        return self.renderer.draft(__import__('daily_plan_mock.planner.renderer', fromlist=['render']).render(
            __import__('daily_plan_mock.planner.models', fromlist=['Draft']).Draft(**result)))

    def _capacity_complete(self, session):
        try:
            value = json.loads(session['capacity_json'] or '{}')
        except (TypeError, json.JSONDecodeError):
            return False
        return value.get('available_minutes') is not None and value.get('energy') in {'高', '中', '低'}

    def _draft_data(self, session):
        must = json.loads(session['must_json'] or '[]')
        fixed = json.loads(session['fixed_json'] or '[]')
        review = json.loads(session['review_json'] or '{}')
        capacity = json.loads(session['capacity_json'] or '{}')
        # Legacy Phase 1.4 review answers do not carry a duration.  Keep the
        # item visible and prioritized; only an explicit duration consumes the
        # net capacity (the fusion contract still deduplicates by key).
        optional = [{'title': title, 'estimated_minutes': 0, 'kind': 'carryover'}
                    for title in review.get('carryover', [])]
        return {'must_do': must, 'fixed_events': fixed, 'optional': optional,
                'available_minutes': capacity['available_minutes']}

    def _maybe_draft(self, now):
        session = self.store.session()
        if not session or not self._capacity_complete(session):
            return None
        self.planner.make_draft(self._draft_data(session), now)
        return self._draft_response()

    def _merge_friendly(self, result):
        ops = to_phase14_operations(result)
        current = self.store.session()
        current_review = json.loads(current['review_json'] or '{}')
        review = (ops.review if any(ops.review.get(key) for key in ('completed', 'unfinished', 'carryover'))
                  else current_review)
        values = {
            'review_json': json.dumps(review, ensure_ascii=False),
            'must_json': json.dumps(ops.must, ensure_ascii=False) if ops.must else (current['must_json'] or '[]'),
            'fixed_json': json.dumps(ops.fixed, ensure_ascii=False) if ops.fixed else (current['fixed_json'] or '[]'),
            'arranged_json': json.dumps(ops.arranged, ensure_ascii=False) if ops.arranged else (current['arranged_json'] or '[]'),
        }
        if ops.capacity is not None:
            previous = json.loads(current['capacity_json'] or '{}')
            merged = dict(previous)
            merged.update({key: value for key, value in ops.capacity.items() if value is not None})
            values['capacity_json'] = json.dumps(merged, ensure_ascii=False)
        elif current['capacity_json'] is None:
            values['capacity_json'] = None
        self.store.update(**values)

    def _friendly_meaningful(self, result):
        return bool(result.fixed_events or result.required_tasks or result.carryovers
                    or result.user_arranged_tasks or result.review_completed
                    or result.review_unfinished or result.available_minutes is not None
                    or result.energy is not None)

    def _next_after_natural(self, state):
        session = self.store.session()
        if self._capacity_complete(session):
            return State.DRAFT_READY
        if state == State.ASK_REVIEW:
            return State.ASK_CAPACITY
        # Missing fields are asked for one at a time; no fixed syntax is
        # required, and an explicit empty required/fixed answer remains valid.
        if not session['must_json']:
            return State.ASK_REQUIRED_RESULT
        if not session['fixed_json']:
            return State.ASK_FIXED_EVENTS
        return State.ASK_CAPACITY

    def _accept_natural(self, event, text, now, state):
        result = parse_friendly(text)
        if 'unsupported_command_or_path' in result.ambiguity_flags:
            return self._send(event, self.renderer.question('invalid'), state.value, state.value)
        if not self._friendly_meaningful(result):
            if result.ambiguity_flags:
                question = 'clarify_capacity' if 'available_time_ambiguous' in result.ambiguity_flags else 'clarify_energy' if 'energy_ambiguous' in result.ambiguity_flags else 'invalid'
                return self._send(event, self.renderer.question(question), state.value, state.value)
            return None
        # An ambiguous fragment is retained only when other typed facts are
        # usable; the existing clarification question handles the missing part.
        self._merge_friendly(result)
        # A natural answer may intentionally contain no required/fixed item.
        # Record that explicit empty answer so the state machine advances
        # instead of asking the same question again.
        current=self.store.session()
        if state == State.ASK_REQUIRED_RESULT and not current['must_json']:
            self.store.update(must_json='[]')
        if state == State.ASK_FIXED_EVENTS and not current['fixed_json']:
            self.store.update(fixed_json='[]')
        next_state = self._next_after_natural(state)
        if next_state == State.DRAFT_READY:
            response = self._maybe_draft(now)
            self.planner.storage.update_session(self.store.session()['planner_session_id'], state=State.DRAFT_READY.value, now=now.isoformat())
            return self._send(event, response, state.value, State.DRAFT_READY.value)
        self.planner.storage.update_session(self.store.session()['planner_session_id'], state=next_state.value, now=now.isoformat())
        question = 'must' if next_state == State.ASK_REQUIRED_RESULT else 'fixed' if next_state == State.ASK_FIXED_EVENTS else 'capacity'
        if next_state == State.ASK_CAPACITY:
            capacity = json.loads(self.store.session()['capacity_json'] or '{}')
            if capacity.get('available_minutes') is None and capacity.get('energy') in {'高', '中', '低'}:
                question = 'capacity_time'
            elif capacity.get('available_minutes') is not None and capacity.get('energy') is None:
                question = 'capacity_energy'
        return self._send(event, self.renderer.question(question), state.value, next_state.value)

    def _start_allowed(self, text, event, now):
        if text == '开始明日计划':
            return True
        if not self.pending_start_context and not (self.pending_start_checker and self.pending_start_checker(event, now)):
            return False
        if text in {'开始吧', '开始', '来吧', '好'}:
            return True
        result = parse_friendly(text)
        return self._friendly_meaningful(result) and 'unsupported_command_or_path' not in result.ambiguity_flags

    def handle(self, event, now):
        if self.probe_storage.setting('wechat_gateway_enabled', 'true') != 'true':
            return 'EMERGENCY_DISABLED'
        if not self._allowed(event):
            return 'REJECTED'
        if self.store.exchange(event.account_scope, event.message_id):
            return 'DUPLICATE_IGNORED'
        text = event.text.strip()
        bridge = self.store.session()
        planner_session = self.planner.storage.session()
        before = planner_session['state'] if planner_session else State.IDLE.value
        if text == '今晚跳过':
            self.planner.skip_tonight(now)
            if bridge:
                self.store.end()
            return self._send(event, self.renderer.question('skipped'), before, State.SKIPPED_TONIGHT.value)
        if text.startswith('暂停到 '):
            self.planner.pause(text[4:].strip())
            if bridge:
                self.store.end()
            return self._send(event, self.renderer.question('paused'), before, State.PAUSED_UNTIL_DATE.value)
        if text == '恢复计划':
            self.planner.resume()
            return self._send(event, self.renderer.question('resumed'), before, State.IDLE.value)

        if not bridge or not bridge['active']:
            if not self._start_allowed(text, event, now):
                self.context_store.consume(self.probe_storage.inbound(event.account_scope, event.message_id)['context_ref'])
                self.store.add_exchange(event.account_scope, event.message_id, before, before, None, 'IGNORED_NO_ACTIVE_SESSION')
                return 'IGNORED_NO_ACTIVE_SESSION'
            self.planner.trigger(now)
            sid = self.planner.storage.session()['id']
            self.store.start(sid)
            self.pending_start_context = False
            if text != '开始明日计划':
                # One-message fast path: the same natural text is processed as
                # ASK_REVIEW after the existing Phase 1.4 session is created.
                state = State.ASK_REVIEW
                return self._accept_natural(event, text, now, state) or self._send(event, self.renderer.question('review'), before, state.value)
            return self._send(event, self.renderer.question('review'), before, State.ASK_REVIEW.value)

        state = State(self.planner.storage.session()['state'])
        if state == State.ASK_REVIEW:
            # Preserve the established structured review path verbatim.
            if any(marker in text for marker in ('完成：', '未完成：', '顺延：', '必须：')):
                data = parser.review(text)
                self.store.update(review_json=json.dumps(data, ensure_ascii=False))
                self.planner.storage.update_session(bridge['planner_session_id'], state=State.ASK_REQUIRED_RESULT.value, now=now.isoformat())
                return self._send(event, self.renderer.question('must'), state.value, State.ASK_REQUIRED_RESULT.value)
            result = self._accept_natural(event, text, now, state)
            if result is not None:
                return result
            data = parser.review(text)
            self.store.update(review_json=json.dumps(data, ensure_ascii=False))
            self.planner.storage.update_session(bridge['planner_session_id'], state=State.ASK_REQUIRED_RESULT.value, now=now.isoformat())
            return self._send(event, self.renderer.question('must'), state.value, State.ASK_REQUIRED_RESULT.value)

        if state == State.ASK_REQUIRED_RESULT:
            data = parser.must(text)
            if data is not None:
                self.store.update(must_json=json.dumps(data, ensure_ascii=False))
                self.planner.storage.update_session(bridge['planner_session_id'], state=State.ASK_FIXED_EVENTS.value, now=now.isoformat())
                return self._send(event, self.renderer.question('fixed'), state.value, State.ASK_FIXED_EVENTS.value)
            result = self._accept_natural(event, text, now, state)
            if result is not None:
                return result
            return self._send(event, self.renderer.question('invalid'), state.value, state.value)

        if state == State.ASK_FIXED_EVENTS:
            data = parser.fixed(text)
            if data is not None:
                self.store.update(fixed_json=json.dumps(data, ensure_ascii=False))
                self.planner.storage.update_session(bridge['planner_session_id'], state=State.ASK_CAPACITY.value, now=now.isoformat())
                return self._send(event, self.renderer.question('capacity'), state.value, State.ASK_CAPACITY.value)
            result = self._accept_natural(event, text, now, state)
            if result is not None:
                return result
            return self._send(event, self.renderer.question('invalid'), state.value, state.value)

        if state == State.ASK_CAPACITY:
            data = parser.capacity(text)
            if data is not None:
                self.store.update(capacity_json=json.dumps(data, ensure_ascii=False))
                response = self._maybe_draft(now)
                self.planner.storage.update_session(bridge['planner_session_id'], state=State.DRAFT_READY.value, now=now.isoformat())
                return self._send(event, response, state.value, State.DRAFT_READY.value)
            result = self._accept_natural(event, text, now, state)
            if result is not None:
                return result
            return self._send(event, self.renderer.question('invalid'), state.value, state.value)

        if state == State.DRAFT_READY:
            change = parser.modification(text)
            if change:
                kind, value = change
                if value is None:
                    return self._send(event, self.renderer.question('invalid'), state.value, state.value)
                session = self.store.session()
                values = {'must_json': session['must_json'], 'fixed_json': session['fixed_json'], 'capacity_json': session['capacity_json'], 'arranged_json': session['arranged_json']}
                values[kind + '_json'] = json.dumps(value, ensure_ascii=False)
                self.store.update(**values)
                response = self._maybe_draft(now)
                return self._send(event, response, state.value, state.value)
            if text != '确认计划':
                return self._send(event, self.renderer.question('invalid'), state.value, state.value)
            if self.confirmation_guard and not self.confirmation_guard(self.planner.storage.session()):
                return self._send(event, self.renderer.question('invalid'), state.value, state.value)
            canonical_override = None
            if self.confirmed_snapshot_builder:
                try:
                    canonical_override = self.confirmed_snapshot_builder(self.planner.storage.session(), self.store.session())
                except ValueError:
                    return self._send(event, self.renderer.question('invalid'), state.value, state.value)
            result, pid, canonical = self.planner.confirm_local(now, canonical_override=canonical_override)
            if result == State.DRAFT_EXPIRED_AFTER_0200.value:
                return self._send(event, self.renderer.question('expired'), state.value, result)
            if result == State.CONFIRMED.value and self.confirmed_snapshot_committer:
                self.confirmed_snapshot_committer(self.planner.storage.session(), now)
                canonical = canonical_override or canonical
            self.store.end()
            return self._send(event, self.renderer.final(canonical), state.value, result)
        return 'IGNORED_NO_ACTIVE_SESSION'
