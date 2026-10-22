import unittest
from concurrent.futures import ThreadPoolExecutor

from chat_agent.runtime import (
    ChatAgentRuntime,
    RUNTIME_DISABLED,
    RUNTIME_POLLED,
    RUNTIME_STOPPED,
    RuntimeControlStore,
)


class _Adapter:
    def __init__(self):
        self.calls = []

    def process_once(self, *, now=None):
        self.calls.append(now)
        return "poll-result"


class RuntimeTests(unittest.TestCase):
    def test_runtime_is_disabled_until_explicit_enable(self):
        adapter = _Adapter()
        control = RuntimeControlStore()
        runtime = ChatAgentRuntime(adapter, control=control, sleep=lambda _seconds: None)
        self.addCleanup(runtime.close)
        self.addCleanup(control.close)
        self.assertEqual(runtime.run_once().state, RUNTIME_DISABLED)
        self.assertEqual(adapter.calls, [])
        runtime.enable()
        tick = runtime.run_once(now="t1")
        self.assertEqual(tick.state, RUNTIME_POLLED)
        self.assertEqual(adapter.calls, ["t1"])

    def test_independent_stop_switch_prevents_next_poll(self):
        adapter = _Adapter()
        control = RuntimeControlStore()
        runtime = ChatAgentRuntime(adapter, control=control)
        self.addCleanup(runtime.close)
        self.addCleanup(control.close)
        runtime.enable()
        runtime.request_stop()
        self.assertEqual(runtime.run_once().state, RUNTIME_STOPPED)
        self.assertEqual(adapter.calls, [])
        runtime.enable()
        self.assertEqual(runtime.run_once().state, RUNTIME_POLLED)
        self.assertEqual(len(adapter.calls), 1)

    def test_run_forever_does_not_start_when_disabled(self):
        adapter = _Adapter()
        control = RuntimeControlStore()
        runtime = ChatAgentRuntime(adapter, control=control, sleep=lambda _seconds: None)
        self.addCleanup(runtime.close)
        self.addCleanup(control.close)
        ticks = runtime.run_forever(max_seconds=1)
        self.assertEqual([tick.state for tick in ticks], [RUNTIME_DISABLED])
        self.assertEqual(adapter.calls, [])

    def test_control_store_can_be_toggled_from_a_ui_thread(self):
        control = RuntimeControlStore()
        self.addCleanup(control.close)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(control.set_enabled, True).result()
            self.assertTrue(pool.submit(lambda: control.enabled).result())
            pool.submit(control.request_stop).result()
            self.assertTrue(control.stop_requested)


if __name__ == "__main__":
    unittest.main()
