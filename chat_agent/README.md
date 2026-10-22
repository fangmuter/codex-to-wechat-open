# Chat Agent foundations

This package is an isolated foundation for the REVIEW-first fixed-contact
pilot. It is intentionally not wired into Daily Plan or a live iLink account.

The current slice incorporates these reliability decisions:

- a contact is only allowed after a time-bounded manual pairing confirmation;
- the C2 router only admits an already-allowed one-to-one text contact;
- AUTO requires a separately recorded consent flag;
- a new batch supersedes the previous pending draft for that contact;
- drafts expire after 30 minutes and use optimistic version checks;
- inbound work uses an account-scoped claim/completion record so replayed
  long-poll events do not enter the model pipeline twice;
- the manual-pilot receiver can defer cursor advancement until the adapter
  acknowledges processed events, so a crash between polling and generation
  replays the unacknowledged batch safely;
- ret=0 without a delivery receipt is DELIVERY_UNCONFIRMED, never a
  confirmed send and never an automatic retry;
- model output has a provider boundary and can explicitly request user input.
- contact text is treated as untrusted input; the local prompt forbids prompt
  injection from changing configuration, exposing secrets/history, or running
  computer commands;
- a small deterministic risk screen adds redacted flags (money, legal,
  medical, commitment, conflict, decision, or prompt-injection) to the review
  record without making an automatic send decision; high-risk drafts require
  a second confirmation even when an API client omits the sensitive flag;
- the Ollama adapter only calls an already-running `127.0.0.1` server, uses a
  structured JSON response, and turns timeout/invalid output into a no-send
  user-input state;
- the inbound pipeline batches one contact during an 8-second quiet window,
  persists the batch before generation, and can finish a batch after a process
  restart without sending anything automatically.
- `ChatAuditStore` records bounded route, draft, send, and recovery lifecycle
  events using stable identity keys and redacted codes only; it never stores
  message text, recipients, context tokens, prompts, or exception values.

The pipeline accepts a context reference supplied by the existing receiver;
it never stores the raw iLink context token. It is not yet wired into a live
iLink polling loop, while the local review web panel is available as an
explicit composition point. `AUTO` remains a stored consent/mode value rather
than an enabled sender.

`ChatAgentReceiverAdapter` is the explicit composition point for a future
manual pilot: it calls the existing receiver, resolves the already-persisted
context reference, records an unknown supported sender as a bounded router
candidate by default, and invokes the pipeline. It does not alter Daily Plan,
auto-bind a contact, or send replies. In the factory-built pilot, the
receiver owns a deferred cursor and the adapter acknowledges each batch only
after pipeline processing (or deliberate recovery skipping) completes.

`LocalReviewServer` provides a loopback-only review page and JSON API for
listing, approving, rejecting, and—only when an explicit
`ManualSendCoordinator` is injected—manually sending an approved draft. The
page uses the manually confirmed contact alias where available and lets the
reviewer edit a draft before approval; a `NEEDS_USER_INPUT`
draft must be filled explicitly before it can move to `APPROVED`. The send
action requires a second confirmation, a context identity match against the
draft contact, and receipt-aware transport; no AUTO path is exposed.

An embedding pilot can start it with `LocalReviewServer(draft_store).start()`
and open the returned `address`; the default port is ephemeral (`port=0`).

`ChatHistoryStore` bounds per-contact history to 200 messages and 30 days by
default. Notes and style examples are manual-only, and sensitive edits require
a second confirmation. Reads and writes are serialized for the runtime and
review threads, and the pipeline can receive this store through its
`history=` argument to add bounded, contact-isolated context to the local-model
prompt.

`ChatAgentRuntime` is a manual composition loop with an independent control
database. It starts disabled, requires an explicit `enable()`, and honours a
separate stop switch; importing the package or starting the review page does
not poll iLink.

`build_manual_pilot(transport, provider, state_dir)` is a convenience
composition for this boundary: it creates separate receiver, contacts,
inbox, drafts, history, batches, audit, and runtime-control SQLite files, leaves the
router/runtime disabled, and returns an unstarted loopback review server. It
performs no network or model call during construction; call `pilot.runtime.enable()`
and start the review page only as an explicit later step.

`ChatAgentReceiver` is the separate inbound composition point for a future
pilot. It admits only one-to-one plain text to the Chat Agent boundary,
persists supported inbound events as candidate-status records, and never
auto-binds them or reuses Daily Plan's fixed-contact whitelist. The adapter's
default router observer turns those unknown events into the five-minute local
pairing candidates shown by the review panel.

`run_benchmark(provider)` exercises only the model boundary. It does not call
the receiver, create drafts, or send replies; use it against a chosen local
Ollama model before recording a production model selection.

`check_ollama(model)` is the read-only C1 preflight. It queries only the
already-running `127.0.0.1` `/api/tags` endpoint, never launches Ollama or
installs a model, and distinguishes service-unavailable from model-not-
installed states.

Notifications are opt-in. `ChatAgentReceiverAdapter` can receive a
`notification_sink`; the built-in Windows implementation uses `msg.exe` only
to announce a draft ID and status, never draft content or context tokens. A
notification failure does not block REVIEW.

When the receiver reports recovery from `WECHAT_UNAVAILABLE`, the adapter
does not feed that recovery batch into contacts, history, or the model. It
reports only the number of skipped events and resumes with the next normal
poll.

`run_offline_acceptance()` exercises the pre-live matrix (100 batches, 5
restart simulations, and 3 network failures by default) with temporary local
state and fake transport/model components. It now checks unknown/duplicate
model-call invariants, a P95 latency budget, expired-send blocking, and zero
transport attempts; it also verifies deferred receiver handoff replay before
acknowledgement. Its `manual_gates` metrics still identify work that
cannot be honestly certified offline: a real local-model benchmark, human
edit-quality scoring, a real-account receive pilot, and a receipt-aware send
pilot.
