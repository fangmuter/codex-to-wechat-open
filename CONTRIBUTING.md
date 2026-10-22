# Contributing

Thanks for considering a contribution. This is an experimental repository with several distinct state and authority boundaries; small, evidence-backed changes are easier to review than broad rewrites.

## Before opening a change

- Open an issue for a substantial behavior change and describe the user-visible effect, data stores touched, and failure behavior.
- Keep pull requests focused. Do not combine protocol changes, state-machine changes, and documentation cleanup without a clear reason.
- Do not add live credentials, personal plans, contact identifiers, local databases, runtime logs, or machine-specific paths.
- Do not add a network test that contacts WeChat, Google Drive, Ollama, or another user account. Use dependency injection and fake transports.

## Tests

Use Python 3.10+ and the standard library:

```powershell
python -m unittest discover -s . -p "test_*.py" -v
python examples/mock_delivery_demo.py
```

Tests should use temporary directories, deterministic clocks/IDs, and mock transports. A test that requires Windows DPAPI or an external service must be clearly marked and must not be part of the default offline suite.

## Safety expectations

- Preserve fail-closed behavior for malformed, conflicting, ambiguous, and already-attempted deliveries.
- Keep Roadmap and Reading mutations behind their explicit review/update interfaces.
- Keep raw token, recipient, cookie, and context-token values out of ordinary logs and SQLite.
- Do not add automatic retries for ambiguous outbound results.
- Document any environment variables and their actual runtime requirements; do not replace private paths with misleading fake defaults.

## Review checklist

- Explain the behavioral change and its limits.
- Include focused tests and the command output summary.
- Identify whether the change can perform network, filesystem, credential, or scheduler side effects.
- Update README/docs when setup, safety boundaries, or supported behavior changes.

The project has not selected a license yet. Contributions should not assume a license grant beyond the rights currently held by their authors; licensing terms will need to be established before public contributions are accepted.
