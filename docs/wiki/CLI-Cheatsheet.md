# CLI Cheatsheet

Global flags: `--db PATH`, `--compact`, `--version`.

| Command | What it does |
|---------|--------------|
| `init` | Create the ledger |
| `status` | Snapshot of tasks, sessions, failures, and metrics |
| `reconcile` | Expire stale leases and report what changed |
| `events [--after N] [--follow]` | Read the audit log, or follow new events |
| `task-add ID ...` | Create a task (idempotent with `--seed-key`) |
| `task-show ID` | One task as JSON |
| `task-list [--status S]` | List tasks |
| `triage ID` | `INBOX` → `READY` |
| `start ID --owner O [--lease S]` | Acquire an execution lease |
| `checkpoint SID --summary --evidence --next --tokens` | Record progress |
| `fail SID --category C --summary S` | Record a typed failure |
| `finish SID --evidence E` | Request review |
| `decide ID accept\|retry\|cancel --reason R` | Operator decision |
| `context-add ID (--file F \| --text T) [--source-label L] [--priority P] [--ttl S]` | Attach context |
| `pack ID [--max-bytes B] [--packet-only]` | Byte-budgeted execution packet |
| `expand ID [--max-bytes B] [--prompt]` | Hydrated prompt |
| `dag ID` | Execution DAG and manifest |
| `handoff ID [--max-bytes B] [--workdir P]` | Taskrivet handoff payload |
| `profile-add ID --kind K --context-limit N [--cost C] [--ttl S]` | Declare a compute profile |
| `route ID [--required-context N]` | Eligibility, never authorization |
| `export [--dir D]` | Write tickets to a file-backed store |
| `github-poll --repo O/R [--apply]` | Observe a repository (read only) |
| `github-publish --repo O/R --issue N --key K --body B [--apply]` | Queue and publish an idempotent comment |
| `dashboard-preview --url U [--status S] [--offset N] [--limit N]` | Preview a local dashboard |

Exit codes: `0` success, `2` refused operation, `3` adapter failure.

## Useful combinations

```bash
# What is blocked and why
hidratador status | jq '.failures'

# What changed since the last look
hidratador events --after 42 | jq '.events[].type'

# The exact prompt a worker would receive
hidratador expand M-1001 --prompt | jq -r .prompt
```
