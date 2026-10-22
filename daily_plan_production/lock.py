"""Ownership-safe, stale-aware lock for the bounded production runner."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


_RUNNER_MARKERS = ("daily_plan_production", "run_production.py")


def _production_process_probe(pid: int) -> dict[str, object]:
    """Return liveness and an affirmative production-runner check.

    Process command-line inspection is deliberately best-effort.  An alive
    process that cannot be positively identified is *not* treated as stale.
    """
    if pid == os.getpid():
        return {"alive": True, "valid": True}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"alive": False, "valid": False}
    except PermissionError:
        return {"alive": True, "valid": False}
    except OSError:
        return {"alive": False, "valid": False}

    command = (
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = %d' "
        "-ErrorAction SilentlyContinue; "
        "if($p){[pscustomobject]@{ExecutablePath=$p.ExecutablePath;"
        "CommandLine=$p.CommandLine}|ConvertTo-Json -Compress}"
    ) % pid
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=2, check=False,
        )
        raw = completed.stdout.strip()
        details = json.loads(raw) if completed.returncode == 0 and raw else None
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        details = None
    if not isinstance(details, dict):
        return {"alive": True, "valid": False}
    executable = str(details.get("ExecutablePath") or "")
    command_line = str(details.get("CommandLine") or "")
    haystack = (executable + " " + command_line).lower().replace("\\", "/")
    valid = all(marker in haystack for marker in _RUNNER_MARKERS)
    return {
        "alive": True,
        "valid": valid,
        "executable": executable,
        "command_line": command_line,
    }


class ProductionLock:
    """A JSON lock with legacy-PID compatibility and bounded stale recovery."""

    SCHEMA_VERSION = 1

    def __init__(self, path, *, process_probe=None, on_stale_recovered=None,
                 run_id=None):
        self.path = Path(path)
        self.owned = False
        self.run_id = run_id or uuid.uuid4().hex
        self._process_probe = process_probe or _production_process_probe
        self._on_stale_recovered = on_stale_recovered

    @staticmethod
    def _executable():
        try:
            return str(Path(sys.executable).resolve())
        except OSError:
            return str(sys.executable)

    def _metadata(self):
        executable = self._executable()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "runner": "daily_plan_production",
            "pid": os.getpid(),
            "run_id": self.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "executable": executable,
            "executable_fingerprint": hashlib.sha256(
                executable.encode("utf-8", "replace")
            ).hexdigest()[:16],
        }

    def _create(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = self._metadata()
        encoded = json.dumps(
            metadata, ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(str(self.path), flags)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            # A partial file is intentionally left for fail-closed forensic
            # handling; it is never silently treated as an old lock.
            raise
        self.owned = True

    @staticmethod
    def _parse(raw: bytes):
        text = raw.decode("utf-8").strip()
        if text.isdigit() and int(text) > 0:
            return {"legacy": True, "pid": int(text), "raw": raw}
        try:
            value = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("production runner lock is corrupt") from error
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != ProductionLock.SCHEMA_VERSION
            or value.get("runner") != "daily_plan_production"
            or isinstance(value.get("pid"), bool)
            or not isinstance(value.get("pid"), int)
            or value.get("pid") <= 0
            or not isinstance(value.get("run_id"), str)
            or not value.get("run_id")
            or not isinstance(value.get("created_at"), str)
        ):
            raise RuntimeError("production runner lock is corrupt")
        value["legacy"] = False
        value["raw"] = raw
        return value

    def _read(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        return self._parse(raw)

    def _probe(self, pid):
        try:
            result = self._process_probe(pid)
        except Exception:
            return {"alive": True, "valid": False}
        if isinstance(result, bool):
            return {"alive": result, "valid": result}
        if not isinstance(result, dict):
            return {"alive": True, "valid": False}
        return {
            "alive": result.get("alive") is True,
            "valid": result.get("valid") is True,
            **result,
        }

    @staticmethod
    def _same_bytes(path, expected):
        try:
            return path.read_bytes() == expected
        except FileNotFoundError:
            return False

    def _remove_stale(self, record):
        """Atomically quarantine exactly the bytes that were inspected."""
        if not self._same_bytes(self.path, record["raw"]):
            return False
        quarantine = self.path.with_name(
            self.path.name + ".stale-" + uuid.uuid4().hex
        )
        try:
            os.rename(self.path, quarantine)
        except FileNotFoundError:
            return False
        finally:
            try:
                quarantine.unlink()
            except FileNotFoundError:
                pass
        return True

    def acquire(self):
        """Acquire once, recovering at most one confirmed dead-owner lock."""
        stale_recovered = False
        for attempt in range(2):
            try:
                self._create()
                if stale_recovered and self._on_stale_recovered:
                    try:
                        self._on_stale_recovered()
                    except Exception:
                        pass
                return self.run_id
            except FileExistsError:
                record = self._read()
                if record is None:
                    continue
                probe = self._probe(record["pid"])
                if not probe["alive"]:
                    if attempt == 1 or not self._remove_stale(record):
                        raise RuntimeError("production runner lock changed during stale recovery")
                    stale_recovered = True
                    continue
                if probe["valid"]:
                    raise RuntimeError("production runner already active")
                raise RuntimeError("production lock owner could not be verified")
        raise RuntimeError("production runner already active")

    def release(self):
        """Remove only the JSON lock instance owned by this process/run_id."""
        if not self.owned:
            return False
        released = False
        try:
            record = self._read()
            if (
                record
                and not record.get("legacy")
                and record.get("pid") == os.getpid()
                and record.get("run_id") == self.run_id
            ):
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                released = True
        except (OSError, RuntimeError):
            released = False
        finally:
            self.owned = False
        return released
