> **Español:** Una traducción de esta página está disponible en [getting-started.es.md](getting-started.es.md).

# Getting started

El Hidratador turns a **dry** ticket — fifty words of intent — into a **wet** execution packet:
expanded context, an ordered graph of work, a sandbox boundary, and a routed compute profile.
This page is the five-minute path. Check the machine, watch one narrated run, then drive a real
ticket through the lifecycle yourself.

Everything below runs against a scratch ledger, so none of it touches your own state.

---

## Step 0 — point at a scratch ledger

The ledger is a single SQLite file, at `~/.el-hidratador/ledger.sqlite` by default. For a first
run, point it somewhere temporary:

```bash
export HIDRATADOR_DB="$(mktemp -d)/ledger.sqlite"
echo "$HIDRATADOR_DB"
```

```text
/var/folders/qm/xxxx/T/tmp.Abc123/ledger.sqlite
```

Delete the directory when you are done.

---

## Step 1 — check the machine

`doctor` answers one question: is this machine ready to run El Hidratador? It checks the
interpreter, the SQLite build, the ledger file and its permissions, the journal mode, the optional
adapters, free disk space, and the two environment variables. It reports. It never repairs
anything.

```bash
hidratador doctor | jq '{healthy, failed, warnings, checks: [.checks[] | "\(.name): \(.status)"]}'
```

```json
{
  "checks": [
    "python: ok",
    "sqlite: ok",
    "ledger: warn",
    "journal-mode: warn",
    "adapter:git: ok",
    "adapter:gh: warn",
    "adapter:docker: ok",
    "disk: ok",
    "env:HIDRATADOR_DB: ok",
    "env:HIDRATADOR_GITHUB_DB: ok"
  ],
  "failed": [],
  "healthy": true,
  "warnings": [
    "ledger",
    "journal-mode",
    "adapter:gh"
  ]
}
```

Each check carries a `detail` string explaining what was found, and a `remedy` string when there is
something to do about it. Exit code `0` when no check fails, `2` when one does. A `warn` is not a
failure: a missing `gh` or `docker` only means that adapter cannot run. The core needs neither.

The `ledger` warning is expected here — the file does not exist yet. Create it:

```bash
hidratador init
```

```json
{
  "command": "init",
  "database": "/var/folders/qm/xxxx/T/tmp.Abc123/ledger.sqlite",
  "initialized": true,
  "ok": true,
  "schema_version": 3
}
```

---

## Step 2 — watch one narrated run

Before you drive anything yourself, watch the whole lifecycle once. `demo` runs a complete ticket
against a scratch ledger in a temporary directory and narrates what happens. Your ledger is not
touched.

```bash
hidratador demo --text
```

```text
El Hidratador demonstration
================================

Created the ticket M-1001 in status INBOX.
  Scope keys: repo/api, repo/edge
  A scope key is a resource, not a description. Two tickets that share a scope key cannot run at the same time.
Attached one context blob, content-addressed by its hash.
  Hash: 7c7bb3f8ba3b47d9 (66 bytes)
  Attached context is reference data. It is quoted inside explicit banners and never treated as an instruction.
Triaged the ticket: INBOX to READY.
  Status is now READY.
Took the execution lease. The task is IN_PROGRESS.
  Session: b2f2376aae62
  Lease expires at 2026-10-22T13:06:44.726Z.
  The lease reserves the scope. A second worker asking for the same scope is refused until the lease is released or expires.
Recorded the first checkpoint with evidence.
  Tokens reported so far: 900
  Evidence is hashed. Three checkpoints with the same evidence hash block the task as no_progress.
Requested review. The task is in REVIEW, not DONE.
  Finishing is not accepting. Only an operator decision can move work to DONE.
Accepted the work. The task is DONE.
  The acceptance gate is explicit and never bypassed.
Read the ledger back.
  Accepted work: 1
  Tokens reported by the worker: 1500
  Reported tokens are declared by the worker. They are not metered by a provider, and the measured cost stays null.
Read the audit log: 9 events.
  task.created -> context.attached -> task.triaged -> session.started -> session.checkpoint -> session.checkpoint -> session.finished -> operator.accept -> task.done

Ledger used: /var/folders/qm/xxxx/T/hidratador-demo-t00uri30/demo.sqlite
The demonstration ran against a scratch ledger. Your ledger was not touched.
```

That narration is the vocabulary of the whole system, in order. Read it once more and note the
three phrases that never change: a scope key is a resource, context is reference data, and
finishing is not accepting.

---

## Step 3 — drive a real ticket

Six commands. Each one prints exactly one JSON object.

### 3.1 Add the ticket

A ticket carries an objective, the acceptance criterion that judges it, the resource scope it
touches, and the token budget it may spend.

```bash
hidratador task-add M-1001 \
  --title "Add request id echo endpoint" \
  --objective "Expose GET /healthz that returns the request id assigned by the edge proxy." \
  --acceptance "GET /healthz returns 200 with the X-Request-Id header; a unit test covers it." \
  --scope repo/api --scope repo/edge \
  --budget 4000 --tags api,health --priority 2 --owner terminal-1
```

```json
{
  "command": "task-add",
  "created": true,
  "idempotent": false,
  "ok": true,
  "task_id": "M-1001"
}
```

The ticket is now in `INBOX`. `--scope` is repeatable, and each value is a **scope key**: a
resource that only one worker may hold at a time. `--seed-key` would make the command idempotent,
so a repeated run returns the original task id instead of creating a duplicate.

### 3.2 Triage it

```bash
hidratador --compact triage M-1001 | jq '{id, status, scope}'
```

