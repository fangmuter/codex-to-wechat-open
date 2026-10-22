"""Explicit-root, read-only Delivery Inbox scanner."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import DeliveryPlan
from .schema import DeliveryReadError, DeliverySchemaError, load_delivery_json


FILENAME = re.compile(r"^DAILY_PLAN_DELIVERY_[^/\\]+\.json$")


@dataclass(frozen=True)
class DeliveryInboxEntry:
    path: Path
    plan: DeliveryPlan | None
    error: str | None = None
    error_category: str | None = None


class DeliveryInbox:
    def __init__(self, root):
        self.root = Path(root)
        self.last_scan_diagnostics = {
            "resolved_inbox_path": str(self.root),
            "inbox_exists": False,
            "inbox_accessible": False,
            "json_file_count": 0,
        }

    def scan(self) -> tuple[DeliveryInboxEntry, ...]:
        try:
            root = self.root.resolve()
        except OSError:
            self.last_scan_diagnostics = {
                "resolved_inbox_path": str(self.root),
                "inbox_exists": False,
                "inbox_accessible": False,
                "json_file_count": 0,
            }
            return ()
        diagnostics = {
            "resolved_inbox_path": str(root),
            "inbox_exists": False,
            "inbox_accessible": False,
            "json_file_count": 0,
        }
        try:
            diagnostics["inbox_exists"] = root.exists()
            if not root.is_dir():
                self.last_scan_diagnostics = diagnostics
                return ()
        except OSError:
            self.last_scan_diagnostics = diagnostics
            return ()
        diagnostics["inbox_accessible"] = True
        try:
            paths = sorted(root.iterdir(), key=lambda value: value.name)
        except OSError:
            diagnostics["inbox_accessible"] = False
            self.last_scan_diagnostics = diagnostics
            return ()
        diagnostics["json_file_count"] = sum(
            1 for path in paths if FILENAME.match(path.name)
        )
        entries: list[DeliveryInboxEntry] = []
        for path in paths:
            if not FILENAME.match(path.name):
                continue
            try:
                if not path.is_file():
                    continue
            except OSError as exc:
                entries.append(DeliveryInboxEntry(
                    path, None, "delivery file unreadable", "UNREADABLE",
                ))
                continue
            try:
                candidate = path.resolve()
            except OSError:
                entries.append(DeliveryInboxEntry(
                    path, None, "delivery file unreadable", "UNREADABLE",
                ))
                continue
            try:
                candidate.relative_to(root)
            except ValueError:
                entries.append(DeliveryInboxEntry(
                    path, None, "path outside inbox root", "INVALID_SCHEMA",
                ))
                continue
            try:
                entries.append(DeliveryInboxEntry(path, load_delivery_json(candidate)))
            except DeliveryReadError as exc:
                entries.append(DeliveryInboxEntry(
                    path, None, str(exc)[:160], "UNREADABLE",
                ))
            except DeliverySchemaError as exc:
                message = str(exc)[:160]
                category = (
                    "WRONG_STATUS"
                    if message == "delivery plan is not CONFIRMED"
                    else "INVALID_SCHEMA"
                )
                entries.append(DeliveryInboxEntry(path, None, message, category))
        self.last_scan_diagnostics = diagnostics
        return tuple(entries)
