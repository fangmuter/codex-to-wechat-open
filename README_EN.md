# codex-to-wechat

A Python project exploring local personal workflows for daily planning, confirmed-plan delivery, iLink messaging, roadmap review sync, Shared State export, and a review-first Chat Agent prototype.

This repository is an **open-source preparation snapshot**, not a turnkey WeChat bot distribution. The default demos and tests use mocks and do not connect to WeChat, Google Drive, Ollama, or personal data. Live iLink, Windows DPAPI, DriveFS, and Task Scheduler integrations are environment-specific experiments. They were not exercised during this release preparation, so this README makes no clean-machine end-to-end deployment claim.

## Quick start

Python 3.10 or newer is required. Runtime and test code use only the Python standard library.

```powershell
python --version
python examples/mock_delivery_demo.py
python daily_plan_mock/scripts/run_demo.py
python -m unittest discover -s . -p "test_*.py" -v
```

The Web-first demo writes a sample confirmed-plan JSON to a temporary directory, uses the SQLite idempotency ledger and a fake transport, then replays the same plan. Expected output includes `DELIVERED`, `NOOP`, one fake send, and `network_access=NO`. The legacy planner demo uses in-memory SQLite and `MockTransport`.

## What is included

- A local daily-plan state machine for testing input, draft, exact confirmation, and mock delivery flows.
- A Web-first delivery service that validates an explicit `CONFIRMED` JSON plan, computes a normalized hash, selects by target date, rejects conflicts, and records idempotent outcomes before calling a transport interface.
- iLink receive/parser, whitelist and fingerprint checks, duplicate suppression, Windows DPAPI Current User credential storage, short-lived reply context, session-health states, and a production lock.
- Roadmap review validation/import and next-task projection. The release copy contains only a fictional example profile, not the maintainer's progress.
- Shared State JSON/Markdown export and an allowlisted local sync adapter.
- A review-first Chat Agent prototype with contact pairing, policy checks, drafts, and transport abstractions.

These modules are not yet a packaged, versioned SDK with a stable unified public API; several imports remain repository-relative.

## Architecture

See [docs/architecture.md](docs/architecture.md) for a Mermaid data-flow diagram. Web-first delivery starts from a plan already confirmed elsewhere. It does not call the legacy multi-turn planner, Roadmap recommender, or Fusion chain. Review import is a separate progress-authority path; Delivery JSON does not mutate Roadmap or reading progress.

## Configuration and safety boundaries

The mock workflows require no configuration. `.env.example` is a variable template only; Python does not load `.env` automatically. Operators must set and verify explicit paths before constructing a live runtime.

| Variable | Purpose |
| --- | --- |
| `ILINK_STATE_DIR` | Local DPAPI credential and iLink state directory; otherwise a generic current-user Local AppData location is used |
| `ILINK_EXPECTED_RECIPIENT_FINGERPRINT` | Operator-approved 12-hex recipient fingerprint; no personal fingerprint is bundled |
| `DAILY_PLAN_STATE_DIR` | Production state directory; otherwise derived from the iLink state directory |
| `DAILY_PLAN_REVIEW_INBOX` | Explicit absolute Review Inbox path |
| `DAILY_PLAN_DELIVERY_INBOX` | Explicit absolute Delivery Inbox path |

Live credential storage requires Windows DPAPI Current User. Real WeChat integration also depends on the iLink service and an authorized account/recipient binding. DriveFS must be configured and synced by the operator. This release copy omits Task Scheduler installers, Production launchers, machine-specific pilot scripts, and re-auth promotion scripts. A low-level experimental CLI still has explicit `--live` operations that can contact a real account or change local credentials; the documented demos and test suite do not call them.

Never commit tokens, cookies, raw recipient IDs, `context_token`, DPAPI files, real plans, SQLite ledgers, or runtime logs. Live construction should fail closed if required configuration is absent; placeholder values are not a substitute for a working service configuration.

## Project map

| Path | Purpose |
| --- | --- |
| `daily_plan_mock/` | Local daily-plan state machine and fake transport |
| `daily_plan_delivery/` | Web-first schema, formatter, delivery service, and ledger |
| `daily_plan_production/` | Bounded delivery runtime, paths, and process lock (no installer or launcher) |
| `ilink_receive_probe/` | iLink protocol, receive path, credential, identity, session-health, and experimental send adapters |
| `daily_plan_roadmap/` | Roadmap definitions, progress store, Review Sync, and task source |
| `daily_plan_recommendation/`, `daily_plan_fusion/` | Recommendation scoring and plan fusion |
| `daily_plan_shared_state/` | Shared State export and allowlisted sync |
| `daily_plan_friendly_input/`, `daily_plan_wechat_integration/` | Deterministic input parsing and legacy-flow adapters |
| `chat_agent/` | Review-first local Chat Agent prototype |
| `daily_plan_codex_planner/` | Codex planner protocol and isolation prototype |
| `examples/` | Clearly labeled sample data and no-network demos |
| `docs/` | Architecture, setup, security, and test notes |

`examples/roadmap_profile.example.json` is a generic fictional roadmap and does not represent the maintainer's learning record. `examples/reading_catalog.example.json` contains one fictional placeholder entry for integration tests; it is not a book list and should be replaced with your own data.

## Tests and limitations

The tests focus on schema validation, normalized hashes and idempotency, fail-closed state transitions, mock transports, review sync, Shared State projection, and log redaction. The default unit tests do not call the live iLink network. Chat Agent review-server tests may bind a loopback HTTP server but do not access the public Internet.

The release-preparation report records the commands actually run. Omitted machine-operation scripts, real DPAPI ciphertext I/O, Windows Task Scheduler, DriveFS cloud propagation, real WeChat send/receive, and an external Ollama service are not covered. A passing mock is not proof of live end-to-end behavior.

## License and third-party attribution

Project-owned code that the maintainer is authorized to license is distributed under the [MIT License](LICENSE). The iLink request shape references Tencent `openclaw-weixin` v2.4.8; the source attribution, applicable Tencent copyright notice, and complete MIT text are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This project is not officially endorsed by or affiliated with Tencent.
