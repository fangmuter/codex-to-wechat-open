"""Offline proof for receive-boundary event timestamps.

The wire protocol is allowed to omit a message timestamp.  Freshness for the
production context gate therefore comes from the local receive ingestion
boundary, not from a guessed ``now`` at validation time and not from the
short-lived context token timestamps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from daily_plan_production.context_gate import ProductionInboundContextGate
from ilink_receive_probe.probe.models import Batch, InboundEvent
from ilink_receive_probe.probe.receiver import Receiver
from ilink_receive_probe.probe.reply_context import ReplyContextStore
from ilink_receive_probe.probe.storage import ProbeStorage
from ilink_receive_probe.probe.transport import FakeIlinkTransport


START = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
SENDER = "sender-fingerprint"
BOT = "bot-fingerprint"
TOKEN = "SYNTHETIC_CONTEXT_TOKEN"


class FixedClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def seconds(self) -> float:
        return self.value.timestamp()


def event(message_id="m-1", *, received_at="", sender=SENDER, bot=BOT,
          token=TOKEN, conversation_type="one_to_one", forwarded=False):
    return InboundEvent(
        "acct", message_id, sender, received_at, "session probe", bot,
        conversation_type, token, "recipient", forwarded,
    )


def context_store(root: Path, clock: FixedClock) -> ReplyContextStore:
    return ReplyContextStore(
        root / "contexts", ttl_seconds=300, now=clock.seconds,
        protect=lambda data, unprotect=False: data if unprotect else b"P:" + data,
        unprotect=lambda data: data[2:],
    )


class ReceivedAtIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storages = []

    def tearDown(self):
        for storage in reversed(self.storages):
            storage.close()
        self.temp.cleanup()

    def make_receiver(self, raw_event, clock):
        storage = ProbeStorage(self.root / "inbound.db")
        self.storages.append(storage)
        contexts = context_store(self.root, clock)
        receiver = Receiver(
            FakeIlinkTransport([Batch((raw_event,), "cursor-1")]), storage,
            SENDER, BOT, context_store=contexts, clock=clock,
        )
        return receiver, storage, contexts

    def make_gate(self, storage, contexts, *, runner_started=START,
                  existing_refs=(), now=None):
        return ProductionInboundContextGate(
            runner_started_at=runner_started,
            expected_sender_fingerprint=SENDER,
            expected_bot_fingerprint=BOT,
            context_store=contexts,
            context_ref_for_event=lambda item: (
                self._context_ref(storage, item)
            ),
            existing_context_refs=existing_refs,
            now=now or (lambda: START + timedelta(seconds=2)),
        )

    @staticmethod
    def _context_ref(storage, item):
        row = storage.inbound(item.account_scope, item.message_id)
        return row["context_ref"] if row is not None else None

    def test_missing_wire_timestamp_is_filled_at_receive_boundary(self):
        clock = FixedClock(START + timedelta(seconds=2))
        receiver, storage, _ = self.make_receiver(event(), clock)
        accepted = receiver.receive_once()
        self.assertEqual(len(accepted), 1)
        row = storage.inbound("acct", "m-1")
        self.assertEqual(row["received_at"], "2026-09-16T14:00:02+00:00")
        self.assertEqual(accepted[0].received_at, row["received_at"])
        self.assertTrue(datetime.fromisoformat(row["received_at"]).tzinfo)

    def test_injected_clock_is_deterministic_and_normalizes_to_utc(self):
        clock = FixedClock(datetime(2026, 9, 16, 22, 0, tzinfo=timezone(timedelta(hours=8))))
        receiver, storage, _ = self.make_receiver(event(), clock)
        receiver.receive_once()
        self.assertEqual(
            storage.inbound("acct", "m-1")["received_at"],
            "2026-09-16T14:00:00+00:00",
        )

    def test_timestamp_survives_storage_roundtrip(self):
        clock = FixedClock(START + timedelta(seconds=3))
        path = self.root / "roundtrip.db"
        storage = ProbeStorage(path)
        self.storages.append(storage)
        receiver = Receiver(
            FakeIlinkTransport([Batch((event(),), "cursor-1")]), storage,
            SENDER, BOT, clock=clock,
        )
        receiver.receive_once()
        stored = storage.inbound("acct", "m-1")["received_at"]
        storage.close()
        self.storages.remove(storage)
        reopened = ProbeStorage(path)
        self.storages.append(reopened)
        self.assertEqual(reopened.inbound("acct", "m-1")["received_at"], stored)

    def test_post_runtime_event_with_ingestion_timestamp_is_accepted(self):
        clock = FixedClock(START + timedelta(seconds=2))
        receiver, storage, contexts = self.make_receiver(event(), clock)
        accepted = receiver.receive_once()
        gate = self.make_gate(storage, contexts, now=clock)
        self.assertTrue(gate.accept(accepted, now=clock()))
        self.assertIsNotNone(gate.binding)

    def test_legacy_empty_timestamp_is_rejected_without_now_fallback(self):
        clock = FixedClock(START + timedelta(seconds=2))
        storage = ProbeStorage(self.root / "legacy.db")
        self.storages.append(storage)
        contexts = context_store(self.root, clock)
        gate = self.make_gate(storage, contexts, now=clock)
        for message_id, missing in (("legacy-empty", ""), ("legacy-null", None)):
            legacy = event(message_id, received_at=missing)
            storage.save_event(legacy, "accepted")
            ref = contexts.store(legacy)
            storage.set_context_ref("acct", message_id, ref)
            self.assertFalse(gate.accept((legacy,), now=clock()))
            self.assertEqual(gate.last_rejection, "INBOUND_TIMESTAMP_UNAVAILABLE")

    def test_pre_runtime_event_is_rejected(self):
        clock = FixedClock(START + timedelta(seconds=2))
        storage = ProbeStorage(self.root / "pre-runtime.db")
        self.storages.append(storage)
        contexts = context_store(self.root / "pre-runtime-context", clock)
        # The receive boundary is authoritative for a newly received event;
        # simulate a persisted pre-runtime legacy event for this gate check.
        pre_runtime = InboundEvent(
            "acct", "pre", SENDER, (START - timedelta(seconds=1)).isoformat(),
            "session probe", BOT, "one_to_one", TOKEN, "recipient", False,
        )
        ref = contexts.store(pre_runtime)
        storage.save_event(pre_runtime, "accepted")
        storage.set_context_ref("acct", "pre", ref)
        gate = self.make_gate(storage, contexts, now=clock)
        self.assertFalse(gate.accept((pre_runtime,), now=clock()))
        self.assertEqual(gate.last_rejection, "EVENT_BEFORE_RUNNER_START")

    def test_post_runtime_wrong_sender_and_group_are_rejected(self):
        clock = FixedClock(START + timedelta(seconds=2))
        for item, expected in (
            (event("wrong", received_at=(START + timedelta(seconds=2)).isoformat(), sender="other"), "SENDER_MISMATCH"),
            (event("group", received_at=(START + timedelta(seconds=2)).isoformat(), conversation_type="group"), "DIRECT_MESSAGE_REQUIRED"),
        ):
            storage = ProbeStorage(self.root / (item.message_id + ".db"))
            self.storages.append(storage)
            contexts = context_store(self.root / item.message_id, clock)
            receiver = Receiver(
                FakeIlinkTransport([Batch((item,), "cursor")]), storage,
                SENDER, BOT, context_store=contexts, clock=clock,
            )
            receiver.receive_once()
            gate = self.make_gate(storage, contexts, now=clock)
            self.assertFalse(gate.accept((item,), now=clock()))
            self.assertEqual(gate.last_rejection, expected)

    def test_old_context_reference_is_not_reused(self):
        clock = FixedClock(START + timedelta(seconds=2))
        receiver, storage, contexts = self.make_receiver(event(), clock)
        accepted = receiver.receive_once()
        ref = storage.inbound("acct", "m-1")["context_ref"]
        gate = self.make_gate(storage, contexts, existing_refs=(ref,), now=clock)
        self.assertFalse(gate.accept(accepted, now=clock()))
        self.assertEqual(gate.last_rejection, "OLD_CONTEXT_IGNORED")

    def test_context_ttl_is_independent_from_event_timestamp(self):
        clock = FixedClock(START + timedelta(seconds=2))
        receiver, storage, contexts = self.make_receiver(event(), clock)
        accepted = receiver.receive_once()
        ref = storage.inbound("acct", "m-1")["context_ref"]
        metadata = __import__("json").loads(
            contexts._paths(ref)[1].read_text(encoding="utf-8")
        )
        self.assertNotEqual(metadata["created_at"], accepted[0].received_at)
        self.assertEqual(metadata["expires_at"] - metadata["created_at"], 300)


if __name__ == "__main__":
    unittest.main()
