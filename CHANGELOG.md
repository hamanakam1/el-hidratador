# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Ticket creation and updates validate required text, kind, scope, budget, priority, and collection
  types before writing. Invalid updates cannot erase scope or bypass the cumulative token budget,
  and refusals preserve the ticket and audit history.
- Schema validation distinguishes booleans from numbers in constant and enumeration checks,
  including nested values. Non-finite numbers are refused, and array errors retain their indices.
- The CLI returns exit code `2` and `ok: false` for schema mismatches, and typed errors for malformed
  JSON, non-UTF-8 documents, and noninteger ticket updates. Schema checks never open a ledger.
- Repeatable `task-update --list` values no longer require an unused `--value` argument.
- Ticket updates cannot remove, replace, or expand scope reservations while a task is in
  progress, blocked, or in review. Refused updates preserve the ticket and its audit events.
- Context expansion excludes expired attachments from both packets and rendered prompts,
  reports each omission, and refuses invalid expiration timestamps without mutating the ledger.
- Evidence writes preserve sparse history and simultaneous writers using atomic publication
  that cannot replace an existing record. New files retain owner-only permissions, collision
  retries are bounded, and similarly prefixed evidence kinds keep independent numbering.

## [0.2.0] — 2026-10-22

The expansion release. It adds the commands that make the project approachable — a doctor, a
narrated demonstration, a published contract, and a replay of the event log — and it turns the
repository into a place where a newcomer can start without asking anyone.

### Added

### Onboarding and diagnostics

- `hidratador doctor` checks the interpreter, the SQLite build, the ledger, the journal mode, the
  optional adapters, the free disk space, and the configuration environment variables. It reports
  what it found and never repairs anything. A missing ledger or a missing adapter is a warning,
  not a failure: the core needs neither.
- `hidratador demo` runs a complete ticket lifecycle against a scratch ledger in a temporary
  directory and narrates it, so a newcomer can watch the whole system work before touching their
  own state.
- `hidratador topology` reports the machine the process runs on.

### The contract layer: `el_hidratador/schema.py`

- Five published JSON Schemas: `ticket`, `packet`, `manifest`, `sandbox`, and `handoff`, written to
  `schema/` in the repository and regenerable with `hidratador schema --out schema/`.
- A dependency-free validator for the subset of JSON Schema those schemas use, so a payload can be
  checked without adding a runtime dependency to the core.
- `hidratador schema --validate FILE --against NAME` validates a document against a published
  schema.
- `handoff.authorized` and `manifest.execution_authorized` are constants of `false`. A payload that
  claimed otherwise does not validate.

### Replay: `el_hidratador/replay.py`

- `hidratador replay <id>` rebuilds a task's lifecycle from the event log and compares it with
  the current state. A divergence is reported, never repaired.
- `hidratador replay --all` replays every task in the ledger.

### More read-only commands

- `hidratador failures [--task ID]` lists the recorded failures, grouped by fingerprint.
- `hidratador checkpoints <session>` lists the checkpoints of a session.
- `hidratador handoffs <task>` lists the recorded handoff payloads.
- `hidratador context-list <task>` lists the context attached to a task.
- `hidratador profile-list` lists the declared compute profiles.
- `hidratador task-update <id> --field F --value V` changes one documented ticket field. Lifecycle
  fields are refused.
- `hidratador sandbox-apply --id <task> --workdir <path>` prints the exact `docker run` argv and the
  boundary report, and creates the container only when `--apply` is passed.
  `hidratador sandbox-teardown --name <container>` removes it. This is the only place in the
  package that starts a container, and it does so only when an operator asks.

### Changed

- Read-only commands no longer create a ledger as a side effect. `doctor`, `demo`, `schema`,
  `topology`, and the three adapter commands open no database at all.
- The `replay` reconstruction treats the prerequisites node as a gate, and the task parser reads
  the `## Tasks` section rather than the whole ticket body.

### Documentation

- New pages: getting started, glossary, FAQ, install, troubleshooting, development, community, and
  seven architecture decision records with an index.
