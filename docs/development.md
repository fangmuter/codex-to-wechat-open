# Development, configuration, and verification

## Environment

The repository is currently run from its root rather than installed as a wheel. Python 3.10+ is the intended baseline. Runtime and test code use only the standard library; `requirements.txt` records this explicitly. No third-party SDK is required for the mock workflows.

On Windows, real credential storage uses DPAPI Current User. The actual production runtime also needs an authorized iLink session, an approved recipient binding, and explicit Inbox directories. The local DriveFS mount is an external deployment prerequisite, not emulated by a path placeholder.

`.env.example` lists configuration names but is not auto-loaded. Set environment variables in the launching environment after replacing the placeholders. Do not put a raw token, raw recipient ID, cookie, or context token in environment files. The expected recipient fingerprint is configuration, not source code.

## Safe local checks

From the repository root:

```powershell
python examples/mock_delivery_demo.py
python daily_plan_mock/scripts/run_demo.py
python -m unittest discover -s . -p "test_*.py" -v
```

All demo/test artifacts use temporary directories or in-memory stores. The fake transport records a local call and does not open a socket. One Chat Agent test uses loopback HTTP to exercise its local review server; no public network endpoint is used.

## Scope not covered by these commands

These commands do not validate Windows DPAPI against a real user credential, iLink account/session health, recipient binding, real WeChat delivery, Task Scheduler definitions, Google Drive Desktop hydration/propagation, or Ollama service availability. The machine-specific operator scripts are deliberately absent from this release snapshot. Do not infer a production deployment recipe from the module names alone.

## Suggested contribution loop

1. Add or update a standard-library `unittest` test beside the affected module.
2. Use temporary directories and injected transports/clocks.
3. Assert failure, retry, and redaction behavior as well as the happy path.
4. Run the focused test and then the full offline command above.
5. Report any live-only behavior separately; never claim a mock test proves it.
