"""Tests for the file-backed ticket store and the JSON-first CLI."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from el_hidratador.cli import main
from el_hidratador.errors import Conflict, ValidationError
from el_hidratador.tickets import TicketStore, parse_front_matter, render_ticket


class TicketStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TicketStore(Path(self.tmp.name) / ".hidratador")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ticket(self) -> dict:
        return {
            "id": "M-397001",
            "title": "Implement JWT authentication",
            "objective": "objective",
            "acceptance": "acceptance",
            "kind": "code",
            "status": "READY",
            "priority": 1,
            "scope": ["repo/api"],
            "tags": ["security"],
            "depends_on": [],
            "blocks": [],
            "owner": "terminal-1",
            "seed_key": "",
            "token_budget": 8000,
            "description": "body text",
            "acceptance_criteria": ["tokens work"],
            "created_at_iso": "2026-10-21T00:00:00.000Z",
            "updated_at_iso": "2026-10-21T00:00:00.000Z",
        }

    def test_ticket_round_trips_through_a_file(self) -> None:
        path = self.store.write_ticket(self._ticket())
        self.assertTrue(path.exists())
        meta = parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(meta["id"], "M-397001")
        self.assertEqual(meta["status"], "READY")

    def test_index_is_rebuilt_from_files(self) -> None:
        self.store.write_ticket(self._ticket())
        index = json.loads(self.store.index_path.read_text(encoding="utf-8"))
        self.assertIn("M-397001", index)
        self.assertEqual(index["M-397001"]["status"], "READY")

    def test_list_tickets_reads_metadata(self) -> None:
        self.store.write_ticket(self._ticket())
        entries = self.store.list_tickets()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "M-397001")

    def test_evidence_is_written_per_task(self) -> None:
        path = self.store.write_evidence("M-397001", "checkpoint", {"tokens": 100})
        self.assertTrue(path.exists())
        records = self.store.evidence("M-397001")
        self.assertEqual(records[0]["tokens"], 100)

    def test_sparse_evidence_history_is_preserved(self) -> None:
        first = self.store.write_evidence("M-1001", "checkpoint", {"summary": "first"})
        third = first.with_name("checkpoint-3.json")
        third.write_text('{"summary": "third"}\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in (first, third)}

        added = self.store.write_evidence("M-1001", "checkpoint", {"summary": "new"})

        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(added.name, "checkpoint-4.json")
        self.assertEqual(len(self.store.evidence("M-1001")), 3)

    def test_similarly_prefixed_evidence_kinds_have_independent_numbering(self) -> None:
        other = self.store.write_evidence("M-1001", "checkpoint-notes", {"summary": "notes"})
        original = other.read_bytes()
        first = self.store.write_evidence("M-1001", "checkpoint", {"summary": "first"})
        second = self.store.write_evidence("M-1001", "checkpoint", {"summary": "second"})
        self.assertEqual(first.name, "checkpoint.json")
        self.assertEqual(second.name, "checkpoint-2.json")
        self.assertEqual(other.read_bytes(), original)

    def test_concurrent_evidence_writers_preserve_every_record(self) -> None:
        writers = 8
        ready = threading.Barrier(writers, timeout=5)
        original = tempfile.mkstemp

        def simultaneous_temporary_files(*args, **kwargs):
            result = original(*args, **kwargs)
            ready.wait()
            return result

        def write_record(number: int) -> Path:
            return TicketStore(self.store.root).write_evidence(
                "M-1001", "checkpoint", {"writer": number}
            )

        with (
            patch("el_hidratador.tickets.tempfile.mkstemp", simultaneous_temporary_files),
            ThreadPoolExecutor(max_workers=writers) as pool,
        ):
            paths = list(pool.map(write_record, range(writers)))

        self.assertEqual(len(set(paths)), writers)
        self.assertEqual(
            {json.loads(path.read_text(encoding="utf-8"))["writer"] for path in paths},
            set(range(writers)),
        )
        self.assertEqual(len(self.store.evidence("M-1001")), writers)
        self.assertEqual(list(paths[0].parent.glob(".tmp-*")), [])

    def test_evidence_publication_exposes_only_complete_private_json(self) -> None:
        payload = {"summary": "synthetic evidence", "details": ["one", "two"]}
        original = os.link

        def inspect_publication(source, destination):
            source = Path(source)
            destination = Path(destination)
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), payload)
            self.assertEqual(source.stat().st_mode & 0o077, 0)
            self.assertEqual(list(destination.parent.glob("*.json")), [])
            original(source, destination)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), payload)

        with patch("el_hidratador.tickets.os.link", side_effect=inspect_publication) as publish:
            path = self.store.write_evidence("M-1001", "checkpoint", payload)
        publish.assert_called_once()
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(list(path.parent.glob(".tmp-*")), [])

    def test_failed_evidence_publication_preserves_history_and_removes_temporary_file(self) -> None:
        first = self.store.write_evidence("M-1001", "checkpoint", {"summary": "first"})
        before = first.read_bytes()
        with (
            patch("el_hidratador.tickets.os.link", side_effect=OSError("synthetic failure")),
            self.assertRaises(OSError),
        ):
            self.store.write_evidence("M-1001", "checkpoint", {"summary": "new"})
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(list(first.parent.iterdir()), [first])

    def test_evidence_collision_retries_are_bounded(self) -> None:
        with (
            patch("el_hidratador.tickets.os.link", side_effect=FileExistsError) as publish,
            self.assertRaises(Conflict),
        ):
            self.store.write_evidence("M-1001", "checkpoint", {"summary": "new"})
        self.assertGreater(publish.call_count, 1)
        self.assertLessEqual(publish.call_count, 128)
        self.assertEqual(list((self.store.evidence_dir / "M-1001").iterdir()), [])

    def test_evidence_kind_length_is_bounded_before_creating_files(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.write_evidence("M-1001", "x" * 65, {"summary": "new"})
        self.assertFalse(self.store.root.exists())

    def test_evidence_kinds_do_not_escape_the_directory(self) -> None:
        path = self.store.write_evidence("M-397001", "../../escape", {"x": 1})
        self.assertEqual(path.parent.name, "M-397001")

    def test_invalid_ticket_id_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.write_ticket({"id": "../evil"})

    def test_render_ticket_includes_acceptance_criteria(self) -> None:
        text = render_ticket(self._ticket())
        self.assertIn("## Acceptance Criteria", text)
        self.assertIn("- [ ] tokens work", text)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ledger.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> tuple[int, dict]:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--db", self.db, *args])
        text = buffer.getvalue().strip()
        return code, json.loads(text) if text else {}

    def test_init_creates_a_ledger(self) -> None:
        code, payload = self._run("init")
        self.assertEqual(code, 0)
        self.assertTrue(payload["initialized"])

    def test_task_add_and_show_round_trip(self) -> None:
        code, _ = self._run(
            "task-add",
            "M-1001",
            "--objective",
            "do the work",
            "--acceptance",
            "the work is done",
            "--scope",
            "repo/api",
        )
        self.assertEqual(code, 0)
        code, payload = self._run("task-show", "M-1001")
        self.assertEqual(code, 0)
        self.assertEqual(payload["objective"], "do the work")

    def test_full_lifecycle_through_the_cli(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        code, payload = self._run("start", "M-1001", "--owner", "terminal-1")
        self.assertEqual(code, 0)
        session = payload["session_id"]
        code, payload = self._run(
            "checkpoint",
            session,
            "--summary",
            "s",
            "--evidence",
            "e",
            "--next",
            "n",
            "--tokens",
            "10",
        )
        self.assertEqual(code, 0)
        code, payload = self._run("finish", session, "--evidence", "all done")
        self.assertEqual(payload["status"], "REVIEW")
        code, payload = self._run("decide", "M-1001", "accept", "--reason", "reviewed")
        self.assertEqual(payload["status"], "DONE")

    def test_refused_operation_returns_exit_code_two(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        code, payload = self._run("decide", "M-1001", "accept", "--reason", "premature")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])

    def test_pack_and_expand_emit_json(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        code, payload = self._run("pack", "M-1001")
        self.assertEqual(code, 0)
        self.assertIn("packet", payload)
        code, payload = self._run("expand", "M-1001")
        self.assertEqual(code, 0)
        self.assertIn("## Objective", payload["prompt"])

    def test_dag_command_builds_a_manifest(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        code, payload = self._run("dag", "M-1001")
        self.assertEqual(code, 0)
        self.assertFalse(payload["execution_authorized"])

    def test_export_writes_ticket_files(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        target = str(Path(self.tmp.name) / "exported")
        code, payload = self._run("export", "--dir", target)
        self.assertEqual(code, 0)
        self.assertTrue(Path(target, "tickets", "M-1001.md").exists())

    def test_status_and_events_are_json(self) -> None:
        code, payload = self._run("status")
        self.assertEqual(code, 0)
        self.assertIn("metrics", payload)
        code, payload = self._run("events")
        self.assertEqual(code, 0)
        self.assertIsInstance(payload["events"], list)

    def test_route_never_authorizes_execution(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        self._run(
            "profile-add",
            "local",
            "--kind",
            "code",
            "--context-limit",
            "16000",
            "--cost",
            "zero_incremental",
        )
        code, payload = self._run("route", "M-1001")
        self.assertEqual(code, 0)
        self.assertFalse(payload["execution_authorized"])

    def test_unknown_command_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--db", self.db, "not-a-command"])

    def test_doctor_reports_the_environment(self) -> None:
        code, payload = self._run("doctor")
        self.assertEqual(code, 0)
        self.assertIn("checks", payload)
        self.assertTrue(payload["healthy"])

    def test_demo_runs_without_touching_the_ledger(self) -> None:
        code, payload = self._run("demo")
        self.assertEqual(code, 0)
        self.assertTrue(payload["steps"])
        self.assertTrue(payload["temporary"])

    def test_schema_lists_every_schema(self) -> None:
        code, payload = self._run("schema")
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 5)

    def test_schema_writes_the_published_files(self) -> None:
        target = str(Path(self.tmp.name) / "schemas")
        code, payload = self._run("schema", "--out", target)
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 5)
        self.assertTrue(Path(target, "handoff.json").exists())

    def test_schema_invalid_document_returns_failure_without_opening_ledger(self) -> None:
        document = Path(self.tmp.name) / "invalid-handoff.json"
        document.write_text("{}", encoding="utf-8")
        code, payload = self._run("schema", "--validate", str(document), "--against", "handoff")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["valid"])
        self.assertTrue(payload["errors"])
        self.assertFalse(Path(self.db).exists())

    def test_schema_valid_document_succeeds_without_opening_ledger(self) -> None:
        document = Path(self.tmp.name) / "valid-ticket.json"
        document.write_text(
            json.dumps(
                {
                    "id": "M-1001",
                    "objective": "o",
                    "acceptance": "a",
                    "status": "INBOX",
                }
            ),
            encoding="utf-8",
        )
        code, payload = self._run("schema", "--validate", str(document), "--against", "ticket")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["errors"], [])
        self.assertFalse(Path(self.db).exists())

    def test_schema_malformed_json_returns_typed_error_without_opening_ledger(self) -> None:
        document = Path(self.tmp.name) / "malformed.json"
        for content in (b'{"authorized":', b'"\xff"'):
            with self.subTest(content=content):
                document.write_bytes(content)
                code, payload = self._run("schema", "--validate", str(document))
                self.assertEqual(code, 2)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["kind"], "ValidationError")
                self.assertFalse(Path(self.db).exists())

    def test_replay_reports_a_missing_task(self) -> None:
        code, payload = self._run("replay", "M-does-not-exist")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])

    def test_replay_reconstructs_a_lifecycle(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        self._run("triage", "M-1001")
        code, payload = self._run("replay", "M-1001")
        self.assertEqual(code, 0)
        self.assertEqual(payload["reconstructed_station"], "READY")
        self.assertTrue(payload["consistent"])

    def test_failures_checkpoints_and_handoffs_are_listed(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        code, payload = self._run("failures")
        self.assertEqual(code, 0)
        self.assertIn("failures", payload)
        code, payload = self._run("handoffs", "M-1001")
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 0)

    def test_context_list_reports_the_attached_blobs(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        self._run("context-add", "M-1001", "--text", "some context", "--source-label", "test")
        code, payload = self._run("context-list", "M-1001")
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)

    def test_task_update_changes_a_field(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        code, payload = self._run(
            "task-update", "M-1001", "--field", "token_budget", "--value", "9000"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["token_budget"], 9000)

    def test_task_update_refuses_an_unknown_field(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        code, payload = self._run("task-update", "M-1001", "--field", "status", "--value", "DONE")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])

    def test_task_update_noninteger_returns_typed_error_without_mutation(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        _, before = self._run("task-show", "M-1001")
        _, events_before = self._run("events")
        for field, value in (("token_budget", "inf"), ("priority", "1.5")):
            with self.subTest(field=field):
                code, payload = self._run(
                    "task-update", "M-1001", "--field", field, "--value", value
                )
                self.assertEqual(code, 2)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["kind"], "ValidationError")
        _, after = self._run("task-show", "M-1001")
        _, events_after = self._run("events")
        self.assertEqual(after, before)
        self.assertEqual(events_after, events_before)

    def test_task_update_refuses_a_list_for_a_scalar_field(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        _, before = self._run("task-show", "M-1001")
        _, events_before = self._run("events")
        code, payload = self._run(
            "task-update", "M-1001", "--field", "token_budget", "--list", "9000"
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["kind"], "ValidationError")
        _, after = self._run("task-show", "M-1001")
        _, events_after = self._run("events")
        self.assertEqual(after, before)
        self.assertEqual(events_after, events_before)

    def test_task_update_accepts_repeatable_list_without_a_placeholder_value(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        code, payload = self._run(
            "task-update", "M-1001", "--field", "tags", "--list", "security", "--list", "auth"
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["tags"], ["security", "auth"])

    def test_task_update_requires_an_explicit_value(self) -> None:
        self._run("task-add", "M-1001", "--objective", "o", "--acceptance", "a")
        _, before = self._run("task-show", "M-1001")
        _, events_before = self._run("events")
        code, payload = self._run("task-update", "M-1001", "--field", "title")
        self.assertEqual(code, 2)
        self.assertEqual(payload["kind"], "ValidationError")
        _, after = self._run("task-show", "M-1001")
        _, events_after = self._run("events")
        self.assertEqual(after, before)
        self.assertEqual(events_after, events_before)

    def test_handoff_schema_prints_the_published_contract(self) -> None:
        code, payload = self._run("handoff", "M-1001", "--schema")
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], "handoff")
        self.assertIn("$id", payload["definition"])

    def test_sandbox_apply_prints_the_command_without_creating_it(self) -> None:
        code, payload = self._run(
            "sandbox-apply", "--id", "M-1001", "--workdir", str(Path(self.tmp.name))
        )
        self.assertEqual(code, 0)
        self.assertFalse(payload["applied"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["argv"][0], "docker")
        self.assertEqual(payload["boundary"]["network"], "none")

    def test_sandbox_teardown_needs_a_name(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--db", self.db, "sandbox-teardown"])

    def test_read_only_commands_do_not_create_a_ledger(self) -> None:
        target = str(Path(self.tmp.name) / "untouched.sqlite")
        code, _ = self._run("--db", target, "topology")
        self.assertEqual(code, 0)
        self.assertFalse(Path(target).exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
