# ADR 0003: Leases and scope keys instead of an external coordinator

## Status

Accepted

## Context

Two workers must not modify the same resource at the same time. The coordination has to survive
the case this project cares about most: a worker that dies without cleaning up after itself. It
also has to work on a machine with nothing else running on it, because a coordination daemon is
another thing to install, secure, monitor, and keep alive. The reservation must be a fact that
can be read out of the ledger with ordinary SQL, because the first question anyone asks when two
tasks disagree is "who holds it right now".

## Decision

Exclusive ownership is expressed as a **lease** over a set of **scope keys**. `start` creates a
session row with an explicit `lease_until` timestamp — 900 seconds by default — and moves the
ticket to `IN_PROGRESS`. Scope keys are named resources such as `repo/api` or `terminal-1`;
`start` refuses with `resource scope reserved by <id>: <keys>` when another live ticket holds an
overlapping key. A checkpoint renews the lease. When the lease runs out, `reconcile()` marks the
session `expired`, moves the task to `BLOCKED`, and records a `lease_expired` failure — and every
mutating command runs `reconcile()` first, so no daemon has to watch the clock.

## Consequences

### Accepted benefits

- Crash recovery needs no daemon. The lease expiry is a timestamp in a row, so the next command
  that touches the ledger notices the dead worker on its own.
- The reservation is legible. `SELECT id, scope FROM tickets WHERE status IN (...)` answers
  "who holds this resource" without any project-specific tooling.
- `Ledger.start()` checks for overlapping reservations, and `Ledger.update_task()` prevents a
  ticket from changing its reserved resource set while in `IN_PROGRESS`, `BLOCKED`, or `REVIEW`.
  Workers cannot release a reservation by editing the ticket's scope.
- Scope keys make the operator's intent explicit and reviewable. A ticket that says it touches
  `repo/api` is a claim that can be checked against what the ticket actually did.
- A partial unique index on `sessions(task_id) WHERE status = 'active'` means the database itself
  refuses two live leases on one task, even if two workers race for it.

### Accepted costs

- Scope keys are declared, not discovered. The ledger cannot know which files a task will touch,
  so a key that is too coarse blocks unrelated work and a key that is too narrow protects nothing.
- Renewal happens as a side effect of `checkpoint()`, so a worker that stalls for longer than the
  lease loses its ownership even though it is still alive.
- A task in `BLOCKED` or `REVIEW` keeps its reservation, so a forgotten review holds a resource
  and blocks other work until an operator decides.
- Lease duration is a guess about worker behavior, and a wrong guess shows up as either spurious
  expiry or an over-long hold.

## Alternatives considered

- **OS file locks (`flock`, `fcntl`).** Rejected: they are advisory, tied to a filesystem rather
  than to a task, released when a process exits, and meaningless across machines. They also cannot
  express "this ticket owns this repository" as a reviewable fact.
- **A Redis lease server.** Rejected: it introduces a service, a failure mode where the lease
  store is unavailable, and a second piece of state to back up and reconcile with the ledger.
- **Git worktrees as the isolation unit.** Rejected: it isolates the working tree but says nothing
  about the ticket, and it does nothing to stop two tickets from editing the same branch.
- **A queue with a visibility timeout.** Rejected: it assumes work is dispatched by the queue,
  whereas here a worker claims a ticket that a human has already approved, and the interesting
  case is two workers acting on overlapping tickets.
- **Optimistic concurrency, detecting the conflict at commit time.** Rejected: it turns a
  preventable collision into a late failure, after both workers have already spent their budget.
- **Redlock.** Rejected: it is a distributed lock algorithm for shared Redis, which is a heavier
  problem than a single local ledger presents.

## Related pages

- [Architecture](https://github.com/hamanakam1/el-hidratador/blob/main/docs/architecture.md) — the
  lifecycle and its refusals
- [Failure playbook](https://github.com/hamanakam1/el-hidratador/blob/main/docs/failure-playbook.md)
  — recovering from lease expiry
