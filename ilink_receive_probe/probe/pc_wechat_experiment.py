"""Safe result model for the manual PC-WeChat dependency experiment.

This module does not control the desktop client and does not send anything.
An operator can record the bounded result of at most one receive probe and one
explicit smoke send in each client state after the client state is changed
manually.  Keeping the matrix as a typed object prevents a later report from
turning an unrun state into evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import subprocess
from typing import Any

from .proactive_send import sanitize_errmsg


class PcWeChatState(str, Enum):
    LOGGED_IN = "LOGGED_IN"
    LOGGED_OUT = "LOGGED_OUT"
    RELOGGED_IN = "RELOGGED_IN"


PC_WECHAT_RUNNING = "PC_WECHAT_RUNNING"
PC_WECHAT_NOT_RUNNING = "PC_WECHAT_NOT_RUNNING"


@dataclass(frozen=True)
class PcWeChatProcess:
    name: str
    pid: int
    start_time: str | None = None
    executable_path: str | None = None


@dataclass(frozen=True)
class PcWeChatProcessSnapshot:
    state: str
    process_count: int
    processes: tuple[PcWeChatProcess, ...] = ()


def detect_pc_wechat_processes(*, process_provider=None, timeout_seconds=3) -> PcWeChatProcessSnapshot:
    """Inspect exact WeChat process names without starting/stopping anything.

    ``process_provider`` is injectable for tests.  The native path uses a
    fixed PowerShell query and returns only process metadata needed by the
    experiment; it never accepts a process name or command from the caller.
    """
    if process_provider is not None:
        raw = process_provider()
    else:
        if __import__("os").name != "nt":
            raw = []
        else:
            command = (
                "$ErrorActionPreference='Stop'; "
                "$p=Get-CimInstance Win32_Process -Filter \"Name='WeChat.exe' OR Name='Weixin.exe'\"; "
                "if($p){$p | Select-Object Name,ProcessId,CreationDate,ExecutablePath | ConvertTo-Json -Compress}"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=timeout_seconds, check=True,
            )
            text = (completed.stdout or "").strip()
            raw = [] if not text else json.loads(text)
    if isinstance(raw, dict):
        raw = [raw]
    processes = []
    for item in raw or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or item.get("name") or "")
        if name.lower() not in {"wechat.exe", "weixin.exe"}:
            continue
        try:
            pid = int(item.get("ProcessId", item.get("pid")))
        except (TypeError, ValueError):
            continue
        processes.append(PcWeChatProcess(
            name=name, pid=pid,
            start_time=(str(item.get("CreationDate", item.get("start_time")))[:64]
                        if item.get("CreationDate", item.get("start_time")) else None),
            executable_path=(str(item.get("ExecutablePath", item.get("executable_path")))[:260]
                             if item.get("ExecutablePath", item.get("executable_path")) else None),
        ))
    processes.sort(key=lambda item: (item.name.lower(), item.pid))
    return PcWeChatProcessSnapshot(
        state=PC_WECHAT_RUNNING if processes else PC_WECHAT_NOT_RUNNING,
        process_count=len(processes), processes=tuple(processes),
    )


@dataclass(frozen=True)
class PcWeChatObservation:
    state: str
    receive_state: str
    send_state: str
    http_status: int | None = None
    ret: Any = None
    errcode: Any = None
    errmsg: str | None = None
    response_keys: tuple[str, ...] = ()
    account_match: bool | None = None
    bot_fingerprint: str | None = None
    user_visible: bool | None = None


class PcWeChatDependencyExperiment:
    """Collect the three manually controlled observations, once each."""

    def __init__(self):
        self._observations: dict[str, PcWeChatObservation] = {}

    @property
    def observations(self) -> tuple[PcWeChatObservation, ...]:
        return tuple(
            self._observations[item.value]
            for item in PcWeChatState
            if item.value in self._observations
        )

    def record(self, observation: PcWeChatObservation) -> None:
        if observation.state not in {item.value for item in PcWeChatState}:
            raise ValueError("unknown PC WeChat experiment state")
        if observation.state in self._observations:
            raise ValueError("each PC WeChat state allows one smoke observation")
        self._observations[observation.state] = PcWeChatObservation(
            state=observation.state,
            receive_state=str(observation.receive_state)[:80],
            send_state=str(observation.send_state)[:80],
            http_status=observation.http_status,
            ret=observation.ret,
            errcode=observation.errcode,
            errmsg=sanitize_errmsg(observation.errmsg),
            response_keys=tuple(sorted(str(key)[:80] for key in observation.response_keys))[:32],
            account_match=observation.account_match,
            bot_fingerprint=observation.bot_fingerprint,
            user_visible=observation.user_visible,
        )

    def verdict(self) -> str:
        required = {item.value for item in PcWeChatState}
        # A successful logged-in and fully-exited comparison is already
        # sufficient to say that the client-login dependency was not observed.
        # Relogin is only needed when the exited-state result is a failure.
        if not required.issubset(self._observations):
            logged_in = self._observations.get(PcWeChatState.LOGGED_IN.value)
            logged_out = self._observations.get(PcWeChatState.LOGGED_OUT.value)
            if (logged_in is not None and logged_out is not None
                    and logged_in.send_state == logged_out.send_state == "ACKED_API"):
                return "PC_WECHAT_LOGIN_DEPENDENCY_NOT_OBSERVED"
            return "PC_WECHAT_LOGIN_DEPENDENCY_INCONCLUSIVE"
        logged_in = self._observations[PcWeChatState.LOGGED_IN.value]
        logged_out = self._observations[PcWeChatState.LOGGED_OUT.value]
        relogged = self._observations[PcWeChatState.RELOGGED_IN.value]
        if (
            logged_out.send_state == "SESSION_PREPARE_FAILED"
            and logged_in.send_state == "ACKED_API"
            and relogged.send_state == "ACKED_API"
        ):
            return "PC_WECHAT_LOGIN_DEPENDENCY_CONFIRMED"
        if logged_in.send_state == logged_out.send_state == relogged.send_state:
            return "PC_WECHAT_LOGIN_DEPENDENCY_NOT_OBSERVED"
        return "PC_WECHAT_LOGIN_DEPENDENCY_INCONCLUSIVE"


__all__ = [
    "PcWeChatState", "PcWeChatObservation", "PcWeChatDependencyExperiment",
    "PcWeChatProcess", "PcWeChatProcessSnapshot", "detect_pc_wechat_processes",
    "PC_WECHAT_RUNNING", "PC_WECHAT_NOT_RUNNING",
]
