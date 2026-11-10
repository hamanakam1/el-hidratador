"""Published schemas and a dependency-free validator.

The handoff between El Hidratador and whatever executes next is a contract.
This module publishes that contract as JSON Schema and ships a small validator
for it, so the contract can be checked without adding a dependency to the
standard-library-only core.

The validator supports the subset of JSON Schema these schemas use:

* ``type`` (including a list of types)
* ``properties``, ``required``, ``additionalProperties``
* ``items``
* ``const``, ``enum``
* ``minimum``, ``maximum``, ``exclusiveMinimum``, ``exclusiveMaximum``
* ``minLength``, ``maxLength``, ``minItems``, ``maxItems``
* ``pattern``
* ``$ref`` against the local ``#/$defs/...`` definitions
* ``anyOf`` and ``oneOf``

Anything it cannot express is not used by the published schemas, and the test
suite asserts that the schemas and the validator agree.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .errors import ValidationError

SCHEMA_VERSION = "1.0.0"

_DRAFT = "https://json-schema.org/draft/2020-12/schema"

_COMMON = {
    "$schema": _DRAFT,
    "$id": "https://hamanakam1.github.io/el-hidratador/schema/{name}.json",
    "title": "El Hidratador {title}",
    "description": "Published by El Hidratador {version}. See the repository for the full contract.",
}

_TASK_ID = {
    "type": "string",
    "description": "Ticket identifier, for example M-1001.",
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
}

_TICKET: dict[str, Any] = {
    "type": "object",
    "required": ["id", "objective", "acceptance", "status"],
    "additionalProperties": False,
    "properties": {
        "id": _TASK_ID,
        "title": {"type": "string"},
        "objective": {"type": "string", "minLength": 1},
        "acceptance": {"type": "string", "minLength": 1},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "kind": {
            "type": "string",
            "enum": ["code", "fix", "closure", "planning", "product"],
        },
        "scope": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
        "status": {
            "type": "string",
            "enum": [
                "INBOX",
                "READY",
                "IN_PROGRESS",
                "BLOCKED",
                "REVIEW",
                "ACCEPTED",
                "DONE",
                "CANCELLED",
            ],
        },
        "priority": {"type": "integer", "minimum": 1, "maximum": 3},
        "token_budget": {"type": "integer", "minimum": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "blocks": {"type": "array", "items": {"type": "string"}},
        "owner": {"type": "string"},
        "seed_key": {"type": "string"},
        "description": {"type": "string"},
        "created_at": {"type": "number"},
        "updated_at": {"type": "number"},
        "created_at_iso": {"type": "string"},
        "updated_at_iso": {"type": "string"},
    },
}

_CONTEXT_ENTRY = {
    "type": "object",
    "required": ["hash", "source", "body"],
    "additionalProperties": False,
    "properties": {
        "hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source": {"type": "string", "minLength": 1},
        "body": {"type": "string"},
    },
}

_PACKET: dict[str, Any] = {
    "type": "object",
    "required": ["task_id", "objective", "acceptance", "scope", "status", "trust", "context"],
    "additionalProperties": True,
    "properties": {
        "task_id": _TASK_ID,
        "title": {"type": "string"},
        "objective": {"type": "string", "minLength": 1},
        "acceptance": {"type": "string", "minLength": 1},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "scope": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string"},
        "priority": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string"},
        "files": {"type": "array", "items": {"type": "object"}},
        "tests": {"type": "array", "items": {"type": "object"}},
        "checkpoint": {"type": ["object", "null"]},
        "trust": {"type": "string"},
        "context": {"type": "array", "items": _CONTEXT_ENTRY},
    },
}

_NODE: dict[str, Any] = {
    "type": "object",
    "required": ["id", "title", "order"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "title": {"type": "string", "minLength": 1},
        "done": {"type": "boolean"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "order": {"type": "integer", "minimum": 0},
        "source": {"type": "string"},
    },
}

_MANIFEST: dict[str, Any] = {
    "type": "object",
    "required": ["task_id", "nodes", "execution_authorized", "provenance"],
    "additionalProperties": True,
    "properties": {
        "task_id": _TASK_ID,
        "objective": {"type": "string"},
        "acceptance": {"type": "string"},
        "scope": {"type": "array", "items": {"type": "string"}},
        "nodes": {"type": "array", "items": _NODE},
        "edges": {"type": "array", "items": {"type": "object"}},
        "next_node": {"type": ["string", "null"]},
        "execution_authorized": {"const": False},
        "provenance": {"type": "string"},
        "manifest_hash": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
    },
}

_SANDBOX: dict[str, Any] = {
    "type": "object",
    "required": ["task_id", "image", "mounts", "resources", "environment"],
    "additionalProperties": True,
    "properties": {
        "task_id": _TASK_ID,
        "image": {"type": "string", "minLength": 1},
        "workdir": {"type": "string"},
        "mounts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source", "target", "mode"],
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "mode": {"type": "string", "enum": ["ro", "rw"]},
                    "purpose": {"type": "string"},
                },
            },
        },
        "read_only_root": {"type": "boolean"},
        "network": {"type": "string"},
        "resources": {
            "type": "object",
            "required": ["memory", "cpus", "pids"],
            "additionalProperties": False,
            "properties": {
                "memory": {"type": "string"},
                "cpus": {"type": "string"},
                "pids": {"type": "integer", "minimum": 1},
            },
        },
        "environment": {"type": "object", "additionalProperties": {"type": "string"}},
        "cap_drop": {"type": "array", "items": {"type": "string"}},
        "security_opt": {"type": "array", "items": {"type": "string"}},
        "command": {"type": "array", "items": {"type": "string"}},
        "created": {"type": "boolean"},
        "provenance": {"type": "string"},
        "spec_hash": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
    },
}

_HANDOFF: dict[str, Any] = {
    "type": "object",
    "required": ["task_id", "sandbox", "manifest", "packet", "authorized"],
    "additionalProperties": False,
    "properties": {
        "task_id": _TASK_ID,
        "sandbox": _SANDBOX,
        "manifest": _MANIFEST,
        "packet": _PACKET,
        "authorized": {"const": False},
        "note": {"type": "string"},
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "ticket": _TICKET,
    "packet": _PACKET,
    "manifest": _MANIFEST,
    "sandbox": _SANDBOX,
    "handoff": _HANDOFF,
}


def schema_for(name: str) -> dict[str, Any]:
    """Return one published schema, complete with its metadata."""
    key = str(name or "").strip().lower()
    if key not in SCHEMAS:
        allowed = ", ".join(sorted(SCHEMAS))
        raise ValidationError(f"unknown schema {name!r}; expected one of: {allowed}")
    body = SCHEMAS[key]
    header = dict(_COMMON)
    header["$id"] = _COMMON["$id"].format(name=key)
    header["title"] = _COMMON["title"].format(title=key)
    header["description"] = _COMMON["description"].format(version=SCHEMA_VERSION)
    return {**header, **body}


def all_schemas() -> dict[str, dict[str, Any]]:
    """Return every published schema, keyed by name."""
    return {name: schema_for(name) for name in sorted(SCHEMAS)}


# ----------------------------------------------------------------------
# a small validator
# ----------------------------------------------------------------------
_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``$ref`` against the root schema."""
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not ref.startswith("#/"):
        raise ValidationError(f"only local references are supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    merged = {k: v for k, v in schema.items() if k != "$ref"}
    return {**node, **merged}


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's boolean/number equivalence."""
    left_type, right_type = _type_of(left), _type_of(right)
    if left_type in {"integer", "number"} and right_type in {"integer", "number"}:
        return bool(left == right)
    if left_type != right_type:
        return False
    if left_type == "array":
        return len(left) == len(right) and all(
            _json_equal(first, second) for first, second in zip(left, right, strict=True)
        )
    if left_type == "object":
        return left.keys() == right.keys() and all(
            _json_equal(value, right[name]) for name, value in left.items()
        )
    if left_type in {"null", "boolean", "string"}:
        return bool(left == right)
    return False


def _nonfinite_errors(instance: Any, path: str = "$") -> list[str]:
    """Check every value, including fields the schema leaves unrestricted."""
    if isinstance(instance, float) and not math.isfinite(instance):
        return [f"{path}: JSON numbers must be finite"]
    errors: list[str] = []
    if isinstance(instance, list):
        for index, item in enumerate(instance):
            errors.extend(_nonfinite_errors(item, f"{path}[{index}]"))
    elif isinstance(instance, dict):
        for name, value in instance.items():
            errors.extend(_nonfinite_errors(value, f"{path}.{name}"))
    return errors


def validate(
    instance: Any, schema: dict[str, Any], root: dict[str, Any] | None = None
) -> list[str]:
    """Validate ``instance`` against ``schema`` and return a list of errors.

    An empty list means the instance satisfies the schema. Non-finite numbers
    are refused even in fields that have no schema constraints.
    """
    errors = _nonfinite_errors(instance)
    if errors:
        return errors
    root = schema if root is None else root
    return _validate(instance, schema, root)


def _validate(instance: Any, schema: dict[str, Any], root: dict[str, Any]) -> list[str]:
    """Apply schema keywords after the complete instance passed the JSON check."""
    schema = _resolve(schema, root)
    errors: list[str] = []
    path = "$"

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(name, lambda _v: False)(instance) for name in types):
            errors.append(f"{path}: expected type {' or '.join(types)}, got {_type_of(instance)}")
            return errors

    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append(f"{path}: expected the constant {schema['const']!r}, got {instance!r}")

    if "enum" in schema and not any(_json_equal(instance, value) for value in schema["enum"]):
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']!r}")

    def _any_of() -> bool:
        return any(not _validate(instance, option, root) for option in schema["anyOf"])

    def _one_of() -> bool:
        matched = sum(1 for option in schema["oneOf"] if not _validate(instance, option, root))
        return matched == 1

    for keyword, ok, message in (
        ("anyOf", _any_of, "matches none of the alternatives"),
        ("oneOf", _one_of, "does not match exactly one alternative"),
    ):
        if keyword in schema and not ok():
            errors.append(f"{path}: {message}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, instance):
            errors.append(f"{path}: does not match {pattern!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above the maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: not below {schema['exclusiveMaximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                for error in _validate(item, item_schema, root):
                    errors.append(f"{path}[{index}]{error[1:]}" if error.startswith("$") else error)

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing the required property {name!r}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                sub = _validate(value, properties[name], root)
                for error in sub:
                    errors.append(f"{path}.{name}{error[1:]}" if error.startswith("$") else error)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: the property {name!r} is not allowed")
            elif isinstance(schema.get("additionalProperties"), dict):
                sub = _validate(value, schema["additionalProperties"], root)
                for error in sub:
                    errors.append(f"{path}.{name}{error[1:]}" if error.startswith("$") else error)

    return errors


def validate_named(instance: Any, name: str) -> list[str]:
    """Validate an instance against one of the published schemas."""
    return validate(instance, schema_for(name))


def to_json(schema: dict[str, Any]) -> str:
    """Render a schema as pretty, stable JSON."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def schema_files() -> list[tuple[str, str]]:
    """Return ``(filename, content)`` pairs for every published schema."""
    return [(f"{name}.json", to_json(schema_for(name))) for name in sorted(SCHEMAS)]
