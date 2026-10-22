"""Shared, bounded helpers for DPAPI-backed inbound context references."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping


def as_datetime(value: Any) -> datetime | None:
    """Normalize the timestamp forms emitted by the receive adapter."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def snapshot_context_refs(context_store: Any) -> frozenset[str]:
    """Snapshot only reference names; never decrypts or reads token blobs."""
    directory = Path(context_store.directory)
    try:
        refs = {
            path.stem
            for path in directory.glob("*.dat")
            if path.is_file()
            and path.with_name(path.name[:-4] + ".meta.json").is_file()
        }
    except OSError:
        return frozenset()
    return frozenset(refs)


def load_reply_context_record(context_store: Any, context_ref: str) -> Mapping[str, Any]:
    """Load one exact DPAPI context into memory with bounded timestamp checks."""
    data_path, metadata_path = context_store._paths(context_ref)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    created = as_datetime(metadata.get("created_at"))
    expires = as_datetime(metadata.get("expires_at"))
    if created is None or expires is None:
        raise ValueError("context timestamps are invalid")
    if metadata.get("used"):
        raise ValueError("context is already used")
    if not data_path.is_file():
        raise ValueError("context blob is unavailable")
    payload = context_store.load(context_ref)
    if not isinstance(payload, Mapping):
        raise ValueError("context payload is invalid")
    result = dict(payload)
    result["context_ref"] = context_ref
    result["created_at"] = created
    result["expires_at"] = expires
    return result


__all__ = ["as_datetime", "snapshot_context_refs", "load_reply_context_record"]
