# Operations

Day-to-day operation of the ledger: where it lives, how to back it up, how to read it, and what to
do when a task gets stuck.

---

## Where the ledger lives

| Database | Default | Override |
|----------|---------|----------|
| Ledger | `~/.el-hidratador/ledger.sqlite` | `$HIDRATADOR_DB`, or `--db PATH` |
| GitHub adapter | `~/.el-hidratador/github.sqlite` | `$HIDRATADOR_GITHUB_DB` |

Resolution order for the ledger path:

1. `--db PATH` on the command line.
2. `$HIDRATADOR_DB`, with `~` expanded.
3. `~/.el-hidratador/ledger.sqlite`.

The parent directory is created with mode `0700` if it does not exist, and the database file is
chmod'd to `0600`.

```bash
# Where is this shell pointed?
echo "${HIDRATADOR_DB:-$HOME/.el-hidratador/ledger.sqlite}"

# What does the package think?
hidratador status | jq -r '.database'

# Use a different ledger for one invocation
hidratador --db /srv/hidratador/ledger.sqlite status
```

`github-poll`, `github-publish`, and `dashboard-preview` read no ledger data, but the CLI still
opens the ledger for every command — so the file at the `--db` path is created if it does not
already exist. Those three commands are governed by `HIDRATADOR_GITHUB_DB` and their own flags
instead.

### Environment variables

| Variable | Read by | Purpose |
|----------|---------|---------|
| `HIDRATADOR_DB` | `core.default_db_path()` | Ledger path. Only used when `--db` is absent. |
| `HIDRATADOR_GITHUB_DB` | `github_sync.GitHubSync.__init__` | GitHub adapter database path. |

These are the only two environment variables the package reads. There are no others — no log level,
no endpoint overrides, no feature flags.

### A note on the adapter database

The GitHub adapter creates its parent directory with mode `0700` but does **not** chmod the
database file itself. Access to it therefore depends on the directory permission. If you relocate it
somewhere less protected, tighten the file yourself:

```bash
chmod 600 "$HIDRATADOR_GITHUB_DB"
```

---

## Backup and restore

The ledger runs in WAL mode, so the database file is accompanied by `-wal` and `-shm` sidecar files
while it is open. Copying the `.sqlite` file alone while a process is writing can produce an
inconsistent copy.

### Backup with the sqlite3 CLI (recommended)

```bash
# Online backup: consistent even while the ledger is open
sqlite3 ~/.el-hidratador/ledger.sqlite ".backup '/backups/ledger-$(date +%F).sqlite'"
```

### Compact copy with VACUUM INTO

```bash
# Produces a single self-contained file with no sidecars. Needs SQLite 3.27 or newer.
sqlite3 ~/.el-hidratador/ledger.sqlite "VACUUM INTO '/backups/ledger-$(date +%F).sqlite'"
```

### Offline copy

If nothing is running, copy the file and its sidecars together:

```bash
cp ~/.el-hidratador/ledger.sqlite{,-wal,-shm} /backups/ 2>/dev/null
cp ~/.el-hidratador/ledger.sqlite /backups/
```

### Verify a backup before trusting it

```bash
sqlite3 /backups/ledger-2026-10-22.sqlite "PRAGMA integrity_check;"
# ok

sqlite3 /backups/ledger-2026-10-22.sqlite "SELECT status, COUNT(*) FROM tickets GROUP BY status;"
```

### Restore

```bash
# Keep the current ledger, then swap in the backup
mv ~/.el-hidratador/ledger.sqlite ~/.el-hidratador/ledger.sqlite.broken
rm -f ~/.el-hidratador/ledger.sqlite-wal ~/.el-hidratador/ledger.sqlite-shm
cp /backups/ledger-2026-10-22.sqlite ~/.el-hidratador/ledger.sqlite
chmod 600 ~/.el-hidratador/ledger.sqlite

hidratador status | jq '.tasks_by_status'
```

Removing the stale `-wal` and `-shm` files matters: they belong to the old database and would
otherwise be replayed into the restored copy.

---

## Pruning expired leases

`reconcile` is the housekeeping command. It expires stale leases and reports what changed.

```bash
$ hidratador reconcile
{
  "command": "reconcile",
  "expired_count": 1,
  "expired_tasks": ["M-2001"],
  "ok": true
}
```

What it does, per expired session:

| Object | Change |
|--------|--------|
| `sessions.status` | `active` → `expired` |
| `tickets.status` | → `BLOCKED` |
| `failures` | A `lease_expired` row is written |
| `events` | A `failure.recorded` event is appended |

