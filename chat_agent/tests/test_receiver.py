import unittest
import json
from tempfile import mktemp

from chat_agent.receiver import ChatAgentReceiver, WECHAT_UNAVAILABLE
from ilink_receive_probe.probe.models import Batch, InboundEvent
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.real_transport import RealIlinkTransport


class _Transport:
    def __init__(self, batches=None, errors=None):
        self.batches = list(batches or [])
        self.errors = list(errors or [])

    def getupdates(self, _cursor):
        if self.errors:
            raise self.errors.pop(0)
        return self.batches.pop(0) if self.batches else Batch((), "")


class _ContextStore:
    def store(self, event):
        return "stored-context-ref"


def event(message_id, text="你好", conversation="one_to_one", forwarded=False):
    return InboundEvent(
        "transport-account", message_id, "sender", "received", text, "bot",
        conversation, "raw-context", "raw-recipient", forwarded,
    )


class ChatAgentReceiverTests(unittest.TestCase):
    def make_storage(self):
        storage = ProbeStorage(mktemp(suffix=".db"))
        self.addCleanup(storage.close)
        return storage

    def test_receiver_isolated_scope_and_candidate_only_admission(self):
        text = event("m1")
        group = event("m2", conversation="group")
        forwarded = event("m3", forwarded=True)
        media_like = event("m4", text="")
        transport = _Transport([Batch((text, group, forwarded, media_like), "cursor-1")])
        storage = self.make_storage()
        receiver = ChatAgentReceiver(
            transport, storage, context_store=_ContextStore(), account_scope="chat-account"
        )
        accepted = receiver.receive_once()
        self.assertEqual([item.message_id for item in accepted], ["m1"])
        self.assertEqual(accepted[0].account_scope, "chat-account")
        self.assertEqual(storage.cursor(), "cursor-1")
        self.assertEqual(storage.inbound("chat-account", "m1")["status"], "candidate")
        self.assertEqual(storage.inbound("chat-account", "m1")["context_ref"], "stored-context-ref")
        self.assertEqual(storage.inbound("chat-account", "m2")["status"], "rejected")
        self.assertEqual(receiver.last_rejection_reasons, ("CONVERSATION_TYPE", "FORWARDED", "EMPTY_TEXT"))

    def test_unavailable_is_bounded_and_state_recovers(self):
        storage = self.make_storage()
        receiver = ChatAgentReceiver(
            _Transport([Batch((), "recovered")], errors=[TimeoutError()] * 4),
            storage,
            account_scope="chat-account",
        )
        self.assertEqual(receiver.receive_once(), [])
        self.assertEqual(receiver.state, WECHAT_UNAVAILABLE)
        self.assertEqual(receiver.receive_once(), [])
        self.assertEqual(receiver.state, "IDLE")

    def test_oserror_is_treated_as_transport_unavailable(self):
        storage = self.make_storage()
        receiver = ChatAgentReceiver(
            _Transport(errors=[OSError("network down")]),
            storage,
            account_scope="chat-account",
            retry_limit=0,
        )
        self.assertEqual(receiver.receive_once(), [])
        self.assertEqual(receiver.state, WECHAT_UNAVAILABLE)

    def test_retry_limit_must_not_be_negative(self):
        with self.assertRaises(ValueError):
            ChatAgentReceiver(_Transport(), self.make_storage(), retry_limit=-1)

    def test_missing_identity_is_not_a_pairing_candidate(self):
        storage = self.make_storage()
        malformed = InboundEvent(
            "transport-account", "m1", "", "received", "你好", "",
            "one_to_one", "raw-context", "raw-recipient",
        )
        receiver = ChatAgentReceiver(
            _Transport([Batch((malformed,), "cursor-1")]), storage,
            account_scope="chat-account",
        )
        self.assertEqual(receiver.receive_once(), [])
        self.assertEqual(storage.inbound("chat-account", "m1")["status"], "rejected")

    def test_real_transport_to_chat_receiver_is_scoped_and_candidate_only(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self):
                return json.dumps({
                    "msgs": [{
                        "message_id": "m-real", "from_user_id": "sender",
                        "to_user_id": "bot", "conversation_type": "one_to_one",
                        "item_list": [{"text_item": {"text": "本地假响应"}}],
                    }],
                    "get_updates_buf": "cursor-real",
                }).encode()

        transport = RealIlinkTransport(
            token="fake", account_scope="chat-agent", opener=lambda *_args, **_kwargs: Response()
        )
        storage = self.make_storage()
        receiver = ChatAgentReceiver(transport, storage, account_scope="chat-agent")
        accepted = receiver.receive_once()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].account_scope, "chat-agent")
        self.assertEqual(storage.cursor(), "cursor-real")
        self.assertEqual(storage.inbound("chat-agent", "m-real")["status"], "candidate")

    def test_deferred_cursor_replays_unacknowledged_event_after_receiver_restart(self):
        path = mktemp(suffix=".db")
        first_storage = ProbeStorage(path)
        event_batch = Batch((event("m-deferred"),), "cursor-deferred")
        first = ChatAgentReceiver(
            _Transport([event_batch]), first_storage,
            account_scope="chat-account", defer_cursor=True,
        )
        first_events = first.receive_once()
        self.assertEqual(len(first_events), 1)
        self.assertEqual(first_storage.cursor(), "")
        first_storage.close()

        second_storage = ProbeStorage(path)
        self.addCleanup(second_storage.close)
        second = ChatAgentReceiver(
            _Transport([event_batch]), second_storage,
            account_scope="chat-account", defer_cursor=True,
        )
        replay = second.receive_once()
        self.assertEqual([item.message_id for item in replay], ["m-deferred"])
        second.acknowledge(replay)
        self.assertEqual(second_storage.cursor(), "cursor-deferred")
        self.assertEqual(
            second_storage.inbound("chat-account", "m-deferred")["status"],
            "chat_agent_processed",
        )


if __name__ == "__main__":
    unittest.main()
