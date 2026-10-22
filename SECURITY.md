# Security policy

This project handles workflows that may touch personal plans, account identities, credentials, and outbound messages. Treat the live integration modules as experimental unless a specific deployment has been reviewed and tested.

## Reporting a vulnerability

Do not publish credentials or exploit details in a public issue. If GitHub private vulnerability reporting is enabled for the repository, use it. Otherwise, contact the maintainer privately through a channel they have explicitly published; if no such channel is available, open a minimal public issue asking for a private reporting route without including sensitive details.

## Data that must not be committed

- iLink bot tokens, cookies, authorization headers, raw recipient/sender IDs, raw `context_token`, QR/session payloads, DPAPI ciphertext, or credential metadata from a real account.
- Real plan JSON, Review Update files, Shared State exports, reading catalogs, SQLite databases, delivery ledgers, cursor state, runtime logs, and local pilot state.
- Personal absolute paths, recipient fingerprints, account fingerprints, or private task history.

The `.gitignore` covers common runtime files but is not a substitute for review. Inspect both the staged diff and the resulting repository before publishing.

## Reporting and operating limits

- Delivery JSON is data, never a command language. The schema rejects unsafe fields and the inbox scanner is rooted to an explicit directory.
- A normalized payload hash and ledger protect plan identity and replay handling. Ambiguous or failed outbound attempts are closed; they are not automatically retried.
- Recipient binding and allowlist checks must remain explicit. Missing live configuration should fail closed.
- `context_token` is short-lived reply context and must not be copied to ordinary logs, Shared State, or long-lived application databases.
- The documented demo and default tests use fake transports. They do not send a WeChat message or verify remote session health.
- Never use the experimental `--live` CLI against an account without understanding the exact operation and obtaining any required operator consent.

## Supported environment

The mock code is designed for local Python 3.10+ execution. Real credential storage requires Windows DPAPI Current User. Google Drive Desktop and iLink are external dependencies. This repository does not provide a security support SLA or a guarantee of production suitability.
