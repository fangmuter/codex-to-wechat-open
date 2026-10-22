"""Small stdio-only metadata probe; it never reads credential files or starts a model turn."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AppServerMetadata:
    auth_status: str
    auth_type: str | None
    plan_type: str | None
    models: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]


class AppServerMetadataProbe:
    """Queries only account/read(refreshToken=false) and model/list over stdio."""
    def __init__(self, executable="codex", timeout_seconds=10):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def collect(self) -> AppServerMetadata:
        proc = subprocess.Popen([self.executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                encoding="utf-8", bufsize=1)
        lines: queue.Queue[str] = queue.Queue()
        def consume():
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
        reader = threading.Thread(target=consume, daemon=True); reader.start()
        def send(method, ident=None, params=None):
            message = {"method": method, "params": params or {}}
            if ident is not None: message["id"] = ident
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(message) + "\n"); proc.stdin.flush()
        try:
            send("initialize", 1, {"clientInfo": {"name": "daily_plan_phase15_preflight", "title": "Daily Plan Phase 1.5 Preflight", "version": "0.1"}})
            send("initialized")
            send("account/read", 2, {"refreshToken": False})
            send("model/list", 3)
            found: dict[int, dict] = {}; errors = []
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline and {2, 3} - found.keys():
                try: obj = json.loads(lines.get(timeout=0.2))
                except queue.Empty: continue
                except json.JSONDecodeError: continue
                if obj.get("id") in (2, 3): found[obj["id"]] = obj
            account = found.get(2, {}); model_list = found.get(3, {})
            if account.get("error"): errors.append("account/read:" + str(account["error"].get("code", "error")))
            if model_list.get("error"): errors.append("model/list:" + str(model_list["error"].get("code", "error")))
            details = account.get("result", {}).get("account") or {}
            auth_status = "OK" if details else ("ERROR" if account.get("error") else "UNKNOWN")
            raw_models = model_list.get("result", {}).get("data") or []
            models = tuple({key: item.get(key) for key in ("id", "defaultReasoningEffort", "supportedReasoningEfforts", "isDefault")} for item in raw_models if isinstance(item, dict))
            return AppServerMetadata(auth_status, details.get("type"), details.get("planType"), models, tuple(errors))
        finally:
            if proc.stdin: proc.stdin.close()
            proc.terminate()
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill()
