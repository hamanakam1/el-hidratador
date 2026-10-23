"""Tests for replay, the doctor checks, and the demonstration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from el_hidratador.core import create_ledger
from el_hidratador.demo import demo_text, run_demo
from el_hidratador.doctor import run_checks
from el_hidratador.errors import NotFound
from el_hidratador.replay import reconstruct, replay, replay_all


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = create_ledger(Path(self.tmp.name) / "ledger.sqlite")

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def _lifecycle(self) -> str:
        self.ledger.add_task("M-1001", "objective", "acceptance", scope=["repo/api"])
        self.ledger.triage("M-1001")
        session = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(session["session_id"], "s", "e", "n", 10)
        self.ledger.finish(session["session_id"], "evidence")
        self.ledger.decide("M-1001", "accept", "reviewed")
        return session["session_id"]

    def test_replay_reconstructs_the_stations(self) -> None:
        self._lifecycle()
        result = replay(self.ledger, "M-1001")
        self.assertEqual(result["reconstructed_station"], "DONE")
        self.assertEqual(result["current_station"], "DONE")
        self.assertTrue(result["consistent"])

    def test_replay_lists_every_transition(self) -> None:
        self._lifecycle()
        result = replay(self.ledger, "M-1001")
        stations = [step["station"] for step in result["transitions"]]
        self.assertEqual(
            stations,
            ["INBOX", "READY", "IN_PROGRESS", "REVIEW", "ACCEPTED", "DONE"],
        )

    def test_replay_includes_the_checkpoints(self) -> None:
        session = self._lifecycle()
        result = replay(self.ledger, "M-1001")
        self.assertEqual(len(result["checkpoints"]), 1)
        self.assertEqual(result["checkpoints"][0]["session_id"], session)

    def test_replay_reports_a_divergence(self) -> None:
        self._lifecycle()
        self.ledger.db.execute("UPDATE tickets SET status='REVIEW' WHERE id='M-1001'")
        result = replay(self.ledger, "M-1001")
        self.assertFalse(result["consistent"])
        self.assertEqual(result["divergences"][0]["kind"], "station_mismatch")

    def test_replay_reports_a_task_with_no_events(self) -> None:
        self.ledger.db.execute(
            "INSERT INTO tickets(id,title,objective,acceptance,kind,scope,status,priority,"
            "token_budget,tags,depends_on,blocks,owner,seed_key,description,acceptance_criteria,"
            "created_at,updated_at) VALUES ('M-9','t','o','a','code','[]','INBOX',2,100,'[]',"
            "'[]','[]','','','', '[]',0,0)"
        )
        result = replay(self.ledger, "M-9")
        self.assertFalse(result["consistent"])
        self.assertEqual(result["divergences"][0]["kind"], "no_events")

    def test_replay_does_not_write(self) -> None:
        self._lifecycle()
        before = self.ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        replay(self.ledger, "M-1001")
        after = self.ledger.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(before, after)

    def _expired_session(self) -> str:
        with patch("time.time", return_value=100):
            self.ledger.add_task("M-1002", "objective", "acceptance", scope=["repo/expired"])
            return self.ledger.start("M-1002", "terminal-2", lease_seconds=1)["session_id"]

    def test_replay_does_not_reconcile_an_expired_task(self) -> None:
        session = self._expired_session()
        before = list(self.ledger.db.iterdump())
        changes = self.ledger.db.total_changes
        with patch("time.time", return_value=102):
            result = replay(self.ledger, "M-1002")
        self.assertEqual(result["current_station"], "IN_PROGRESS")
        self.assertTrue(result["consistent"])
        self.assertEqual(self.ledger.session(session)["status"], "active")
        self.assertEqual(list(self.ledger.db.iterdump()), before)
        self.assertEqual(self.ledger.db.total_changes, changes)
        self.assertFalse(self.ledger.db.in_transaction)

    def test_replay_does_not_reconcile_an_unrelated_expired_task(self) -> None:
        self._lifecycle()
        self._expired_session()
        before = list(self.ledger.db.iterdump())
        changes = self.ledger.db.total_changes
        with patch("time.time", return_value=102):
            result = replay(self.ledger, "M-1001")
        self.assertTrue(result["consistent"])
        self.assertEqual(result["current_station"], "DONE")
        self.assertEqual(list(self.ledger.db.iterdump()), before)
        self.assertEqual(self.ledger.db.total_changes, changes)

    def test_replay_all_does_not_reconcile_expired_tasks(self) -> None:
        self._lifecycle()
        self._expired_session()
        before = list(self.ledger.db.iterdump())
        changes = self.ledger.db.total_changes
        with patch("time.time", return_value=102):
            result = replay_all(self.ledger)
        self.assertEqual(result["inconsistent"], [])
        self.assertEqual(list(self.ledger.db.iterdump()), before)
        self.assertEqual(self.ledger.db.total_changes, changes)

    def test_replay_finds_tasks_created_after_the_first_500_events(self) -> None:
        self.ledger.add_task("M-1001", "objective", "acceptance")
        for index in range(501):
            self.ledger.update_task("M-1001", title=f"revision {index}")
        self.ledger.add_task("M-1002", "objective", "acceptance")
        self.ledger.triage("M-1002")
        result = replay(self.ledger, "M-1002")
        self.assertEqual(result["event_count"], 2)
        self.assertEqual(result["reconstructed_station"], "READY")
        self.assertTrue(result["consistent"])
        self.assertGreater(result["steps"][0]["seq"], 500)

    def test_replay_includes_a_tasks_complete_history_beyond_500_events(self) -> None:
        self.ledger.add_task("M-1001", "objective", "acceptance")
        for index in range(501):
            self.ledger.update_task("M-1001", title=f"revision {index}")
        session = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(session["session_id"], "summary", "evidence", "next", 10)
        self.ledger.finish(session["session_id"], "evidence")
        self.ledger.decide("M-1001", "accept", "reviewed")
        result = replay(self.ledger, "M-1001")
        sequences = [
            row["seq"]
            for row in self.ledger.db.execute(
                "SELECT seq FROM events WHERE task_id=? ORDER BY seq", ("M-1001",)
            )
        ]
        self.assertEqual([step["seq"] for step in result["steps"]], sequences)
        self.assertEqual(result["event_count"], len(sequences))
        self.assertEqual(result["reconstructed_station"], "DONE")
        self.assertTrue(result["consistent"])

    def test_replay_preserves_an_existing_caller_transaction(self) -> None:
        self._lifecycle()
        original_title = self.ledger.task("M-1001")["title"]
        for all_tasks in (False, True):
            with self.subTest(all_tasks=all_tasks):
                self.ledger.db.execute("BEGIN")
                try:
                    self.ledger.db.execute(
                        "UPDATE tickets SET title=? WHERE id=?", ("pending change", "M-1001")
                    )
                    changes = self.ledger.db.total_changes
                    if all_tasks:
                        replay_all(self.ledger)
                    else:
                        replay(self.ledger, "M-1001")
                    self.assertTrue(self.ledger.db.in_transaction)
                    self.assertEqual(self.ledger.task("M-1001")["title"], "pending change")
                    self.assertEqual(self.ledger.db.total_changes, changes)
                finally:
                    self.ledger.db.rollback()
                self.assertEqual(self.ledger.task("M-1001")["title"], original_title)

    def test_missing_task_refusal_preserves_transaction_ownership(self) -> None:
        self._lifecycle()
        with self.assertRaises(NotFound):
            replay(self.ledger, "M-404")
        self.assertFalse(self.ledger.db.in_transaction)
        self.ledger.db.execute("BEGIN")
        try:
            self.ledger.db.execute(
                "UPDATE tickets SET title=? WHERE id=?", ("pending change", "M-1001")
            )
            with self.assertRaises(NotFound):
                replay(self.ledger, "M-404")
            self.assertTrue(self.ledger.db.in_transaction)
            self.assertEqual(self.ledger.task("M-1001")["title"], "pending change")
        finally:
            self.ledger.db.rollback()

    def test_replay_keeps_one_snapshot_when_a_writer_commits(self) -> None:
        self.ledger.add_task("M-1001", "objective", "acceptance")
        original_task = self.ledger.task
        with create_ledger(self.ledger.path) as writer:
            writer.db.execute("PRAGMA busy_timeout=100")

            def read_then_write(task_id: str) -> dict:
                task = original_task(task_id)
                writer.triage(task_id)
                return task

            with patch.object(self.ledger, "task", side_effect=read_then_write):
                result = replay(self.ledger, "M-1001")
        self.assertEqual(result["current_station"], "INBOX")
        self.assertEqual(result["reconstructed_station"], "INBOX")
        self.assertTrue(result["consistent"])
        self.assertEqual(self.ledger.task("M-1001")["status"], "READY")

    def test_replay_all_keeps_one_snapshot_across_tasks(self) -> None:
        self.ledger.add_task("M-1001", "objective", "acceptance")
        self.ledger.add_task("M-1002", "objective", "acceptance")
        original_task = self.ledger.task
        with create_ledger(self.ledger.path) as writer:
            writer.db.execute("PRAGMA busy_timeout=100")

            def read_then_write(task_id: str) -> dict:
                task = original_task(task_id)
                if task_id == "M-1001":
                    writer.triage("M-1002")
                return task

            with patch.object(self.ledger, "task", side_effect=read_then_write):
                result = replay_all(self.ledger)
        self.assertEqual([task["current_station"] for task in result["tasks"]], ["INBOX", "INBOX"])
        self.assertEqual(result["inconsistent"], [])
        self.assertEqual(self.ledger.task("M-1002")["status"], "READY")

    def test_replay_all_covers_every_task(self) -> None:
        self._lifecycle()
        result = replay_all(self.ledger)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["inconsistent"], [])

    def test_reconstruct_handles_an_empty_log(self) -> None:
        result = reconstruct([])
        self.assertEqual(result["final_station"], "")
        self.assertEqual(result["steps"], [])


class DoctorTests(unittest.TestCase):
    def test_the_checks_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_checks(Path(directory) / "ledger.sqlite")
        self.assertIn("checks", result)
        self.assertTrue(result["checks"])
        self.assertIn("healthy", result)

    def test_a_missing_ledger_is_a_warning_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_checks(Path(directory) / "absent.sqlite")
        self.assertTrue(result["healthy"])
        self.assertIn("ledger", result["warnings"])

    def test_an_existing_ledger_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite"
            ledger = create_ledger(path)
            ledger.close()
            result = run_checks(path)
        self.assertIn("ledger", [check["name"] for check in result["checks"]])
        self.assertNotIn("ledger", result["warnings"])

    def test_the_checks_never_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite"
            result = run_checks(path)
        self.assertFalse(path.exists())
        self.assertIn("never repair", result["note"])


class DemoTests(unittest.TestCase):
    def test_the_demo_runs_a_full_lifecycle(self) -> None:
        result = run_demo()
        self.assertTrue(result["steps"])
        self.assertTrue(result["narrative"])
        self.assertIn("DONE", result["narrative"][-1] + "".join(result["narrative"]))

    def test_the_demo_reports_the_acceptance_gate(self) -> None:
        result = run_demo()
        joined = "\n".join(result["narrative"])
        self.assertIn("REVIEW", joined)
        self.assertIn("accepted", joined.lower())

    def test_the_demo_uses_a_scratch_ledger(self) -> None:
        result = run_demo()
        self.assertTrue(result["temporary"])
        self.assertIn("scratch ledger", result["note"])

    def test_the_demo_text_rendering_is_readable(self) -> None:
        text = demo_text(run_demo())
        self.assertIn("El Hidratador demonstration", text)
        self.assertIn("M-1001", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