- The README leads with the two commands a newcomer should run, and points at the pages that
  answer the questions they are about to have.

### Repository

- A devcontainer, so the toolchain is already installed when the project is opened.
- Discussion forms for introductions and ideas, an issue form for requests for comments, and an
  issue form for small tasks.
- `CITATION.cff` for citation metadata.
- Container image publishing, build attestations with an SBOM, and a scheduled workflow that
  publishes the wiki pages from `docs/wiki/`.

---

## [0.1.0] — 2026-10-22

The first release. It establishes the pipeline position of El Hidratador
between the Ticket Board and Taskrivet, and nothing else.

### Added

**Core ledger** (`el_hidratador/core.py`)

- SQLite ledger with WAL mode, foreign keys enabled, and schema version 3
- Additive, idempotent schema migrations
- Ticket lifecycle with eight stations: `INBOX`, `READY`, `IN_PROGRESS`,
  `BLOCKED`, `REVIEW`, `ACCEPTED`, `DONE`, `CANCELLED`
- Execution leases with expiry, reconciliation, and automatic blocking
- Resource scope reservations that refuse overlapping concurrent work
- Cumulative token budgets that survive retries and never decrease
- Stagnation detection: three checkpoints with identical evidence block the
  task as `no_progress`
- Fingerprinted failures (`sha256(category:summary)[:16]`) grouped for
  recurrence analysis
- Context attachment that is content-addressed, deduplicated, and expirable
- Byte-budgeted packet assembly that reports everything it omits
- Compute profile declarations and eligibility-only routing
- Append-only event log and recorded handoff payloads

**Context expansion** (`el_hidratador/context_expander.py`)

- Deterministic prompt rendering with a stable output contract
- Untrusted-context banners around every attached body
- Exact byte budgets with explicit omission reporting
- Character-based token estimation, labelled as an estimate

**Execution DAG** (`el_hidratador/dag.py`)

- Checklist and file-section parsing for ticket bodies
- Deterministic topological ordering with cycle detection
- Parallel wave grouping
- Execution manifests that never claim authorization

**Sandbox staging** (`el_hidratador/sandbox.py`)

- Pure sandbox specifications: image, mounts, resources, capabilities
- Read-only root filesystem, no network by default, all capabilities dropped
- `docker run` argv rendering and a dry-run-first apply path

**Hardware routing** (`el_hidratador/hardware_router.py`)

- Workload classification and target eligibility evaluation
- Cost as a hard gate; a paid target is never eligible for autonomous dispatch
- Dispatch plans that never authorize execution

**GitHub sync** (`el_hidratador/github_sync.py`)

- Read-only observations with an eight-kind vocabulary and a six-action
  recommendation vocabulary
- An outbox with idempotency keys, body hashes, and reconciliation markers
- Marker-based reconciliation so a repeated run never double-posts

**Dashboard preview** (`el_hidratador/dashboard.py`)

- Loopback-only, HTTP-only, no-redirect client with an 8 MiB cap and a 10 s
  timeout

**Ticket storage** (`el_hidratador/tickets.py`)

- Portable markdown tickets with front matter, an index, and per-task evidence
  files
- Atomic writes

**Command line interface** (`el_hidratador/cli.py`)

- Twenty-three JSON-first commands
- Exit codes: `0` success, `2` refused operation, `3` adapter failure

### Documentation and repository

- Full documentation set under `docs/`, a runnable walkthrough under
  `examples/`, and a pinned MkDocs configuration
- Continuous integration across Linux and macOS on Python 3.11 through 3.14
- CodeQL, OpenSSF Scorecard, dependency review, `pip-audit`, and Bandit
- Issue forms, a pull request template, `CODEOWNERS`, and a labeler
- Discussion and wiki enablement, release automation, and a published
  documentation site

[Unreleased]: https://github.com/hamanakam1/el-hidratador/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hamanakam1/el-hidratador/releases/tag/v0.2.0
[0.1.0]: https://github.com/hamanakam1/el-hidratador/releases/tag/v0.1.0
