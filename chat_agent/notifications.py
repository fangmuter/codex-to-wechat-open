"""Opt-in local notifications for newly reviewable drafts."""

from dataclasses import dataclass
import os
import subprocess
from typing import Protocol


@dataclass(frozen=True)
class NotificationEvent:
    draft_id: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class RecoveryNotification:
    missed_count: int


class NotificationSink(Protocol):
    def notify(self, event: NotificationEvent) -> bool:
        """Return whether a local notification was delivered."""


class NullNotificationSink:
    def notify(self, _event):
        return False


class CallbackNotificationSink:
    """Small adapter for a UI, test double, or user-owned notification app."""

    def __init__(self, callback):
        if not callable(callback):
            raise ValueError("notification callback must be callable")
        self.callback = callback

    def notify(self, event):
        self.callback(event)
        return True


class WindowsMessageNotificationSink:
    """Best-effort built-in Windows session notification via ``msg.exe``.

    It only announces that a draft is waiting; draft content and context
    references are never passed to the OS notification command. Failures are
    returned as ``False`` so notification availability cannot block REVIEW.
    """

    def __init__(
        self,
        *,
        runner=None,
        executable="msg.exe",
        timeout_seconds=5,
        platform_name=None,
    ):
        platform_name = platform_name or os.name
        if not str(platform_name).lower().startswith("nt") and platform_name != "Windows":
            raise RuntimeError("WindowsMessageNotificationSink requires Windows")
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("notification executable is required")
        if timeout_seconds <= 0:
            raise ValueError("notification timeout must be positive")
        self.runner = runner or subprocess.run
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def notify(self, event):
        if isinstance(event, RecoveryNotification):
            message = f"Chat Agent REVIEW\n网络恢复，已跳过 {event.missed_count} 条旧消息"
        else:
            short_id = event.draft_id[:12]
            message = f"Chat Agent REVIEW\n草稿 {short_id} 等待审核 ({event.status})"
        try:
            result = self.runner(
                [self.executable, "*", message],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return getattr(result, "returncode", 1) == 0
        except (OSError, subprocess.SubprocessError):
            return False