`reconcile` is also run automatically by `start`, `checkpoint`, `finish`, `decide`, `pack`, and
`status`. Running it explicitly is useful for a cron job that keeps the ledger tidy between
workers.

```bash
# Hourly lease pruning
0 * * * * hidratador --compact reconcile >> /var/log/hidratador/reconcile.log 2>&1
```

To find leases that are *about* to expire:

```bash
sqlite3 ~/.el-hidratador/ledger.sqlite \
  "SELECT id, task_id, owner, datetime(lease_until,'unixepoch') FROM sessions WHERE status='active';"
```

---

## Exporting tickets to a file-backed store

The ledger is the authority for state. The file tree is the *portable* form: a directory that can
be copied, reviewed in a diff, and archived without a running process.

```bash
$ hidratador export --dir .hidratador
{
  "command": "export",
  "index": ".hidratador/index.json",
  "ok": true,
  "root": ".hidratador",
  "written": ["M-1001.md", "M-1002.md"]
}
```

Result:

```text
.hidratador/
├── tickets/
│   ├── M-1001.md
│   └── M-1002.md
├── index.json
└── evidence/
    └── M-1001/
        └── checkpoint-1.json
```

`index.json` maps each task id to its title, status, kind, priority, and file name.

```json
{
  "M-1001": {
    "file": "M-1001.md",
    "kind": "code",
    "priority": "2",
    "status": "DONE",
    "title": "Add request id echo endpoint"
  }
}
```

Each ticket file is YAML front matter plus a markdown body. Writes are atomic, so an interrupted
export never leaves a torn ticket on disk.

The evidence directory is populated by `TicketStore.write_evidence()`, which the CLI does not call.
To record evidence records yourself:

```python
from el_hidratador.tickets import TicketStore

store = TicketStore(".hidratador")
store.write_evidence("M-1001", "checkpoint", {"summary": "...", "tokens": 1200})
store.write_evidence("M-1001", "finish", {"evidence": "..."})
```

Each call writes a new file; a second record of the same kind is written as
`<kind>-2.json`, `<kind>-3.json`, and so on.

---

## Reading the event log

Every mutation appends an event. The log is the audit trail.

```bash
# Everything
hidratador events

# Only what happened after a sequence number you have already seen
hidratador --compact events --after 42 | jq '.events[] | {seq, type, task_id}'
```

Event types and what they mean:

| Type | Emitted when |
|------|--------------|
| `task.created` | A ticket is created |
| `task.updated` | Mutable ticket fields change |
| `task.triaged` | A ticket moves from `INBOX` to `READY` |
| `session.started` | A lease is acquired |
| `session.checkpoint` | Progress is recorded |
| `session.finished` | `finish()` moves a ticket to `REVIEW` |
| `failure.recorded` | A failure is written |
| `context.attached` | A context blob is attached |
| `profile.declared` | A compute profile is declared |
| `handoff.recorded` | A handoff payload is stored |
| `operator.accept` | An operator accepts work |
| `operator.retry` | An operator sends work back |
| `operator.cancel` | An operator cancels work |
| `task.done` | A ticket reaches `DONE` |

Useful queries:

```bash
# The full history of one task
hidratador --compact events | jq '.events[] | select(.task_id == "M-1001") | {seq, type, created_iso}'

# Every failure ever recorded
hidratador --compact status | jq '.failures[] | {category, fingerprint, occurrences, last_seen_iso}'

# What did the operator decide, and why?
hidratador --compact events | jq '.events[] | select(.type | startswith("operator.")) | {seq, task_id, data}'
```

The event table is append-only in practice — the package never deletes events. It grows with use.
The compact `events` command returns at most 500 events per call; page with `--after`.

---

## Runbook: a stuck task

A task is "stuck" when it is in `IN_PROGRESS`, `BLOCKED`, or `REVIEW` and nobody is advancing it.

### Step 1 — look at it

```bash
hidratador --compact task-show M-1001 | jq '{id, status, scope, token_budget, owner, updated_at_iso}'
```

### Step 2 — check for an expired lease

```bash
hidratador reconcile
```

If the task was `IN_PROGRESS` with a dead worker, this moves it to `BLOCKED` and records a
`lease_expired` failure.

### Step 3 — read the failure history

```bash
hidratador --compact status | jq '.failures[] | select(.task_id == "M-1001")'
```

The `fingerprint` groups the same cause across attempts. `occurrences` above `1` means the same
thing has gone wrong more than once — do not retry blindly.

### Step 4 — check what is holding the scope

```bash
hidratador --compact task-list | jq '.tasks[] | select(.status == "IN_PROGRESS" or .status == "BLOCKED" or .status == "REVIEW") | {id, status, scope}'
```

