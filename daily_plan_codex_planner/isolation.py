"""The only App Server turn configuration permitted for a future approved test."""

from __future__ import annotations

from pathlib import Path


def isolated_turn_spec(workspace: Path, model: str, effort: str) -> dict:
    """Return a restrictive spec; this module never starts an App Server turn."""
    root = str(workspace.resolve())
    return {
        "cwd": root,
        "model": model,
        "effort": effort,
        "approvalPolicy": "never",
        "sandboxPolicy": {
            "type": "readOnly",
            "access": {"type": "restricted", "includePlatformDefaults": False, "readableRoots": [root]},
        },
        "outputSchema": {"type": "object", "additionalProperties": False},
    }


# App Server documents sandboxing and approval policies, but not a built-in
# `tools: none` switch for Codex's own shell/file tools, nor a documented
# readOnly-network-off guarantee.  This must remain a hard preflight blocker
# rather than being papered over with a prompt.
CAN_FULLY_DISABLE_BUILTIN_TOOLS = False
