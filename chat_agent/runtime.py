"""Manual, fail-closed runtime control for the isolated Chat Agent."""

from dataclasses import dataclass
import sqlite3
import time
import threading

from .adapter import ChatAgentPollResult


RUNTIME_DISABLED = "RUNTIME_DISABLED"
RUNTIME_STOPPED = "RUNTIME_STOPPED"
RUNTIME_POLLED = "RUNTIME_POLLED"


class RuntimeControlStore:
    """Independent enabled/stop state; Daily Plan has no access to this DB."""

    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS runtime_control(
                 key TEXT PRIMARY KEY,
                 value TEXT NOT NULL)"""
        )
        self.db.executemany(
            "INSERT OR IGNORE INTO runtime_control(key,value) VALUES(?,?)",
            (("enabled", "0"), ("stop_requested", "0")),
        )
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def _get(self, key):
        with self._lock:
            row = self.db.execute("SELECT value FROM runtime_control WHERE key=?", (key,)).fetchone()
            return bool(row and row[0] == "1")

    def _set(self, key, value):
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO runtime_control(key,value) VALUES(?,?)",
                (key, "1" if value else "0"),
            )

    @property
    def enabled(self):
        return self._get("enabled")

    @property
    def stop_requested(self):
        return self._get("stop_requested")

    def set_enabled(self, enabled):
        self._set("enabled", bool(enabled))

    def request_stop(self):
        self._set("stop_requested", True)

    def clear_stop(self):
        self._set("stop_requested", False)


@dataclass(frozen=True)
class RuntimeTick:
    state: str
    poll: ChatAgentPollResult | None = None


class ChatAgentRuntime:
    """Poll an already-constructed adapter only after explicit enablement."""

    def __init__(
        self,
        adapter,
        *,
        control=None,
        poll_interval_seconds=2,
        sleep=time.sleep,
        route_controller=None,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.adapter = adapter
        self.control = control or RuntimeControlStore()
        self._owns_control = control is None
        self.poll_interval_seconds = poll_interval_seconds
        self.sleep = sleep
        self.route_controller = route_controller

    def close(self):
        if self._owns_control:
            self.control.close()

    def enable(self):
        self.control.clear_stop()
        self.control.set_enabled(True)
        if self.route_controller is not None:
            self.route_controller.enabled = True

    def disable(self):
        self.control.set_enabled(False)
        if self.route_controller is not None:
            self.route_controller.enabled = False

    def request_stop(self):
        self.control.request_stop()
        if self.route_controller is not None:
            self.route_controller.enabled = False

    def run_once(self, *, now=None):
        if self.control.stop_requested:
            return RuntimeTick(RUNTIME_STOPPED)
        if not self.control.enabled:
            return RuntimeTick(RUNTIME_DISABLED)
        return RuntimeTick(RUNTIME_POLLED, self.adapter.process_once(now=now))

    def run_forever(self, *, max_seconds=None, now=None):
        """Manual loop; it never enables itself and exits on the stop switch."""
        started = time.monotonic()
        ticks = []
        while True:
            tick = self.run_once(now=now)
            ticks.append(tick)
            if tick.state in (RUNTIME_DISABLED, RUNTIME_STOPPED):
                return tuple(ticks)
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                return tuple(ticks)
            self.sleep(self.poll_interval_seconds)
