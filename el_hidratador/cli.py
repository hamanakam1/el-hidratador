"""JSON-first command line interface.

Every command prints one JSON object to stdout. Nothing is written to stdout
that is not machine readable, so the CLI composes with pipes, scripts, and the
local dashboard without parsing prose.

Exit codes:

* ``0`` — the command succeeded
* ``2`` — a refused operation (validation, conflict, budget, lease)
* ``3`` — an adapter failure (GitHub CLI, dashboard, Docker)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .context_expander import expand_from_ledger
from .core import Ledger, create_ledger, default_db_path
from .dag import build_dag, handoff_payload, manifest, summarize
from .dashboard import preview
from .demo import demo_text, run_demo
from .doctor import run_checks
from .errors import AdapterError, HidratadorError, ValidationError
from .github_sync import GitHubSync
from .hardware_router import (
    CLASS_HEAVY,
    CLASS_LIGHT,
    Target,
    default_targets,
    dispatch_plan,
    evaluate,
    local_topology,
)
from .replay import replay, replay_all
from .sandbox import DEFAULT_IMAGE, apply_sandbox, boundary_report, build_sandbox, teardown_sandbox
from .schema import all_schemas, schema_files, schema_for, validate_named
from .tickets import TicketStore

FOLLOW_INTERVAL = 0.5


def _emit(payload: dict[str, Any], pretty: bool = True) -> None:
    text = json.dumps(payload, indent=2 if pretty else None, sort_keys=True, default=str)
    print(text)


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="hidratador",
        description="El Hidratador — context expansion and execution staging engine.",
    )
    parser.add_argument("--version", action="version", version=f"hidratador {__version__}")
    parser.add_argument(
        "--db",
        default=None,
        help="ledger path (default: $HIDRATADOR_DB or ~/.el-hidratador/ledger.sqlite)",
    )
    parser.add_argument("--compact", action="store_true", help="print JSON on a single line")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the ledger database")
    sub.add_parser("status", help="snapshot the whole ledger")
    sub.add_parser("reconcile", help="expire stale leases and report")
    sub.add_parser("events", help="print audit events")
    sub.add_parser("doctor", help="check whether this machine is ready")
    sub.add_parser("topology", help="report the local machine and the adapters")
    sub.add_parser("demo", help="run a narrated demonstration on a scratch ledger")

    demo_cmd = sub.choices["demo"]
    demo_cmd.add_argument("--text", action="store_true", help="print the narrative as plain text")
    demo_cmd.add_argument("--keep", action="store_true", help="keep the scratch ledger")
    demo_cmd.add_argument(
        "--db", dest="demo_db", default=None, help="use this ledger instead of a scratch one"
    )

    schema_cmd = sub.add_parser("schema", help="print or write the published schemas")
    schema_cmd.add_argument("--name", default=None, help="print one schema by name")
    schema_cmd.add_argument("--out", default=None, help="write every schema into this directory")
    schema_cmd.add_argument(
        "--validate",
        dest="validate_file",
        default=None,
        help="validate a JSON document against a named schema",
    )
    schema_cmd.add_argument("--against", default="handoff", help="the schema used by --validate")

    replay_cmd = sub.add_parser("replay", help="rebuild a task from its event log")
    replay_cmd.add_argument("id", nargs="?", default=None)
    replay_cmd.add_argument("--all", action="store_true", help="replay every task")

    failures_cmd = sub.add_parser("failures", help="list the recorded failures")
    failures_cmd.add_argument("--task", default=None)

    checkpoints_cmd = sub.add_parser("checkpoints", help="list the checkpoints of a session")
    checkpoints_cmd.add_argument("session")

    handoffs_cmd = sub.add_parser("handoffs", help="list the recorded handoffs of a task")
    handoffs_cmd.add_argument("id")

    context_list = sub.add_parser("context-list", help="list the context attached to a task")
    context_list.add_argument("id")

    sub.add_parser("profile-list", help="list the declared compute profiles")

    sandbox_apply = sub.add_parser("sandbox-apply", help="print or create the staging sandbox")
    sandbox_apply.add_argument("--id", required=True, help="the task the sandbox stages")
    sandbox_apply.add_argument(
        "--workdir", required=True, help="the absolute path mounted as the workspace"
    )
    sandbox_apply.add_argument("--image", default=DEFAULT_IMAGE, help="the container image")
    sandbox_apply.add_argument("--repo-mount", default=None, help="a read-only reference checkout")
    sandbox_apply.add_argument(
        "--evidence-dir", default=None, help="a read-only evidence directory"
    )
    sandbox_apply.add_argument("--name", default=None, help="the container name")
    sandbox_apply.add_argument(
        "--apply", action="store_true", help="create the container instead of printing the command"
    )

    sandbox_teardown = sub.add_parser("sandbox-teardown", help="remove a staging sandbox")
    sandbox_teardown.add_argument("--name", required=True, help="the container name")

    task_update = sub.add_parser("task-update", help="change a ticket field")
    task_update.add_argument("id")
    task_update.add_argument("--field", required=True, help="the field to change")
    task_update.add_argument("--value", required=True, help="the new value")
    task_update.add_argument(
        "--list", dest="value_list", action="append", default=None, help="a list value, repeatable"
    )

    events = sub.choices["events"]
    events.add_argument("--after", type=int, default=0)
    events.add_argument(
        "--follow",
        action="store_true",
        help="print new events as they are written, one JSON object per line",
    )

    task_add = sub.add_parser("task-add", help="create a task")
    task_add.add_argument("id")
    task_add.add_argument("--objective", required=True)
    task_add.add_argument("--acceptance", required=True)
    task_add.add_argument("--title", default="")
    task_add.add_argument("--kind", default="code")
    task_add.add_argument("--scope", action="append", default=[])
    task_add.add_argument("--budget", type=int, default=8000)
    task_add.add_argument("--tags", default="")
    task_add.add_argument("--depends-on", default="")
    task_add.add_argument("--blocks", default="")
    task_add.add_argument("--owner", default="")
    task_add.add_argument("--seed-key", default="")
    task_add.add_argument("--priority", type=int, default=2)
    task_add.add_argument("--description", default="")

    task_show = sub.add_parser("task-show", help="show one task")
    task_show.add_argument("id")

    task_list = sub.add_parser("task-list", help="list tasks")
    task_list.add_argument("--status", default=None)

    triage = sub.add_parser("triage", help="move a task from INBOX to READY")
    triage.add_argument("id")

    start = sub.add_parser("start", help="acquire an execution lease")
    start.add_argument("id")
    start.add_argument("--owner", required=True)
    start.add_argument("--lease", type=int, default=900)

    checkpoint = sub.add_parser("checkpoint", help="record progress with evidence")
    checkpoint.add_argument("session")
    checkpoint.add_argument("--summary", required=True)
    checkpoint.add_argument("--evidence", required=True)
    checkpoint.add_argument("--next", required=True)
    checkpoint.add_argument("--tokens", type=int, required=True)
    checkpoint.add_argument("--lease", type=int, default=900)

    fail = sub.add_parser("fail", help="record an explicit failure")
    fail.add_argument("session")
    fail.add_argument("--category", required=True)
    fail.add_argument("--summary", required=True)

    finish = sub.add_parser("finish", help="request review")
    finish.add_argument("session")
    finish.add_argument("--evidence", required=True)

    decide = sub.add_parser("decide", help="record an operator decision")
    decide.add_argument("id")
    decide.add_argument("decision", choices=["accept", "retry", "cancel"])
    decide.add_argument("--reason", required=True)

    context_add = sub.add_parser("context-add", help="attach context to a task")
    context_add.add_argument("id")
    source = context_add.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--text")
    context_add.add_argument("--source-label", default="cli")
    context_add.add_argument("--priority", type=int, default=0)
    context_add.add_argument("--ttl", type=int, default=None)

    pack = sub.add_parser("pack", help="build a byte-budgeted execution packet")
    pack.add_argument("id")
    pack.add_argument("--max-bytes", type=int, default=12000)
    pack.add_argument("--packet-only", action="store_true")

    expand = sub.add_parser("expand", help="expand a ticket into a hydrated prompt")
    expand.add_argument("id")
    expand.add_argument("--max-bytes", type=int, default=12000)
    expand.add_argument("--prompt", action="store_true", help="print the prompt only")

    dag_cmd = sub.add_parser("dag", help="build the execution DAG")
    dag_cmd.add_argument("id")

    handoff = sub.add_parser("handoff", help="prepare the Taskrivet handoff payload")
    handoff.add_argument("id")
    handoff.add_argument("--max-bytes", type=int, default=12000)
    handoff.add_argument("--workdir", default=None)
    handoff.add_argument(
        "--schema", action="store_true", help="print the published handoff schema instead"
    )

    profile_add = sub.add_parser("profile-add", help="declare a compute profile")
    profile_add.add_argument("id")
    profile_add.add_argument("--kind", action="append", default=[])
    profile_add.add_argument("--context-limit", type=int, required=True)
    profile_add.add_argument("--cost", default="zero_incremental")
    profile_add.add_argument("--ttl", type=int, default=3600)

    route = sub.add_parser("route", help="rank compute targets for a task")
    route.add_argument("id")
    route.add_argument("--required-context", type=int, default=4096)

    export = sub.add_parser("export", help="export tickets to a file-backed store")
    export.add_argument("--dir", default=".hidratador")

    gh_poll = sub.add_parser("github-poll", help="observe a repository (read only)")
    gh_poll.add_argument("--repo", required=True)
    gh_poll.add_argument("--apply", action="store_true", help="persist the observations")

    gh_publish = sub.add_parser("github-publish", help="queue and publish an idempotent comment")
    gh_publish.add_argument("--repo", required=True)
    gh_publish.add_argument("--issue", type=int, required=True)
    gh_publish.add_argument("--key", required=True)
    gh_publish.add_argument("--body", required=True)
    gh_publish.add_argument("--apply", action="store_true", help="publish instead of queueing")

    dash = sub.add_parser("dashboard-preview", help="preview a local dashboard")
    dash.add_argument("--url", required=True)
    dash.add_argument("--status", default="planned")
    dash.add_argument("--offset", type=int, default=0)
    dash.add_argument("--limit", type=int, default=50)

    return parser


def _db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db).expanduser()
    return default_db_path()


#: Commands that must not create or open a ledger. A read-only command that
#: creates state on the way is a read-only command with a side effect.
#: `doctor` and `demo` are handled before the ledger is opened; the rest live
#: in `_NO_LEDGER_HANDLERS`.


def run(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI invocation. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    pretty = not args.compact
    command = args.command

    try:
        if command == "doctor":
            payload = run_checks(_db_path(args))
            payload.setdefault("ok", True)
            payload["command"] = command
            _emit(payload, pretty)
            return 0 if payload["healthy"] else 2

        if command == "demo":
            result = run_demo(args.demo_db, keep=args.keep)
            if args.text:
                print(demo_text(result), end="")
            else:
                _emit(result, pretty)
            return 0

        if command == "init":
            path = _db_path(args)
            ledger = create_ledger(path)
            payload = {
                "initialized": True,
                "database": str(path),
                "schema_version": ledger.schema_version,
            }
            ledger.close()
            _emit(payload, pretty)
            return 0

        if command in _NO_LEDGER_HANDLERS:
            payload = _NO_LEDGER_HANDLERS[command](args, None)
            payload.setdefault("ok", True)
            payload["command"] = command
            _emit(payload, pretty)
            return 0

        ledger = create_ledger(_db_path(args))
        try:
            if command == "events" and args.follow:
                try:
                    follow_events(ledger, after=args.after)
                except KeyboardInterrupt:
                    return 130
                return 0
            handler: Callable[[argparse.Namespace, Any], dict[str, Any]] = _HANDLERS[command]
            payload = handler(args, ledger)
            payload.setdefault("ok", True)
            payload["command"] = command
            _emit(payload, pretty)
            return 0
        finally:
            if ledger is not None:
                ledger.close()
    except AdapterError as exc:
        _emit({"ok": False, "error": str(exc), "kind": "adapter"}, pretty)
        return 3
    except HidratadorError as exc:
        _emit({"ok": False, "error": str(exc), "kind": type(exc).__name__}, pretty)
        return 2
    except FileNotFoundError as exc:
        _emit({"ok": False, "error": str(exc), "kind": "file"}, pretty)
        return 2


def _cmd_status(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.status()


def _cmd_reconcile(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.reconcile()


def _cmd_events(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    events = ledger.events(after=args.after)
    return {"events": events, "count": len(events)}


def _cmd_topology(args: argparse.Namespace, ledger: Ledger | None) -> dict[str, Any]:
    return local_topology()


def _cmd_failures(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    if args.task:
        return {"failures": ledger.failures(args.task), "task_id": args.task}
    return {"failures": ledger.failure_summary()}


def _cmd_checkpoints(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    checkpoints = ledger.checkpoints(args.session)
    return {"session_id": args.session, "checkpoints": checkpoints, "count": len(checkpoints)}


def _cmd_handoffs(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    handoffs = ledger.handoffs(args.id)
    return {"task_id": args.id, "handoffs": handoffs, "count": len(handoffs)}


def _cmd_context_list(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    entries = ledger.context_entries(args.id)
    return {
        "task_id": args.id,
        "count": len(entries),
        "entries": [
            {
                "hash": entry["hash"],
                "source": entry["source"],
                "priority": entry["priority"],
                "bytes": len(entry["body"].encode()),
                "expires": entry["expires"],
            }
            for entry in entries
        ],
    }


def _cmd_sandbox_apply(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    spec = build_sandbox(
        args.id,
        args.workdir,
        image=args.image,
        repo_mount=args.repo_mount,
        evidence_dir=args.evidence_dir,
    )
    result = apply_sandbox(spec, dry_run=not args.apply)
    argv = result.pop("command", [])
    result["argv"] = argv
    if args.name:
        result["container_name"] = args.name
    result["boundary"] = boundary_report(spec)
    result["note"] = "A sandbox specification is prepared state, not permission to execute."
    return result


def _cmd_sandbox_teardown(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return teardown_sandbox(args.name)


def _cmd_profile_list(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    profiles = [profile.to_dict() for profile in ledger.profiles()]
    return {"profiles": profiles, "count": len(profiles)}


def _cmd_task_update(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    value: object
    if args.value_list is not None:
        value = list(args.value_list)
    elif args.field in ("scope", "tags", "depends_on", "blocks", "acceptance_criteria"):
        value = [part.strip() for part in args.value.split(",") if part.strip()]
    elif args.field == "token_budget" or args.field == "priority":
        value = int(args.value)
    else:
        value = args.value
    return ledger.update_task(args.id, **{args.field: value})


def _cmd_replay(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    if args.all:
        return replay_all(ledger)
    if not args.id:
        raise ValidationError("replay needs a task id, or --all")
    return replay(ledger, args.id)


def _cmd_schema(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    if args.out:
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        written = []
        for filename, content in schema_files():
            path = directory / filename
            path.write_text(content, encoding="utf-8")
            written.append(str(path))
        return {"written": written, "count": len(written)}
    if args.validate_file:
        document = json.loads(Path(args.validate_file).read_text(encoding="utf-8"))
        errors = validate_named(document, args.against)
        return {
            "file": args.validate_file,
            "schema": args.against,
            "valid": not errors,
            "errors": errors,
        }
    if args.name:
        return {"schema": args.name, "definition": schema_for(args.name)}
    return {"schemas": all_schemas(), "count": len(all_schemas())}


def follow_events(
    ledger: Ledger,
    after: int = 0,
    *,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    source: Callable[[int], list[dict[str, Any]]] | None = None,
) -> None:
    """Print new audit events as they arrive, one JSON object per line.

    Following starts after the newest event already in the ledger, so a
    freshly attached follower never repeats history. ``source`` exists so
    tests can drive the loop without waiting on a real writer.
    """
    last = max(after, ledger.latest_event_seq())

    def read(after: int) -> list[dict[str, Any]]:
        if source is not None:
            return source(after)
        return ledger.events(after=after)

    while True:
        for event in read(last):
            emit(json.dumps(event, sort_keys=True, default=str))
            last = int(event["seq"])
        sleep(FOLLOW_INTERVAL)


def _cmd_task_add(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.add_task(
        task_id=args.id,
        objective=args.objective,
        acceptance=args.acceptance,
        title=args.title,
        kind=args.kind,
        scope=args.scope or None,
        token_budget=args.budget,
        tags=_split(args.tags),
        depends_on=_split(args.depends_on),
        blocks=_split(args.blocks),
        owner=args.owner,
        seed_key=args.seed_key,
        description=args.description,
        priority=args.priority,
    )


def _cmd_task_show(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.task(args.id)


def _cmd_task_list(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    tasks = ledger.tasks(args.status)
    return {"tasks": tasks, "count": len(tasks)}


def _cmd_triage(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.triage(args.id)


def _cmd_start(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.start(args.id, args.owner, args.lease)


def _cmd_checkpoint(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.checkpoint(
        args.session,
        args.summary,
        args.evidence,
        args.next,
        args.tokens,
        args.lease,
    )


def _cmd_fail(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.fail(args.session, args.category, args.summary)


def _cmd_finish(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.finish(args.session, args.evidence)


def _cmd_decide(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.decide(args.id, args.decision, args.reason)


def _cmd_context_add(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
        source = args.source_label if args.source_label != "cli" else args.file
    else:
        body = args.text or ""
        source = args.source_label
    return ledger.add_context(args.id, body, source, args.priority, args.ttl)


def _cmd_pack(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    result = ledger.pack(args.id, args.max_bytes)
    if args.packet_only:
        return {"packet": result["packet"], "packet_bytes": result["packet_bytes"]}
    return result


def _cmd_expand(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    result = expand_from_ledger(ledger, args.id, args.max_bytes)
    if args.prompt:
        return {"task_id": args.id, "prompt": result["prompt"]}
    return {
        "task_id": args.id,
        "prompt": result["prompt"],
        "prompt_bytes": result["prompt_bytes"],
        "packet_bytes": result["packet_bytes"],
        "omitted": result["omitted"],
        "token_estimate": result["token_estimate"],
        "note": result["note"],
    }


def _cmd_dag(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    ticket = ledger.task(args.id)
    dag = build_dag(
        args.id,
        markdown=ticket.get("description", ""),
        ticket_dependencies=ticket.get("depends_on"),
    )
    payload = manifest(dag, ticket)
    payload["summary"] = summarize(dag)
    return payload


def _cmd_handoff(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    if args.schema:
        return {"task_id": args.id, "schema": "handoff", "definition": schema_for("handoff")}
    ticket = ledger.task(args.id)
    dag = build_dag(
        args.id,
        markdown=ticket.get("description", ""),
        ticket_dependencies=ticket.get("depends_on"),
    )
    expansion = expand_from_ledger(ledger, args.id, args.max_bytes)
    workdir = args.workdir or f"~/.hidratador/work/{args.id}"
    sandbox = build_sandbox(args.id, workdir)
    payload = handoff_payload(dag, expansion["packet"], ticket, sandbox)
    ledger.record_handoff(args.id, payload["manifest"], expansion["packet"])
    return {
        "handoff": payload,
        "prompt": expansion["prompt"],
        "omitted_context": expansion["omitted"],
        "note": "A handoff is prepared state, not permission to execute.",
    }


def _cmd_profile_add(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    return ledger.profile(args.id, args.kind, args.context_limit, args.cost, args.ttl)


def _cmd_route(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    ticket = ledger.task(args.id)
    profiles = ledger.profiles()
    if profiles:
        # Declared profiles are the operator's own. They win over the built-in
        # targets, because an operator who declared a profile meant it.
        targets = [
            Target(
                id=profile.id,
                class_name=CLASS_HEAVY if profile.context_limit >= 32000 else CLASS_LIGHT,
                context_limit=profile.context_limit,
                cost=profile.cost,
                kinds=list(profile.kinds),
                verified_until=profile.verified_until,
                physical=False,
            )
            for profile in profiles
        ]
        source = "declared profiles"
    else:
        targets = default_targets()
        source = "built-in targets; declare one with `hidratador profile-add` to route on it"
    result = evaluate(ticket, targets, args.required_context)
    payload = result.to_dict()
    payload["targets"] = source
    payload["plan"] = dispatch_plan(result, args.id)
    return payload


def _cmd_export(args: argparse.Namespace, ledger: Ledger) -> dict[str, Any]:
    store = TicketStore(args.dir)
    return store.export_all(ledger.tasks())


def _cmd_github_poll(args: argparse.Namespace, ledger: Ledger | None) -> dict[str, Any]:
    sync = GitHubSync()
    try:
        return sync.poll(args.repo, dry_run=not args.apply)
    finally:
        sync.close()


def _cmd_github_publish(args: argparse.Namespace, ledger: Ledger | None) -> dict[str, Any]:
    sync = GitHubSync()
    try:
        queued = sync.enqueue(args.repo, args.issue, args.key, args.body)
        published = sync.publish(args.repo, dry_run=not args.apply)
        return {"queued": queued, "publish": published}
    finally:
        sync.close()


def _cmd_dashboard_preview(args: argparse.Namespace, ledger: Ledger | None) -> dict[str, Any]:
    return preview(args.url, args.status, args.offset, args.limit)


# The handlers keep their own precise signatures; the table is typed loosely
# enough to hold both the ledger-backed and the ledger-free handlers.
_HANDLERS: dict[str, Callable[[argparse.Namespace, Any], dict[str, Any]]] = {
    "status": _cmd_status,
    "failures": _cmd_failures,
    "checkpoints": _cmd_checkpoints,
    "handoffs": _cmd_handoffs,
    "context-list": _cmd_context_list,
    "profile-list": _cmd_profile_list,
    "sandbox-apply": _cmd_sandbox_apply,
    "sandbox-teardown": _cmd_sandbox_teardown,
    "task-update": _cmd_task_update,
    "replay": _cmd_replay,
    "reconcile": _cmd_reconcile,
    "events": _cmd_events,
    "task-add": _cmd_task_add,
    "task-show": _cmd_task_show,
    "task-list": _cmd_task_list,
    "triage": _cmd_triage,
    "start": _cmd_start,
    "checkpoint": _cmd_checkpoint,
    "fail": _cmd_fail,
    "finish": _cmd_finish,
    "decide": _cmd_decide,
    "context-add": _cmd_context_add,
    "pack": _cmd_pack,
    "expand": _cmd_expand,
    "dag": _cmd_dag,
    "handoff": _cmd_handoff,
    "profile-add": _cmd_profile_add,
    "route": _cmd_route,
    "export": _cmd_export,
}

#: Handlers that run without opening a ledger.
_NO_LEDGER_HANDLERS: dict[str, Callable[[argparse.Namespace, Any], dict[str, Any]]] = {
    "schema": _cmd_schema,
    "topology": _cmd_topology,
    "github-poll": _cmd_github_poll,
    "github-publish": _cmd_github_publish,
    "dashboard-preview": _cmd_dashboard_preview,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point used by the ``hidratador`` console script."""
    try:
        return run(argv)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        _emit({"ok": False, "error": "interrupted", "kind": "interrupt"}, True)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
