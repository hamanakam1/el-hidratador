"""Local coordination ledger for El Hidratador.

The Context Expansion and Execution Staging Engine.

This module is the *core* boundary of the package:

* standard library only
* no network calls
* no shell execution
* no telemetry

All state lives in a single SQLite database opened in WAL mode with foreign
keys enabled. Every mutation is transactional, every mutation emits an event,
and every refusal raises a typed error instead of degrading silently.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .errors import BudgetExceeded, Conflict, LeaseExpired, NotFound, ValidationError

SCHEMA_VERSION = 3

#: Column and table names are identifiers, not values, so they are validated
#: against this pattern before they are interpolated into a statement.
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

#: The exact column definitions a migration is allowed to add. Anything else is
#: refused, so a column definition can never carry caller input.
ALLOWED_COLUMN_DDL = {
    "title": "TEXT NOT NULL DEFAULT ''",
    "priority": "INTEGER NOT NULL DEFAULT 2",
    "stop_reason": "TEXT NOT NULL DEFAULT ''",
}

#: A checkpoint whose evidence hash repeats this many times in a row is treated
#: as "no progress" and blocks the task. Three consecutive identical evidence
#: hashes means the worker is looping.
STAGNATION_LIMIT = 3


class TicketStatus(StrEnum):
    """Lifecycle stations of a hydrated task."""

    INBOX = "INBOX"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    REVIEW = "REVIEW"
    ACCEPTED = "ACCEPTED"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class TicketKind(StrEnum):
    """What kind of work a ticket describes."""

    CODE = "code"
    FIX = "fix"
    CLOSURE = "closure"
    PLANNING = "planning"
    PRODUCT = "product"


class FailureCategory(StrEnum):
    """Stable failure taxonomy. Grouped by fingerprint, never by free text."""

    LEASE_EXPIRED = "lease_expired"
    BUDGET = "budget"
    TOOL = "tool"
    DEPENDENCY = "dependency"
    NO_PROGRESS = "no_progress"


class Decision(StrEnum):
    """Operator decisions. Only an operator can produce one."""

    ACCEPT = "accept"
    RETRY = "retry"
    CANCEL = "cancel"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    FINISHED = "finished"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


def utc_iso(when: float | None = None) -> str:
    """Return an ISO-8601 UTC timestamp with millisecond precision."""
    stamp = time.time() if when is None else when
    return (
        datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{int((stamp % 1) * 1000):03d}Z"
    )


def fingerprint(category: str, summary: str) -> str:
    """Stable grouping key for recurring failures: sha256(category:summary)[:16]."""
    return hashlib.sha256(f"{category}:{summary}".encode()).hexdigest()[:16]


def evidence_hash(evidence: str) -> str:
    """Stable hash of a checkpoint's evidence payload."""
    return hashlib.sha256(evidence.encode()).hexdigest()