```json
{
  "id": "M-1001",
  "status": "READY",
  "scope": [
    "repo/api",
    "repo/edge"
  ]
}
```

Triage moves `INBOX` to `READY`, and it refuses if the task has no scope. A ticket is not runnable
until its resources are named.

### 3.3 Start it and take a lease

```bash
hidratador start M-1001 --owner terminal-1 --lease 900
```

```json
{
  "command": "start",
  "lease_until": 1792674407.926934,
  "lease_until_iso": "2026-10-22T13:06:47.926Z",
  "ok": true,
  "session_id": "30789e01508a4a14a5df6ca35be86392",
  "task_id": "M-1001",
  "token_budget": 4000,
  "tokens_used": 0
}
```

The task is `IN_PROGRESS`. The **lease** runs for 900 seconds and reserves the scope keys. A second
worker asking for `repo/api` is refused until this lease is released or expires. Capture the
session id:

```bash
export SESSION=30789e01508a4a14a5df6ca35be86392
```

### 3.4 Record a checkpoint

A checkpoint is four facts: what was done, the evidence, the next step, and the cumulative token
count reported by the worker.

```bash
hidratador checkpoint "$SESSION" \
  --summary "Route table registered" \
  --evidence "diff 4f1c2d: router.py adds GET /healthz" \
  --next "Read the request id header" \
  --tokens 900
```

```json
{
  "command": "checkpoint",
  "evidence_hash": "a4efe85688de6e3e24e58f3a431ea19c88489cf788eb0a1f410e46b78ae30f38",
  "ok": true,
  "session_id": "30789e01508a4a14a5df6ca35be86392",
  "stagnant": 0,
  "status": "active",
  "tokens": 900
}
```

The evidence is hashed. Three checkpoints in a row with the same hash mean the worker is looping,
and the task blocks as `no_progress`. The token count is cumulative and never decreases; the
checkpoint also renews the lease.

### 3.5 Request review

```bash
hidratador finish "$SESSION" \
  --evidence "GET /healthz returns 200 with X-Request-Id; unit test green"
```

```json
{
  "command": "finish",
  "ok": true,
  "status": "REVIEW",
  "task_id": "M-1001"
}
```

The task is in `REVIEW`. It is not done. `finish` cannot produce `DONE`.

### 3.6 Accept the work

```bash
hidratador decide M-1001 accept --reason "Reviewed the diff and the test output"
```

```json
{
  "command": "decide",
  "decision": "accept",
  "ok": true,
  "status": "DONE",
  "task_id": "M-1001"
}
```

The decision passes through `ACCEPTED` and lands on `DONE`, and two audit events are appended:
`operator.accept` and `task.done`. The other two decisions are `retry`, which returns the task to
`READY` with its cumulative token count intact, and `cancel`, which moves it to `CANCELLED`.

---

## Step 4 — read the ledger back

```bash
hidratador --compact status | jq '{tasks_by_status, metrics}'
```

```json
{
  "metrics": {
    "accepted": 1,
    "measured_cash_cost": null,
    "measured_token_savings": null,
    "note": "Reported tokens are declared by workers, not metered by a provider.",
    "reported_tokens": 900
  },
  "tasks_by_status": {
    "DONE": 1
  }
}
```

Two fields are `null` on purpose: this package does not meter providers, so it cannot report cash
cost or token savings. `reported_tokens` is what the worker declared.

The audit log holds every mutation:

```bash
hidratador --compact events | jq -r '.events[] | "\(.seq)\t\(.type)"'
```

```text
1	task.created
2	task.triaged
3	session.started
4	session.checkpoint
5	session.finished
6	operator.accept
7	task.done
```

---

## The vocabulary, in the order you meet it

| Word | What it means here |
|------|--------------------|
| dry ticket | Intent with no context attached: objective, acceptance, scope, budget |
| hydration | Expanding that ticket into a byte-budgeted packet |
| packet | The hydrated structure: objective, acceptance, scope, latest checkpoint, quoted context |
| station | One of `INBOX`, `READY`, `IN_PROGRESS`, `BLOCKED`, `REVIEW`, `ACCEPTED`, `DONE`, `CANCELLED` |
| scope key | A resource a task touches; two live tasks cannot hold the same one |
| lease | A time-boxed claim on a task, default 900 seconds |
| session | One attempt under one lease |
| checkpoint | A recorded step: summary, evidence, next step, cumulative tokens |
| evidence hash | SHA-256 of the evidence; three identical ones in a row block the task |
| failure | A recorded refusal with a category and a fingerprint |
| fingerprint | `sha256(category:summary)[:16]`, the key that groups recurring failures |
| operator decision | `accept`, `retry`, or `cancel` — the only thing that can finish work |
| acceptance gate | `finish` produces `REVIEW`; only `decide accept` produces `DONE` |

The [glossary](glossary.md) has the full list.

---

## Where to go next

- [Install](install.md) — every installation path, and how to verify one
- [CLI reference](cli-reference.md) — every command, flag, and exit code
- [Architecture](architecture.md) — the five layers and the data model
- [Glossary](glossary.md) — every domain term, defined once
- [FAQ](faq.md) — the questions that come up most often
- [Troubleshooting](troubleshooting.md) — symptom, cause, and recovery command
- [Failure playbook](failure-playbook.md) — one page per failure category
- [Operations](operations.md) — ledger location, backups, and the stuck-task runbook
- [Security](security.md) — the trust boundaries, and what is deliberately not done
- [Development](development.md) — working on the project itself
- [Community](community.md) — asking a question, reporting a bug, proposing a change
