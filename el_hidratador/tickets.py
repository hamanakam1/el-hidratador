"""File-backed ticket storage.

The ledger is the authority for state. The file tree is the *portable* form:
a ticket directory that can be copied, reviewed in a diff, and archived without
a running process.

Layout::

    .hidratador/
    ├── tickets/
    │   ├── M-397001.md
    │   └── ...
    ├── index.json
    └── evidence/
        └── M-397001/
            ├── checkpoint-1.json
            └── finish.json

Files are written atomically so an interrupted write never leaves a torn
ticket on disk.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .errors import Conflict, NotFound, ValidationError

TICKET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_EVIDENCE_COLLISION_ATTEMPTS = 128


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value if value is not None else "")
    if not text:
        return '""'
    if any(char in text for char in ":#\"'\n") or text.strip() != text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return text


def render_front_matter(data: dict[str, Any]) -> str:
    """Render a metadata mapping as YAML front matter."""
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, (list, tuple)):
            items = ", ".join(_yaml_scalar(item) for item in value)
            lines.append(f"{key}: [{items}]")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def render_ticket(ticket: dict[str, Any], body: str = "") -> str:
    """Render a ticket as markdown with front matter."""
    meta = {
        "id": ticket.get("id", ""),
        "title": ticket.get("title", ""),
        "status": ticket.get("status", "INBOX"),
        "priority": ticket.get("priority", 2),
        "kind": ticket.get("kind", "code"),
        "scope": list(ticket.get("scope") or []),
        "token_budget": ticket.get("token_budget", 8000),
        "tags": list(ticket.get("tags") or []),
        "depends_on": list(ticket.get("depends_on") or []),
        "blocks": list(ticket.get("blocks") or []),
        "owner": ticket.get("owner", ""),
        "seed_key": ticket.get("seed_key", ""),
        "created_at": ticket.get("created_at_iso", ""),
        "updated_at": ticket.get("updated_at_iso", ""),
    }
    parts = [render_front_matter(meta), ""]
    description = body.strip() or ticket.get("description", "")
    if description:
        parts.extend(["## Description", description, ""])
    criteria = ticket.get("acceptance_criteria") or []
    if criteria:
        parts.append("## Acceptance Criteria")
        parts.extend(f"- [ ] {item}" for item in criteria)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def parse_front_matter(text: str) -> dict[str, Any]:
    """Parse the front matter of a ticket file into a mapping."""
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}
    meta: dict[str, Any] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            items = [item.strip().strip('"') for item in raw[1:-1].split(",")]
            meta[key.strip()] = [item for item in items if item]
        else:
            meta[key.strip()] = raw.strip('"')
    return meta


class TicketStore:
    """A portable, file-backed ticket directory."""

    def __init__(self, root: Path | str = ".hidratador") -> None:
        self.root = Path(root).expanduser()

    @property
    def tickets_dir(self) -> Path:
        return self.root / "tickets"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _validate_id(self, task_id: str) -> str:
        text = str(task_id or "").strip()
        if not TICKET_ID.match(text):
            raise ValidationError(f"invalid ticket id: {task_id!r}")
        return text

    def write_ticket(self, ticket: dict[str, Any], body: str = "") -> Path:
        """Write one ticket file and refresh the index."""
        task_id = self._validate_id(ticket.get("id", ""))
        path = self.tickets_dir / f"{task_id}.md"
        _atomic_write(path, render_ticket(ticket, body))
        self.rebuild_index()
        return path

    def read_ticket(self, task_id: str) -> dict[str, Any]:
        """Read one ticket file."""
        task_id = self._validate_id(task_id)
        path = self.tickets_dir / f"{task_id}.md"
        if not path.exists():
            raise NotFound(f"unknown ticket file: {task_id}")
        text = path.read_text(encoding="utf-8")
        return {"meta": parse_front_matter(text), "body": text}

    def list_tickets(self) -> list[dict[str, Any]]:
        """List every ticket file with its metadata."""
        if not self.tickets_dir.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self.tickets_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            meta = parse_front_matter(text)
            entries.append({"file": path.name, **meta})
        return entries

    def rebuild_index(self) -> Path:
        """Rewrite ``index.json`` from the ticket files."""
        index: dict[str, Any] = {}
        for entry in self.list_tickets():
            task_id = entry.get("id") or Path(entry["file"]).stem
            index[task_id] = {
                "title": entry.get("title", ""),
                "status": entry.get("status", "INBOX"),
                "kind": entry.get("kind", "code"),
                "priority": entry.get("priority", "2"),
                "file": entry["file"],
            }
        _atomic_write(self.index_path, json.dumps(index, indent=2, sort_keys=True) + "\n")
        return self.index_path

    def write_evidence(self, task_id: str, kind: str, payload: dict[str, Any]) -> Path:
        """Publish one complete evidence record without replacing prior records."""
        task_id = self._validate_id(task_id)
        safe_kind = re.sub(r"[^a-z0-9._-]+", "-", str(kind or "record").lower()).strip("-")
        if not safe_kind or len(safe_kind) > 64:
            raise ValidationError("evidence kind must contain 1 to 64 filename characters")
        text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
        directory = self.evidence_dir / task_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        pattern = re.compile(rf"{re.escape(safe_kind)}(?:-([0-9]+))?\.json")
        sequence = 1
        for existing in directory.iterdir():
            match = pattern.fullmatch(existing.name)
            if match:
                sequence = max(sequence, int(match.group(1) or 1) + 1)

        handle, temp_name = tempfile.mkstemp(dir=str(directory), prefix=".tmp-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            for _ in range(_EVIDENCE_COLLISION_ATTEMPTS):
                filename = f"{safe_kind}.json" if sequence == 1 else f"{safe_kind}-{sequence}.json"
                path = directory / filename
                try:
                    # Linking a complete private file publishes it atomically,
                    # and refuses any existing destination, even a symlink.
                    os.link(temp_name, path)
                except FileExistsError:
                    sequence += 1
                    continue
                return path
            raise Conflict("evidence publication collided repeatedly; retry the write")
        finally:
            os.unlink(temp_name)

    def evidence(self, task_id: str) -> list[dict[str, Any]]:
        """Read every evidence record for a task."""
        task_id = self._validate_id(task_id)
        directory = self.evidence_dir / task_id
        if not directory.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                records.append({"file": path.name, **json.loads(path.read_text(encoding="utf-8"))})
            except json.JSONDecodeError:
                records.append({"file": path.name, "error": "malformed evidence file"})
        return records

    def export_all(self, tickets: list[dict[str, Any]]) -> dict[str, Any]:
        """Write every ticket from a ledger into the store."""
        written: list[str] = []
        for ticket in tickets:
            path = self.write_ticket(ticket)
            written.append(path.name)
        return {"root": str(self.root), "written": written, "index": str(self.index_path)}