def _json_list(value: Any) -> list[str]:
    """Coerce a stored JSON column into a list of strings."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be nonempty text")
    return value


def _require_positive(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _require_nonnegative(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValidationError(f"{name} must be a nonnegative integer")
    return value


def _normalize_status(value: Any) -> str:
    text = str(value or "").strip().upper()
    try:
        return TicketStatus(text).value
    except ValueError as exc:
        allowed = ", ".join(status.value for status in TicketStatus)
        raise ValidationError(f"unknown status {value!r}; expected one of: {allowed}") from exc


@dataclass
class Ticket:
    """A dry semantic work package plus its hydration state."""

    id: str
    title: str
    objective: str
    acceptance: str
    kind: str = TicketKind.CODE.value
    scope: list[str] = field(default_factory=list)
    status: str = TicketStatus.INBOX.value
    priority: int = 2
    token_budget: int = 8000
    tags: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    owner: str = ""
    seed_key: str = ""
    description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "objective": self.objective,
            "acceptance": self.acceptance,
            "kind": self.kind,
            "scope": list(self.scope),
            "status": self.status,
            "priority": self.priority,
            "token_budget": self.token_budget,
            "tags": list(self.tags),
            "depends_on": list(self.depends_on),
            "blocks": list(self.blocks),
            "owner": self.owner,
            "seed_key": self.seed_key,
            "description": self.description,
            "acceptance_criteria": list(self.acceptance_criteria),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_at_iso": utc_iso(self.created_at),
            "updated_at_iso": utc_iso(self.updated_at),
        }


@dataclass
class Session:
    """An execution lease on a task."""

    id: str
    task_id: str
    owner: str
    status: str = SessionStatus.ACTIVE.value
    lease_until: float = 0.0
    tokens: int = 0
    progress: str = ""
    stagnant: int = 0
    created: float = field(default_factory=time.time)
    error: str = ""
    warning: str = ""
    stop_reason: str = ""

    def is_expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.lease_until

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "owner": self.owner,
            "status": self.status,
            "lease_until": self.lease_until,
            "lease_until_iso": utc_iso(self.lease_until) if self.lease_until else None,
            "tokens": self.tokens,
            "stagnant": self.stagnant,
            "created": self.created,
            "created_iso": utc_iso(self.created),
            "error": self.error,
            "warning": self.warning,
            "stop_reason": self.stop_reason,
        }


@dataclass
class Checkpoint:
    """Evidence recorded at a point in a session."""

    id: int
    session_id: str
    summary: str
    evidence: str
    next_step: str
    tokens: int
    created: float
    evidence_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "summary": self.summary,
            "evidence": self.evidence,
            "next_step": self.next_step,
            "tokens": self.tokens,
            "created": self.created,
            "created_iso": utc_iso(self.created),
            "evidence_hash": self.evidence_hash,
        }


@dataclass
class Failure:
    """A recorded, fingerprinted failure."""

    id: int
    task_id: str
    session_id: str | None
    category: str
    fingerprint: str
    summary: str
    created: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "category": self.category,
            "fingerprint": self.fingerprint,
            "summary": self.summary,
            "created": self.created,
            "created_iso": utc_iso(self.created),
        }


@dataclass
class Profile:
    """A declared compute profile (operator declaration, not provider proof)."""

    id: str
    kinds: list[str]
    context_limit: int
    cost: str
    verified_until: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kinds": list(self.kinds),
            "context_limit": self.context_limit,
            "cost": self.cost,
            "verified_until": self.verified_until,
            "verified_until_iso": utc_iso(self.verified_until),
        }


@dataclass
class Event:
    """An append-only audit record."""

    seq: int
    type: str
    task_id: str | None
    data: dict[str, Any]
    created: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type,
            "task_id": self.task_id,
            "data": self.data,
            "created": self.created,
            "created_iso": utc_iso(self.created),
        }


class Ledger:
    """The central coordination ledger for El Hidratador.

    Owns tasks, sessions, checkpoints, contexts, failures, profiles, events,
    handoffs, and the GitHub observation/outbox tables.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
            self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self._init_schema()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                objective TEXT NOT NULL,
                acceptance TEXT NOT NULL,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 2,
                token_budget INTEGER NOT NULL DEFAULT 8000,
                tags TEXT NOT NULL DEFAULT '[]',
                depends_on TEXT NOT NULL DEFAULT '[]',
                blocks TEXT NOT NULL DEFAULT '[]',
                owner TEXT NOT NULL DEFAULT '',
                seed_key TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS tickets_seed_key ON tickets(seed_key)
                WHERE seed_key <> '';

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                owner TEXT NOT NULL,
                status TEXT NOT NULL,
                lease_until REAL NOT NULL,
                tokens INTEGER NOT NULL DEFAULT 0,
                progress TEXT NOT NULL DEFAULT '',
                stagnant INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                warning TEXT NOT NULL DEFAULT '',
                stop_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_session ON sessions(task_id)
                WHERE status = 'active';

            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                evidence TEXT NOT NULL,
                next_step TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                created REAL NOT NULL,
                evidence_hash TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS checkpoints_session ON checkpoints(session_id);

            CREATE TABLE IF NOT EXISTS contexts (
                hash TEXT PRIMARY KEY,
                body TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_context (
                task_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                hash TEXT NOT NULL REFERENCES contexts(hash) ON DELETE CASCADE,
                source TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                expires REAL,
                PRIMARY KEY (task_id, hash)
            );

            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
                category TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                summary TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS failures_task ON failures(task_id);

            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                kinds TEXT NOT NULL,
                context_limit INTEGER NOT NULL,
                cost TEXT NOT NULL,
                verified_until REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                task_id TEXT,
                data TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS handoffs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
                manifest TEXT NOT NULL DEFAULT '{}',
                packet TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS observations (
                repo TEXT NOT NULL,
                kind TEXT NOT NULL,
                identity TEXT NOT NULL,
                revision TEXT NOT NULL DEFAULT '',
                observed TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (repo, kind, identity)
            );

            CREATE TABLE IF NOT EXISTS outbox (
                repo TEXT NOT NULL,
                issue INTEGER NOT NULL,
                key TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                url TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                PRIMARY KEY (repo, issue, key)
            );
            """
        )
        self._migrate()

    def _migrate(self) -> None:
        """Apply additive, idempotent schema migrations."""
        current = int(self._meta("schema_version", "0"))
        if current < 1:
            self._set_meta("schema_version", "1")
            current = 1
        if current < 2:
            # handoffs + ticket title/priority columns
            self._ensure_column("tickets", "title", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("tickets", "priority", "INTEGER NOT NULL DEFAULT 2")
            self._set_meta("schema_version", "2")
            current = 2
        if current < 3:
            self._ensure_column("sessions", "stop_reason", "TEXT NOT NULL DEFAULT ''")
            self._set_meta("schema_version", "3")
            current = 3
        self._set_meta("schema_version", str(SCHEMA_VERSION))

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        # Identifiers cannot be bound as parameters, so they are validated
        # against a strict pattern before they reach the statement.
        if not IDENTIFIER.match(table) or not IDENTIFIER.match(column):
            raise ValidationError(f"invalid identifier: {table}.{column}")
        if ALLOWED_COLUMN_DDL.get(column) != ddl:
            raise ValidationError(f"invalid column definition: {ddl}")
        columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @property
    def schema_version(self) -> int:
        return int(self._meta("schema_version", "0"))

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):  # pragma: no cover - defensive
            self.db.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block inside an immediate (write-locking) transaction."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def _event(self, _type: str, _task_id: str | None = None, **data: Any) -> None:
        """Append an audit event. Data keys are never allowed to shadow the call."""
        self.db.execute(
            "INSERT INTO events(type,task_id,data,created) VALUES (?,?,?,?)",
            (_type, _task_id, json.dumps(data, sort_keys=True, default=str), time.time()),
        )

    def events(self, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Return audit events with a sequence number greater than ``after``."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit)
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "type": row["type"],
                "task_id": row["task_id"],
                "data": json.loads(row["data"]),
                "created": row["created"],
                "created_iso": utc_iso(row["created"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    def _row_to_ticket(self, row: sqlite3.Row) -> Ticket:
        return Ticket(
            id=row["id"],
            title=row["title"],
            objective=row["objective"],
            acceptance=row["acceptance"],
            kind=row["kind"],
            scope=_json_list(row["scope"]),
            status=row["status"],
            priority=row["priority"],
            token_budget=row["token_budget"],
            tags=_json_list(row["tags"]),
            depends_on=_json_list(row["depends_on"]),
            blocks=_json_list(row["blocks"]),
            owner=row["owner"],
            seed_key=row["seed_key"],
            description=row["description"],
            acceptance_criteria=_json_list(row["acceptance_criteria"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def task(self, task_id: str) -> dict[str, Any]:
        """Return one task as a dictionary."""
        row = self.db.execute("SELECT * FROM tickets WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(f"unknown task: {task_id}")
        return self._row_to_ticket(row).to_dict()

    def get_ticket(self, task_id: str) -> Ticket:
        row = self.db.execute("SELECT * FROM tickets WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(f"unknown task: {task_id}")
        return self._row_to_ticket(row)

    def tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        """List tasks, optionally filtered by status."""
        if status:
            wanted = _normalize_status(status)
            rows = self.db.execute(
                "SELECT * FROM tickets WHERE status=? ORDER BY created_at", (wanted,)
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM tickets ORDER BY created_at").fetchall()
        return [self._row_to_ticket(row).to_dict() for row in rows]

    def add_task(
        self,
        task_id: str,
        objective: str,
        acceptance: str,
        title: str = "",
        kind: str = TicketKind.CODE.value,
        scope: Sequence[str] | None = None,
        token_budget: int = 8000,
        tags: Sequence[str] | None = None,
        depends_on: Sequence[str] | None = None,
        blocks: Sequence[str] | None = None,
        owner: str = "",
        seed_key: str = "",
        description: str = "",
        acceptance_criteria: Sequence[str] | None = None,
        priority: int = 2,
    ) -> dict[str, Any]:
        """Create a task. Idempotent when a ``seed_key`` repeats."""
        task_id = _require_text(task_id, "task id").strip()
        objective = _require_text(objective, "objective")
        acceptance = _require_text(acceptance, "acceptance")
        _require_positive(token_budget, "token_budget")
        scope_list = [str(item) for item in (scope or [task_id]) if str(item).strip()]
        if not scope_list:
            raise ValidationError("scope must contain at least one resource key")
        kind_value = str(kind or TicketKind.CODE.value).strip().lower()
        try:
            TicketKind(kind_value)
        except ValueError as exc:
            allowed = ", ".join(k.value for k in TicketKind)
            raise ValidationError(f"unknown kind {kind!r}; expected one of: {allowed}") from exc
        if not isinstance(priority, int) or not 1 <= priority <= 3:
            raise ValidationError("priority must be 1 (P0) .. 3 (P3)")

        with self.transaction():
            if seed_key:
                existing = self.db.execute(
                    "SELECT id FROM tickets WHERE seed_key=?", (seed_key,)
                ).fetchone()
                if existing:
                    return {"task_id": existing["id"], "created": False, "idempotent": True}

            now = time.time()
            self.db.execute(
                """INSERT INTO tickets(
                       id,title,objective,acceptance,kind,scope,status,priority,
                       token_budget,tags,depends_on,blocks,owner,seed_key,description,
                       acceptance_criteria,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    title or objective[:120],
                    objective,
                    acceptance,
                    kind_value,
                    json.dumps(scope_list),
                    TicketStatus.INBOX.value,
                    priority,
                    token_budget,
                    json.dumps([str(tag) for tag in (tags or [])]),
                    json.dumps([str(dep) for dep in (depends_on or [])]),
                    json.dumps([str(block) for block in (blocks or [])]),
                    str(owner or ""),
                    str(seed_key or ""),
                    str(description or ""),
                    json.dumps([str(item) for item in (acceptance_criteria or [])]),
                    now,
                    now,
                ),
            )
            self._event("task.created", task_id, kind=kind_value, scope=scope_list)
        return {"task_id": task_id, "created": True, "idempotent": False}

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """Update ticket fields without changing a held scope reservation.

        Lifecycle fields are not settable here. Scope keys remain fixed while
        a task is in progress, blocked, or awaiting review.
        """
        allowed = {
            "title",
            "objective",
            "acceptance",
            "kind",
            "scope",
            "token_budget",
            "tags",
            "depends_on",
            "blocks",
            "owner",
            "description",
            "acceptance_criteria",
            "priority",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValidationError(f"cannot update field(s): {', '.join(sorted(unknown))}")
        with self.transaction():
            ticket = self.get_ticket(task_id)
            assignments: list[str] = []
            values: list[Any] = []
            for key, value in fields.items():
                if not IDENTIFIER.match(key):
                    raise ValidationError(f"invalid column name: {key}")
                assignments.append(f"{key}=?")
                if key in ("scope", "tags", "depends_on", "blocks", "acceptance_criteria"):
                    items = [str(item) for item in value]
                    if (
                        key == "scope"
                        and ticket.status
                        in (
                            TicketStatus.IN_PROGRESS.value,
                            TicketStatus.BLOCKED.value,
                            TicketStatus.REVIEW.value,
                        )
                        and set(items) != set(ticket.scope)
                    ):
                        raise Conflict(
                            f"cannot change scope while task is {ticket.status}; "
                            "release the reservation through an operator decision first"
                        )
                    values.append(json.dumps(items))
                else:
                    values.append(value)
            if assignments:
                values.append(time.time())
                # Column names come from the allowlist above and every value is
                # bound as a parameter, so nothing caller-supplied is inlined.
                self.db.execute(
                    f"UPDATE tickets SET {', '.join(assignments)}, updated_at=? WHERE id=?",  # nosec B608
                    (*values, task_id),
                )
            self._event("task.updated", task_id, fields=sorted(fields))
        return self.task(task_id)

    def triage(self, task_id: str) -> dict[str, Any]:
        """Move a ticket from INBOX to READY once scope and budget are defined."""
        with self.transaction():
            ticket = self.get_ticket(task_id)
            if ticket.status != TicketStatus.INBOX.value:
                raise Conflict(f"task is {ticket.status}; expected INBOX")
            if not ticket.scope:
                raise Conflict("task has no scope; define scope before triage")
            self.db.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                (TicketStatus.READY.value, time.time(), task_id),
            )
            self._event("task.triaged", task_id)
        return self.task(task_id)

    # ------------------------------------------------------------------
    # sessions, leases, checkpoints
    # ------------------------------------------------------------------
    def _expire(self) -> list[str]:
        """Expire stale leases. Returns the affected task ids."""
        now = time.time()
        rows = self.db.execute(
            "SELECT id,task_id FROM sessions WHERE status='active' AND lease_until<=?", (now,)
        ).fetchall()
        affected: list[str] = []
        for row in rows:
            self.db.execute("UPDATE sessions SET status='expired' WHERE id=?", (row["id"],))
            self.db.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                (TicketStatus.BLOCKED.value, now, row["task_id"]),
            )
            self._record_failure(
                row["task_id"],
                row["id"],
                FailureCategory.LEASE_EXPIRED.value,
                "Ownership expired; reconcile the old worker before retrying.",
            )
            affected.append(row["task_id"])
        return affected

    def reconcile(self) -> dict[str, Any]:
        """Expire stale leases and report what changed."""
        with self.transaction():
            affected = self._expire()
        return {"expired_tasks": affected, "expired_count": len(affected)}

    def used_tokens(self, task_id: str) -> int:
        """Cumulative tokens spent on a task across every attempt."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM sessions WHERE task_id=?", (task_id,)
        ).fetchone()
        return int(row[0])

    def start(self, task_id: str, owner: str, lease_seconds: int = 900) -> dict[str, Any]:
        """Acquire an execution lease on a task."""
        owner = _require_text(owner, "owner")
        _require_positive(lease_seconds, "lease_seconds")
        self.reconcile()
        with self.transaction():
            ticket = self.get_ticket(task_id)
            if ticket.status not in (TicketStatus.INBOX.value, TicketStatus.READY.value):
                raise Conflict(f"task is {ticket.status}; expected READY or INBOX")
            used = self.used_tokens(task_id)
            if used >= ticket.token_budget:
                raise BudgetExceeded("task token budget exhausted across attempts")
            scopes = set(ticket.scope)
            conflicting = self.db.execute(
                "SELECT id,scope FROM tickets WHERE status IN (?,?,?) AND id<>?",
                (
                    TicketStatus.IN_PROGRESS.value,
                    TicketStatus.BLOCKED.value,
                    TicketStatus.REVIEW.value,
                    task_id,
                ),
            ).fetchall()
            for row in conflicting:
                overlap = scopes.intersection(_json_list(row["scope"]))
                if overlap:
                    raise Conflict(
                        f"resource scope reserved by {row['id']}: {', '.join(sorted(overlap))}"
                    )
            active = self.db.execute(
                "SELECT id FROM sessions WHERE task_id=? AND status='active'", (task_id,)
            ).fetchone()
            if active:
                raise Conflict(f"task already has active session {active['id']}")

            session_id = uuid.uuid4().hex
            now = time.time()
            self.db.execute(
                "INSERT INTO sessions(id,task_id,owner,status,lease_until,created) "
                "VALUES (?,?,?,'active',?,?)",
                (session_id, task_id, owner, now + lease_seconds, now),
            )
            self.db.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                (TicketStatus.IN_PROGRESS.value, now, task_id),
            )
            self._event(
                "session.started",
                task_id,
                session=session_id,
                owner=owner,
                lease_until=utc_iso(now + lease_seconds),
            )
        return {
            "session_id": session_id,
            "task_id": task_id,
            "lease_until": now + lease_seconds,
            "lease_until_iso": utc_iso(now + lease_seconds),
            "tokens_used": used,
            "token_budget": ticket.token_budget,
        }

    def _session_row(self, sid: str) -> sqlite3.Row:
        row: sqlite3.Row | None = self.db.execute(
            "SELECT * FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if row is None:
            raise NotFound(f"unknown session: {sid}")
        return row

    def session(self, sid: str) -> dict[str, Any]:
        row = self._session_row(sid)
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "owner": row["owner"],
            "status": row["status"],
            "lease_until": row["lease_until"],
            "tokens": row["tokens"],
            "stagnant": row["stagnant"],
            "created": row["created"],
            "error": row["error"],
            "warning": row["warning"],
            "stop_reason": row["stop_reason"],
        }

    def _session_or_conflict(self, sid: str) -> sqlite3.Row:
        row = self._session_row(sid)
        if row["status"] != SessionStatus.ACTIVE.value or row["lease_until"] <= time.time():
            raise LeaseExpired("session is inactive or expired")
        return row

    def checkpoint(
        self,
        sid: str,
        summary: str,
        evidence: str,
        next_step: str,
        tokens: int,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Record progress. Requires summary, evidence, next step, and tokens."""
        summary = _require_text(summary, "summary")
        evidence = _require_text(evidence, "evidence")
        next_step = _require_text(next_step, "next_step")
        _require_nonnegative(tokens, "tokens")
        _require_positive(lease_seconds, "lease_seconds")
        self.reconcile()
        with self.transaction():
            session = self._session_or_conflict(sid)
            if tokens < session["tokens"]:
                raise Conflict("cumulative token count cannot decrease")
            digest = evidence_hash(evidence)
            stagnant = session["stagnant"] + 1 if digest == session["progress"] else 0
            ticket = self.get_ticket(session["task_id"])
            other_tokens = self.db.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM sessions WHERE task_id=? AND id<>?",
                (session["task_id"], sid),
            ).fetchone()[0]
            exhausted = (other_tokens + tokens) >= ticket.token_budget
            no_progress = stagnant >= STAGNATION_LIMIT - 1
            blocked = exhausted or no_progress
            now = time.time()
            self.db.execute(
                "UPDATE sessions SET tokens=?,progress=?,stagnant=?,lease_until=?,status=? WHERE id=?",
                (
                    tokens,
                    digest,
                    stagnant,
                    now + lease_seconds,
                    SessionStatus.FAILED.value if blocked else SessionStatus.ACTIVE.value,
                    sid,
                ),
            )
            self.db.execute(
                "INSERT INTO checkpoints(session_id,summary,evidence,next_step,tokens,created,evidence_hash) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, summary, evidence, next_step, tokens, now, digest),
            )
            self._event(
                "session.checkpoint",
                session["task_id"],
                session=sid,
                tokens=tokens,
                stagnant=stagnant,
                evidence_hash=digest,
            )
            if blocked:
                self.db.execute(
                    "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                    (TicketStatus.BLOCKED.value, now, session["task_id"]),
                )
                if exhausted:
                    self._record_failure(
                        session["task_id"],
                        sid,
                        FailureCategory.BUDGET.value,
                        "Task token budget reached.",
                    )
                else:
                    self._record_failure(
                        session["task_id"],
                        sid,
                        FailureCategory.NO_PROGRESS.value,
                        "Three consecutive checkpoints cite identical evidence.",
                    )
        return {
            "session_id": sid,
            "status": "blocked" if blocked else "active",
            "tokens": tokens,
            "stagnant": stagnant,
            "evidence_hash": digest,
        }

    def checkpoints(self, sid: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM checkpoints WHERE session_id=? ORDER BY id", (sid,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "summary": row["summary"],
                "evidence": row["evidence"],
                "next_step": row["next_step"],
                "tokens": row["tokens"],
                "created": row["created"],
                "created_iso": utc_iso(row["created"]),
                "evidence_hash": row["evidence_hash"],
            }
            for row in rows
        ]

    def _record_failure(
        self, task_id: str, session_id: str | None, category: str, summary: str
    ) -> str:
        digest = fingerprint(category, summary)
        self.db.execute(
            "INSERT INTO failures(task_id,session_id,category,fingerprint,summary,created) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, session_id, category, digest, summary, time.time()),
        )
        self._event(
            "failure.recorded",
            task_id,
            session=session_id,
            category=category,
            fingerprint=digest,
        )
        return digest

    def fail(self, sid: str, category: str, summary: str) -> dict[str, Any]:
        """Record an explicit failure and block the task."""
        category = _require_text(category, "category").strip().lower()
        summary = _require_text(summary, "summary")
        try:
            FailureCategory(category)
        except ValueError as exc:
            allowed = ", ".join(c.value for c in FailureCategory)
            raise ValidationError(
                f"unknown failure category {category!r}; expected: {allowed}"
            ) from exc
        self.reconcile()
        with self.transaction():
            session = self._session_row(sid)
            if session["status"] != SessionStatus.ACTIVE.value:
                raise Conflict(f"session is {session['status']}; expected active")
            self.db.execute(
                "UPDATE sessions SET status=?, error=? WHERE id=?",
                (SessionStatus.FAILED.value, summary, sid),
            )
            self.db.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                (TicketStatus.BLOCKED.value, time.time(), session["task_id"]),
            )
            digest = self._record_failure(session["task_id"], sid, category, summary)
        return {
            "status": TicketStatus.BLOCKED.value,
            "task_id": session["task_id"],
            "category": category,
            "fingerprint": digest,
        }

    def finish(self, sid: str, evidence: str) -> dict[str, Any]:
        """Request review. Acceptance is a separate, explicit operator decision."""
        evidence = _require_text(evidence, "evidence")
        self.reconcile()
        with self.transaction():
            session = self._session_or_conflict(sid)
            has_checkpoint = self.db.execute(
                "SELECT 1 FROM checkpoints WHERE session_id=? LIMIT 1", (sid,)
            ).fetchone()
            if not has_checkpoint:
                raise Conflict("record a checkpoint with usage and evidence before finishing")
            self.db.execute(
                "UPDATE sessions SET status=? WHERE id=?", (SessionStatus.FINISHED.value, sid)
            )
            self.db.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                (TicketStatus.REVIEW.value, time.time(), session["task_id"]),
            )
            self._event("session.finished", session["task_id"], session=sid, evidence=evidence)
        return {"status": TicketStatus.REVIEW.value, "task_id": session["task_id"]}

    def decide(self, task_id: str, decision: str, reason: str) -> dict[str, Any]:
        """Operator decision: accept, retry, or cancel.

        Only ``accept`` moves work toward DONE, and it never happens implicitly.
        """
        reason = _require_text(reason, "reason")
        value = str(decision or "").strip().lower()
        try:
            choice = Decision(value)
        except ValueError as exc:
            allowed = ", ".join(d.value for d in Decision)
            raise ValidationError(f"unknown decision {decision!r}; expected: {allowed}") from exc
        self.reconcile()
        with self.transaction():
            ticket = self.get_ticket(task_id)
            allowed_from = {
                Decision.ACCEPT: {TicketStatus.REVIEW.value},
                Decision.RETRY: {TicketStatus.BLOCKED.value, TicketStatus.REVIEW.value},
                Decision.CANCEL: {
                    TicketStatus.READY.value,
                    TicketStatus.BLOCKED.value,
                    TicketStatus.REVIEW.value,
                },
            }
            if ticket.status not in allowed_from[choice]:
                raise Conflict(f"cannot {choice.value} a {ticket.status} task")
            now = time.time()
            if choice is Decision.ACCEPT:
                self.db.execute(
                    "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                    (TicketStatus.ACCEPTED.value, now, task_id),
                )
                self._event("operator.accept", task_id, reason=reason)
                self.db.execute(
                    "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                    (TicketStatus.DONE.value, now, task_id),
                )
                self._event("task.done", task_id, accepted_by="operator")
                status = TicketStatus.DONE.value
            elif choice is Decision.RETRY:
                self.db.execute(
                    "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                    (TicketStatus.READY.value, now, task_id),
                )
                self._event("operator.retry", task_id, reason=reason)
                status = TicketStatus.READY.value
            else:
                self.db.execute(
                    "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                    (TicketStatus.CANCELLED.value, now, task_id),
                )
                self._event("operator.cancel", task_id, reason=reason)
                status = TicketStatus.CANCELLED.value
        return {"task_id": task_id, "status": status, "decision": choice.value}

    # ------------------------------------------------------------------
    # context
    # ------------------------------------------------------------------
    def add_context(
        self,
        task_id: str,
        body: str,
        source: str,
        priority: int = 0,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        """Attach a context blob to a task. Content-addressed and deduplicated."""
        body = _require_text(body, "body")
        source = _require_text(source, "source")
        if not isinstance(priority, int):
            raise ValidationError("priority must be an integer")
        if ttl is not None:
            _require_positive(ttl, "ttl")
        digest = evidence_hash(body)
        with self.transaction():
            self.get_ticket(task_id)
            self.db.execute(
                "INSERT OR IGNORE INTO contexts(hash,body) VALUES (?,?)", (digest, body)
            )
            self.db.execute(
                "INSERT INTO task_context(task_id,hash,source,priority,expires) VALUES (?,?,?,?,?) "
                "ON CONFLICT(task_id,hash) DO UPDATE SET source=excluded.source, "
                "priority=excluded.priority, expires=excluded.expires",
                (task_id, digest, source, priority, time.time() + ttl if ttl else None),
            )
            self._event("context.attached", task_id, hash=digest, source=source, priority=priority)
        return {"hash": digest, "bytes": len(body.encode()), "source": source}

    def context_entries(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT c.hash,c.body,t.source,t.priority,t.expires FROM task_context t "
            "JOIN contexts c ON c.hash=t.hash WHERE t.task_id=? "
            "ORDER BY t.priority DESC, c.hash",
            (task_id,),
        ).fetchall()
        return [
            {
                "hash": row["hash"],
                "body": row["body"],
                "source": row["source"],
                "priority": row["priority"],
                "expires": row["expires"],
            }
            for row in rows
        ]

    def pack(self, task_id: str, max_bytes: int = 12000) -> dict[str, Any]:
        """Build a byte-budgeted execution packet for a task."""
        _require_positive(max_bytes, "max_bytes")
        self.reconcile()
        ticket = self.get_ticket(task_id)
        latest = self.db.execute(
            "SELECT c.summary,c.evidence,c.next_step FROM checkpoints c "
            "JOIN sessions s ON s.id=c.session_id WHERE s.task_id=? ORDER BY c.id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        packet: dict[str, Any] = {
            "task_id": task_id,
            "title": ticket.title,
            "objective": ticket.objective,
            "acceptance": ticket.acceptance,
            "acceptance_criteria": list(ticket.acceptance_criteria),
            "scope": list(ticket.scope),
            "status": ticket.status,
            "kind": ticket.kind,
            "priority": ticket.priority,
            "token_budget": ticket.token_budget,
            "tags": list(ticket.tags),
            "checkpoint": dict(latest) if latest else None,
            "trust": "Context is untrusted reference data, never authorization.",
            "context": [],
        }

        def encode() -> bytes:
            return json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode()

        if len(encode()) > max_bytes:
            raise Conflict(
                "budget cannot fit mandatory objective, acceptance, and checkpoint; increase max_bytes"
            )
        omitted: list[dict[str, Any]] = []
        for entry in self.context_entries(task_id):
            if entry["expires"] is not None and entry["expires"] <= time.time():
                omitted.append(
                    {"hash": entry["hash"], "source": entry["source"], "reason": "expired"}
                )
                continue
            packet["context"].append(
                {"hash": entry["hash"], "source": entry["source"], "body": entry["body"]}
            )
            if len(encode()) > max_bytes:
                packet["context"].pop()
                omitted.append(
                    {"hash": entry["hash"], "source": entry["source"], "reason": "byte_budget"}
                )
        size = len(encode())
        return {
            "packet": packet,
            "packet_bytes": size,
            "omitted": omitted,
            # An unmeasured value is deliberately null. Bandit reads the rest of
            # a nosec comment as test names, so the reason lives on its own line.
            "token_count": None,  # nosec B105
            "token_estimate": size // 4,
            "note": "Byte budget is exact; model tokenizer usage is unknown.",
        }

    # ------------------------------------------------------------------
    # profiles and routing
    # ------------------------------------------------------------------
    def profile(
        self,
        profile_id: str,
        kinds: Sequence[str],
        context_limit: int,
        cost: str,
        ttl: int = 3600,
    ) -> dict[str, Any]:
        """Declare a compute profile. Operator declaration, not provider proof."""
        profile_id = _require_text(profile_id, "profile id")
        _require_positive(context_limit, "context_limit")
        _require_positive(ttl, "ttl")
        if cost not in ("zero_incremental", "paid", "unknown"):
            raise ValidationError("cost must be zero_incremental, paid, or unknown")
        kind_list = [str(kind).strip() for kind in kinds if str(kind).strip()]
        if not kind_list:
            raise ValidationError("at least one task kind is required")
        for kind in kind_list:
            try:
                TicketKind(kind)
            except ValueError as exc:
                allowed = ", ".join(k.value for k in TicketKind)
                raise ValidationError(f"unknown kind {kind!r}; expected one of: {allowed}") from exc
        with self.transaction():
            self.db.execute(
                "INSERT INTO profiles(id,kinds,context_limit,cost,verified_until) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET kinds=excluded.kinds, "
                "context_limit=excluded.context_limit, cost=excluded.cost, "
                "verified_until=excluded.verified_until",
                (
                    profile_id,
                    json.dumps(kind_list),
                    context_limit,
                    cost,
                    time.time() + ttl,
                ),
            )
            self._event("profile.declared", None, profile=profile_id, cost=cost)
        return {
            "profile": profile_id,
            "kinds": kind_list,
            "context_limit": context_limit,
            "cost": cost,
            "provenance": "operator declaration; not provider verification",
        }

    def profiles(self) -> list[Profile]:
        rows = self.db.execute("SELECT * FROM profiles ORDER BY id").fetchall()
        return [
            Profile(
                id=row["id"],
                kinds=_json_list(row["kinds"]),
                context_limit=row["context_limit"],
                cost=row["cost"],
                verified_until=row["verified_until"],
            )
            for row in rows
        ]

    def route(self, task_id: str, required_context: int = 4096) -> dict[str, Any]:
        """Rank declared profiles for a task. Eligibility is not authorization."""
        _require_positive(required_context, "required_context")
        ticket = self.get_ticket(task_id)
        now = time.time()
        options: list[dict[str, Any]] = []
        for profile in self.profiles():
            reasons: list[str] = []
            if ticket.kind not in profile.kinds:
                reasons.append("task kind unsupported")
            if profile.context_limit < required_context:
                reasons.append("insufficient declared context capacity")
            if profile.cost != "zero_incremental":
                reasons.append("new spending is not authorized or cost is unknown")
            if profile.verified_until <= now:
                reasons.append("profile declaration expired")
            options.append(
                {
                    "profile": profile.id,
                    "eligible": not reasons,
                    "reasons": reasons,
                    "kinds": list(profile.kinds),
                    "context_limit": profile.context_limit,
                    "cost": profile.cost,
                    "quality_estimate": None,
                    "provenance": "operator declaration",
                }
            )
        return {
            "task_id": task_id,
            "options": options,
            "recommendation": "Eligible options are unranked until outcome evidence exists.",
            "execution_authorized": False,
        }

    # ------------------------------------------------------------------
    # handoff
    # ------------------------------------------------------------------
    def record_handoff(
        self,
        task_id: str,
        manifest: dict[str, Any],
        packet: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Store the execution handoff payload handed to Taskrivet."""
        handoff_id = uuid.uuid4().hex
        with self.transaction():
            self.get_ticket(task_id)
            self.db.execute(
                "INSERT INTO handoffs(id,task_id,session_id,manifest,packet,created) VALUES (?,?,?,?,?,?)",
                (
                    handoff_id,
                    task_id,
                    session_id,
                    json.dumps(manifest, sort_keys=True, default=str),
                    json.dumps(packet, sort_keys=True, default=str),
                    time.time(),
                ),
            )
            self._event("handoff.recorded", task_id, handoff=handoff_id, session=session_id)
        return {"handoff_id": handoff_id, "task_id": task_id}

    def handoffs(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM handoffs WHERE task_id=? ORDER BY created DESC", (task_id,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "session_id": row["session_id"],
                "manifest": json.loads(row["manifest"]),
                "packet": json.loads(row["packet"]),
                "created": row["created"],
                "created_iso": utc_iso(row["created"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def failures(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id:
            rows = self.db.execute(
                "SELECT * FROM failures WHERE task_id=? ORDER BY created DESC", (task_id,)
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM failures ORDER BY created DESC").fetchall()
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "session_id": row["session_id"],
                "category": row["category"],
                "fingerprint": row["fingerprint"],
                "summary": row["summary"],
                "created": row["created"],
                "created_iso": utc_iso(row["created"]),
            }
            for row in rows
        ]

    def failure_summary(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT fingerprint,category,summary,COUNT(*) AS occurrences,MAX(created) AS last_seen "
            "FROM failures GROUP BY fingerprint,category,summary ORDER BY occurrences DESC, last_seen DESC"
        ).fetchall()
        return [
            {
                "fingerprint": row["fingerprint"],
                "category": row["category"],
                "summary": row["summary"],
                "occurrences": row["occurrences"],
                "last_seen": row["last_seen"],
                "last_seen_iso": utc_iso(row["last_seen"]),
            }
            for row in rows
        ]

    def status(self) -> dict[str, Any]:
        """A snapshot of the whole ledger."""
        self.reconcile()
        tasks = self.tasks()
        by_status: dict[str, int] = {}
        for ticket in tasks:
            by_status[ticket["status"]] = by_status.get(ticket["status"], 0) + 1
        sessions = [
            dict(row)
            for row in self.db.execute("SELECT * FROM sessions ORDER BY created").fetchall()
        ]
        accepted = by_status.get(TicketStatus.ACCEPTED.value, 0) + by_status.get(
            TicketStatus.DONE.value, 0
        )
        return {
            "schema_version": self.schema_version,
            "database": str(self.path),
            "tasks": tasks,
            "tasks_by_status": by_status,
            "sessions": sessions,
            "failures": self.failure_summary(),
            "metrics": {
                "accepted": accepted,
                "reported_tokens": int(
                    self.db.execute("SELECT COALESCE(SUM(tokens),0) FROM sessions").fetchone()[0]
                ),
                # An unmeasured value is deliberately null; see the note above.
                "measured_cash_cost": None,  # nosec B105
                "measured_token_savings": None,  # nosec B105
                "note": "Reported tokens are declared by workers, not metered by a provider.",
            },
        }


def create_ledger(path: str | Path = "~/.el-hidratador/ledger.sqlite") -> Ledger:
    """Create (or open) a ledger at ``path``."""
    return Ledger(path)


def default_db_path() -> Path:
    """The default ledger location, overridable with ``HIDRATADOR_DB``."""
    import os

    env = os.environ.get("HIDRATADOR_DB")
    if env:
        return Path(env).expanduser()
    return Path("~/.el-hidratador/ledger.sqlite").expanduser()
