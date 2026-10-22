import unittest
from datetime import datetime, timedelta, timezone

from chat_agent.history import (
    ChatHistoryStore,
    ROLE_CONTACT,
    ROLE_OWNER,
    SensitiveManualEditRequired,
    build_prompt_messages,
)


UTC = timezone.utc


class HistoryTests(unittest.TestCase):
    def test_history_is_bounded_by_age_and_count(self):
        store = ChatHistoryStore(max_messages=3, max_age=timedelta(days=30))
        self.addCleanup(store.close)
        base = datetime(2026, 9, 10, 12, tzinfo=UTC)
        store.append_message("account", "contact", "old", "过期", occurred_at=base - timedelta(days=31))
        for index in range(4):
            store.append_message(
                "account", "contact", f"m{index}", f"消息{index}",
                occurred_at=base + timedelta(minutes=index),
            )
        store.append_message("account", "contact", "m3", "重复不会增加", occurred_at=base + timedelta(minutes=3))
        recent = store.recent("account", "contact", now=base + timedelta(minutes=4))
        self.assertEqual([item.message_id for item in recent], ["m1", "m2", "m3"])
        self.assertTrue(all(item.role == ROLE_CONTACT for item in recent))

    def test_manual_context_and_sensitive_confirmation(self):
        store = ChatHistoryStore()
        self.addCleanup(store.close)
        with self.assertRaises(SensitiveManualEditRequired):
            store.set_notes("account", "contact", ["私人立场"], sensitive=True)
        store.set_notes(
            "account", "contact", ["称呼对方为朋友", "不要主动承诺时间"],
            sensitive=True, second_confirmation=True,
        )
        store.add_style_example("account", "contact", "短句、自然、不用过多表情")
        self.assertEqual(len(store.notes("account", "contact")), 2)
        self.assertEqual(store.style_examples("account", "contact"), ("短句、自然、不用过多表情",))

    def test_prompt_has_previous_history_but_excludes_current_batch(self):
        store = ChatHistoryStore()
        self.addCleanup(store.close)
        now = datetime(2026, 9, 10, 12, tzinfo=UTC)
        store.append_message("account", "contact", "prior", "之前的消息", occurred_at=now)
        store.record_owner_message("account", "contact", "reply", "之前的回复", occurred_at=now)
        store.append_message("account", "contact", "current", "当前消息", occurred_at=now)
        store.set_notes("account", "contact", ["只作参考"])
        messages = build_prompt_messages(
            "系统约束",
            store,
            "account",
            "contact",
            [{"role": "user", "content": "当前消息"}],
            exclude_message_ids=("current",),
        )
        self.assertIn("<manual_note>只作参考</manual_note>", messages[0]["content"])
        self.assertEqual([item["content"] for item in messages[1:]], ["之前的消息", "之前的回复", "当前消息"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[2]["role"], "assistant")

    def test_prompt_never_crosses_contact_context(self):
        store = ChatHistoryStore()
        self.addCleanup(store.close)
        store.append_message("account", "contact-a", "a1", "A 的私密上下文")
        store.record_owner_message("account", "contact-a", "a2", "A 的回复")
        store.append_message("account", "contact-b", "b1", "B 的当前上下文")
        messages = build_prompt_messages(
            "系统约束", store, "account", "contact-b",
            [{"role": "user", "content": "B 的新消息"}],
        )
        rendered = "\n".join(item["content"] for item in messages)
        self.assertIn("B 的当前上下文", rendered)
        self.assertIn("B 的新消息", rendered)
        self.assertNotIn("A 的私密上下文", rendered)
        self.assertNotIn("A 的回复", rendered)

    def test_history_store_can_be_used_from_a_runtime_thread(self):
        from concurrent.futures import ThreadPoolExecutor

        store = ChatHistoryStore()
        self.addCleanup(store.close)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(store.append_message, "account", "contact", "thread-1", "消息").result()
            recent = pool.submit(store.recent, "account", "contact").result()
        self.assertEqual([item.message_id for item in recent], ["thread-1"])


if __name__ == "__main__":
    unittest.main()