Tasks in `IN_PROGRESS`, `BLOCKED`, and `REVIEW` all reserve their scope keys. A task blocked
because of a scope conflict will keep failing to start until the holder moves.

### Step 5 — decide

| Situation | Action |
|-----------|--------|
| The worker is alive and working | Do nothing. Wait for the next checkpoint. |
| The worker died | `decide retry` with a reason, then `start` with a new owner and lease |
| The same failure keeps recurring | `context-add` the missing information, then `decide retry` |
| The requirement is ambiguous | Leave it `BLOCKED` and escalate to an operator |
| The work is no longer wanted | `decide cancel` |

```bash
# Retry
hidratador decide M-1001 retry --reason "Worker terminal-1 confirmed stopped at 07:48"
hidratador start M-1001 --owner terminal-2 --lease 1800

# Cancel
hidratador decide M-1001 cancel --reason "Superseded by M-1042"
```

### Step 6 — confirm

```bash
hidratador --compact task-show M-1001 | jq '{id, status}'
hidratador --compact events --after <last_seq> | jq '.events[] | {seq, type, task_id}'
```

### Step 7 — if the budget is the blocker

`decide retry` preserves the cumulative token count, so a task that exhausted its budget stays
exhausted. The budget must be raised through the library — there is no CLI command for it:

```python
from el_hidratador import create_ledger
from el_hidratador.core import default_db_path

with create_ledger(default_db_path()) as ledger:   # $HIDRATADOR_DB, or the default path
    ledger.update_task("M-1001", token_budget=16000)
```

```bash
hidratador decide M-1001 retry --reason "Budget raised to 16000"
hidratador start M-1001 --owner terminal-2 --lease 1800
```

---

## Recovery drill: a task that will not move

Use this when a task is stuck and the reason is not obvious from `hidratador status`. It is
deliberately read-only until the last step, so a drill that is interrupted half way leaves the
ledger exactly as it found it.

### 1. Read the task

```bash
hidratador task-show M-1001 | jq '{status, owner, scope, token_budget}'
```

### 2. Read the sessions

```bash
hidratador status | jq '.sessions[] | select(.task_id == "M-1001") | {id, owner, status, tokens, lease_until}'
```

A session that is `active` with a `lease_until` in the past is a lease that nobody reconciled.
A session that is `active` with a future `lease_until` is a worker that is still running — do not
take the lease away from it.

### 3. Read the failure history

```bash
hidratador status | jq '.failures[] | select(.category != null) | {category, summary, occurrences, last_seen}'
```

Repeated occurrences of the same fingerprint are an environment problem, not a worker problem.

### 4. Read the event trail

```bash
hidratador events | jq -r '.events[] | select(.task_id == "M-1001") | "\(.seq)\t\(.type)"'
```

The last event before the stall usually names the cause: `session.started` with no
`session.checkpoint` after it means the worker never reported progress.

### 5. Decide, and only then act

| What the trail shows | The action |
|----------------------|------------|
| An expired lease and no later activity | `hidratador decide M-1001 retry --reason "Reconciled the expired lease"` |
| A budget failure | Raise the budget deliberately, then `decide retry` — the counter is not reset by a retry |
| A `no_progress` failure | Change the approach or the context, then `decide retry` |
| An active session with a future lease | Wait. Taking the lease from a live worker is how two workers end up in the same scope |

### 6. Reconcile before you start again

```bash
hidratador reconcile
hidratador start M-1001 --owner terminal-2 --lease 900
```

`reconcile` expires every lease that has run out. Running it first means the new session starts
from a known state instead of inheriting a stale reservation.

---

## Housekeeping notes

| Item | Detail |
|------|--------|
| Schema version | Stored in the `meta` table. Current version is `3`. Migrations are additive and idempotent; they run automatically when the ledger is opened. |
| WAL files | `ledger.sqlite-wal` and `ledger.sqlite-shm` appear next to the ledger while it is open. They can be deleted once every process has closed the database. |
| Growth | The `events` table is append-only and the main source of growth. Context bodies are stored once per unique hash. |
| Compacting | `VACUUM INTO` produces a smaller copy and reclaims space in the copy, not in the original. |
| Permissions | The ledger file is `0600` and its directory `0700`. Verify on unusual filesystems — the chmod is best-effort. |
| Secrets | Never store tokens in the ledger, in context blobs, or in ticket files. The package never reads any, but it will happily persist whatever you attach. |

---

## Related pages

- [CLI reference](cli-reference.md) — every command used above
- [Failure playbook](failure-playbook.md) — recovery per failure category
- [Architecture](architecture.md) — the data model in detail
- [Security](security.md) — permissions and what the ledger contains
