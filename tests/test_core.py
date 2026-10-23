"""Tests for the local coordination ledger."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from el_hidratador.core import create_ledger
from el_hidratador.errors import (
    BudgetExceeded,
    Conflict,
    LeaseExpired,
    NotFound,
    ValidationError,
)


class LedgerTestCase(unittest.TestCase):
    """Base fixture: one fresh ledger per test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.ledger = create_ledger(self.path)

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def make_task(self, task_id: str = "M-1001", **overrides):
        kwargs = {
            "task_id": task_id,
            "objective": "Implement the thing",
            "acceptance": "The thing works and is tested",
            "scope": ["repo/api"],
        }
        kwargs.update(overrides)
        return self.ledger.add_task(**kwargs)


class TaskLifecycleTests(LedgerTestCase):
    def test_add_task_creates_inbox_ticket(self) -> None:
        result = self.make_task()
        self.assertTrue(result["created"])
        ticket = self.ledger.task("M-1001")
        self.assertEqual(ticket["status"], "INBOX")
        self.assertEqual(ticket["scope"], ["repo/api"])

    def test_add_task_requires_text_fields(self) -> None:
        with self.assertRaises(ValidationError):
            self.ledger.add_task("M-1", "", "acceptance")
        with self.assertRaises(ValidationError):
            self.ledger.add_task("M-1", "objective", "  ")

    def test_add_task_rejects_bad_budget_and_kind(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_task(token_budget=0)
        with self.assertRaises(ValidationError):
            self.make_task(kind="imaginary")

    def test_seed_key_is_idempotent(self) -> None:
        first = self.ledger.add_task("M-2001", "objective", "acceptance", seed_key="auth-jwt-v1")
        second = self.ledger.add_task("M-2002", "objective", "acceptance", seed_key="auth-jwt-v1")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["task_id"], "M-2001")
        self.assertEqual(len(self.ledger.tasks()), 1)

    def test_triage_moves_inbox_to_ready(self) -> None:
        self.make_task()
        ticket = self.ledger.triage("M-1001")
        self.assertEqual(ticket["status"], "READY")

    def test_triage_refuses_non_inbox(self) -> None:
        self.make_task()
        self.ledger.triage("M-1001")
        with self.assertRaises(Conflict):
            self.ledger.triage("M-1001")

    def test_unknown_task_raises_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.ledger.task("M-9999")

    def test_update_rejects_lifecycle_fields(self) -> None:
        self.make_task()
        with self.assertRaises(ValidationError):
            self.ledger.update_task("M-1001", status="DONE")

    def test_update_accepts_documented_fields(self) -> None:
        self.make_task()
        ticket = self.ledger.update_task("M-1001", owner="terminal-2", tags=["security"])
        self.assertEqual(ticket["owner"], "terminal-2")
        self.assertEqual(ticket["tags"], ["security"])


class TicketValidationTests(LedgerTestCase):
    def invalid_scalar_fields(self):
        return {
            "token_budget": (0, -1, True, 1.5, "10", None, [10], float("inf"), float("nan"), 2**63),
            "objective": ("", "  ", None, 1, []),
            "acceptance": ("", "\t", None, 1, []),
            "kind": ("", " ", "invented", None, 1, []),
            "priority": (0, 4, -1, True, 1.5, "2", None, [2], float("inf")),
            "title": (None, 1, []),
            "owner": (None, 1, []),
            "description": (None, 1, []),
        }

    def test_invalid_scalar_updates_are_atomic_typed_refusals(self) -> None:
        self.make_task()
        for field, values in self.invalid_scalar_fields().items():
            for value in values:
                with self.subTest(field=field, value=value):
                    before = self.ledger.task("M-1001")
                    events = self.ledger.events()
                    with self.assertRaises(ValidationError):
                        self.ledger.update_task("M-1001", **{"owner": "changed", field: value})
                    self.assertEqual(self.ledger.task("M-1001"), before)
                    self.assertEqual(self.ledger.events(), events)

    def test_creation_enforces_the_same_scalar_constraints(self) -> None:
        index = 1001
        for field, values in self.invalid_scalar_fields().items():
            for value in values:
                with self.subTest(field=field, value=value):
                    events = self.ledger.events()
                    with self.assertRaises(ValidationError):
                        self.make_task(f"M-{index}", **{field: value})
                    self.assertEqual(self.ledger.events(), events)
                    with self.assertRaises(NotFound):
                        self.ledger.task(f"M-{index}")
                index += 1

    def test_invalid_collection_updates_are_atomic_typed_refusals(self) -> None:
        self.make_task()
        for field in ("scope", "tags", "depends_on", "blocks", "acceptance_criteria"):
            values = [None, "repo/api", b"repo/api", {"repo/api": True}, [None], [1]]
            if field == "scope":
                values.extend([[], [""], ["  "], ["repo/api", " "]])
            for value in values:
                with self.subTest(field=field, value=value):
                    before = self.ledger.task("M-1001")
                    events = self.ledger.events()
                    with self.assertRaises(ValidationError):
                        self.ledger.update_task("M-1001", title="changed", **{field: value})
                    self.assertEqual(self.ledger.task("M-1001"), before)
                    self.assertEqual(self.ledger.events(), events)

    def test_creation_refuses_invalid_collection_types_and_empty_scope(self) -> None:
        index = 1001
        for field in ("scope", "tags", "depends_on", "blocks", "acceptance_criteria"):
            values = ["repo/api", b"repo/api", {"repo/api": True}, [None], [1]]
            if field == "scope":
                values.extend([[], [""], ["  "], ["repo/api", " "]])
            for value in values:
                with self.subTest(field=field, value=value):
                    events = self.ledger.events()
                    with self.assertRaises(ValidationError):
                        self.make_task(f"M-{index}", **{field: value})
                    self.assertEqual(self.ledger.events(), events)
                    with self.assertRaises(NotFound):
                        self.ledger.task(f"M-{index}")
                index += 1

    def test_refused_infinite_budget_preserves_budget_enforcement(self) -> None:
        self.make_task(token_budget=10)
        with self.assertRaises(ValidationError):
            self.ledger.update_task("M-1001", token_budget=float("inf"))
        session = self.ledger.start("M-1001", "terminal-1")
        result = self.ledger.checkpoint(session["session_id"], "summary", "evidence", "next", 10)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.ledger.task("M-1001")["token_budget"], 10)

    def test_refused_empty_scope_preserves_scope_exclusivity(self) -> None:
        self.make_task("M-1001", scope=["repo/api"])
        self.make_task("M-1002", scope=["repo/api"])
        with self.assertRaises(ValidationError):
            self.ledger.update_task("M-1001", scope=[])
        self.ledger.start("M-1001", "terminal-1")
        with self.assertRaises(Conflict):
            self.ledger.start("M-1002", "terminal-2")

    def test_integer_budget_storage_boundaries_remain_supported(self) -> None:
        for index, budget in enumerate((1, 2**63 - 1)):
            with self.subTest(budget=budget):
                task_id = f"M-{1001 + index}"
                self.make_task(task_id, token_budget=budget)
                self.assertEqual(self.ledger.task(task_id)["token_budget"], budget)
                self.ledger.update_task(task_id, token_budget=budget)
                self.assertEqual(self.ledger.task(task_id)["token_budget"], budget)

    def test_deliberate_budget_increase_keeps_cumulative_accounting(self) -> None:
        self.make_task(token_budget=10)
        first = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(first["session_id"], "summary", "evidence", "next", 10)
        self.ledger.update_task("M-1001", token_budget=20)
        self.ledger.decide("M-1001", "retry", "operator increased the budget")
        second = self.ledger.start("M-1001", "terminal-2")
        result = self.ledger.checkpoint(second["session_id"], "summary", "new evidence", "next", 10)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.ledger.used_tokens("M-1001"), 20)

    def test_valid_updates_normalize_kind_and_emit_one_event(self) -> None:
        self.make_task()
        events = self.ledger.events()
        fields = {
            "objective": "new objective",
            "acceptance": "new acceptance",
            "kind": " FIX ",
            "scope": ("repo/web",),
            "token_budget": 20,
            "priority": 3,
            "tags": ("security",),
            "depends_on": ("M-1002",),
            "blocks": (),
            "acceptance_criteria": ("focused tests pass",),
            "title": "",
            "owner": "",
            "description": "",
        }
        ticket = self.ledger.update_task("M-1001", **fields)
        self.assertEqual(ticket["kind"], "fix")
        self.assertEqual(ticket["scope"], ["repo/web"])
        self.assertEqual(ticket["tags"], ["security"])
        self.assertEqual(ticket["token_budget"], 20)
        self.assertEqual(ticket["priority"], 3)
        self.assertEqual(ticket["status"], "INBOX")
        self.assertEqual(len(self.ledger.events()), len(events) + 1)
        self.assertEqual(self.ledger.events()[-1]["data"], {"fields": sorted(fields)})

    def test_creation_defaults_tuples_and_seed_idempotency_remain_supported(self) -> None:
        self.make_task(
            "M-1001", scope=None, kind=" FIX ", tags=("security",), seed_key="synthetic-seed"
        )
        before = self.ledger.task("M-1001")
        events = self.ledger.events()
        second = self.make_task("M-1002", scope=("repo/web",), seed_key="synthetic-seed")
        self.assertTrue(second["idempotent"])
        self.assertEqual(before["scope"], ["M-1001"])
        self.assertEqual(before["kind"], "fix")
        self.assertEqual(before["tags"], ["security"])
        self.assertEqual(self.ledger.task("M-1001"), before)
        self.assertEqual(self.ledger.events(), events)


class ScopeReservationTests(LedgerTestCase):
    def reserve(self, task_id: str, status: str) -> list[str]:
        scope = [f"repo/{task_id}/api", f"repo/{task_id}/web"]
        self.make_task(task_id, scope=scope)
        session = self.ledger.start(task_id, "terminal-1")
        if status == "BLOCKED":
            self.ledger.fail(session["session_id"], "tool", "tool unavailable")
        elif status == "REVIEW":
            self.ledger.checkpoint(session["session_id"], "summary", "evidence", "review", 10)
            self.ledger.finish(session["session_id"], "review evidence")
        return scope

    def test_scope_changes_cannot_release_or_expand_a_reservation(self) -> None:
        for index, status in enumerate(("IN_PROGRESS", "BLOCKED", "REVIEW")):
            task_id = f"M-{1001 + index}"
            scope = self.reserve(task_id, status)
            for replacement in ([], ["repo/replacement"], [*scope, "repo/additional"]):
                with self.subTest(status=status, replacement=replacement):
                    before = self.ledger.task(task_id)
                    events = self.ledger.events()
                    with self.assertRaises(Conflict):
                        self.ledger.update_task(task_id, title="changed", scope=replacement)
                    self.assertEqual(self.ledger.task(task_id), before)
                    self.assertEqual(self.ledger.events(), events)
            contender = f"M-{2001 + index}"
            self.make_task(contender, scope=scope)
            with self.assertRaises(Conflict):
                self.ledger.start(contender, "terminal-2")

    def test_equivalent_scope_updates_preserve_reservations(self) -> None:
        for index, status in enumerate(("IN_PROGRESS", "BLOCKED", "REVIEW")):
            with self.subTest(status=status):
                task_id = f"M-{1001 + index}"
                scope = self.reserve(task_id, status)
                ticket = self.ledger.update_task(
                    task_id,
                    scope=(scope[1], scope[0], scope[0]),
                    owner="terminal-2",
                    tags=["reviewed"],
                )
                self.assertEqual(set(ticket["scope"]), set(scope))
                self.assertEqual(ticket["owner"], "terminal-2")
                self.assertEqual(ticket["tags"], ["reviewed"])
                self.assertEqual(ticket["status"], status)
                self.assertEqual(self.ledger.events()[-1]["type"], "task.updated")

    def test_other_fields_can_change_while_scope_is_reserved(self) -> None:
        scope = self.reserve("M-1001", "IN_PROGRESS")
        ticket = self.ledger.update_task("M-1001", description="clarified description")
        self.assertEqual(ticket["description"], "clarified description")
        self.assertEqual(ticket["scope"], scope)
        self.assertEqual(ticket["status"], "IN_PROGRESS")

    def test_scope_can_change_before_start(self) -> None:
        for index, status in enumerate(("INBOX", "READY")):
            with self.subTest(status=status):
                task_id = f"M-{1001 + index}"
                self.make_task(task_id)
                if status == "READY":
                    self.ledger.triage(task_id)
                scope = [f"repo/{task_id}/updated"]
                self.ledger.update_task(task_id, scope=scope)
                self.ledger.start(task_id, "terminal-1")
                self.assertEqual(self.ledger.task(task_id)["scope"], scope)

    def test_scope_can_change_after_explicit_operator_release(self) -> None:
        for index, decision in enumerate(("retry", "cancel", "accept")):
            with self.subTest(decision=decision):
                task_id = f"M-{1001 + index}"
                original_scope = self.reserve(task_id, "REVIEW")
                self.ledger.decide(task_id, decision, "operator reviewed the reservation")
                scope = [f"repo/{task_id}/updated"]
                self.ledger.update_task(task_id, scope=scope)
                self.assertEqual(self.ledger.task(task_id)["scope"], scope)
                contender = f"M-{2001 + index}"
                self.make_task(contender, scope=original_scope)
                self.ledger.start(contender, "terminal-2")
                self.assertEqual(self.ledger.task(contender)["status"], "IN_PROGRESS")


class SessionAndLeaseTests(LedgerTestCase):
    def test_start_acquires_lease_and_moves_to_in_progress(self) -> None:
        self.make_task()
        result = self.ledger.start("M-1001", "terminal-1", 900)
        self.assertIn("session_id", result)
        self.assertEqual(self.ledger.task("M-1001")["status"], "IN_PROGRESS")

    def test_start_requires_owner(self) -> None:
        self.make_task()
        with self.assertRaises(ValidationError):
            self.ledger.start("M-1001", "", 900)

    def test_start_refuses_when_scope_is_reserved(self) -> None:
        self.make_task("M-1001", scope=["repo/api"])
        self.make_task("M-1002", scope=["repo/api"])
        self.ledger.start("M-1001", "terminal-1")
        with self.assertRaises(Conflict):
            self.ledger.start("M-1002", "terminal-2")

    def test_start_refuses_when_scope_overlaps(self) -> None:
        self.make_task("M-1001", scope=["repo/api"])
        self.make_task("M-1002", scope=["repo/api", "repo/web"])
        self.ledger.start("M-1001", "terminal-1")
        with self.assertRaises(Conflict):
            self.ledger.start("M-1002", "terminal-2")

    def test_disjoint_scopes_run_in_parallel(self) -> None:
        self.make_task("M-1001", scope=["repo/api"])
        self.make_task("M-1002", scope=["repo/web"])
        self.ledger.start("M-1001", "terminal-1")
        second = self.ledger.start("M-1002", "terminal-2")
        self.assertEqual(self.ledger.task("M-1002")["status"], "IN_PROGRESS")
        self.assertIn("session_id", second)

    def test_second_session_on_same_task_is_refused(self) -> None:
        self.make_task()
        self.ledger.start("M-1001", "terminal-1")
        with self.assertRaises(Conflict):
            self.ledger.start("M-1001", "terminal-2")

    def test_expired_lease_blocks_task_and_records_failure(self) -> None:
        self.make_task()
        result = self.ledger.start("M-1001", "terminal-1", lease_seconds=1)
        time.sleep(1.05)
        report = self.ledger.reconcile()
        self.assertIn("M-1001", report["expired_tasks"])
        self.assertEqual(self.ledger.task("M-1001")["status"], "BLOCKED")
        failures = self.ledger.failures("M-1001")
        self.assertEqual(failures[0]["category"], "lease_expired")
        with self.assertRaises(LeaseExpired):
            self.ledger.checkpoint(result["session_id"], "s", "e", "n", 10)

    def test_checkpoint_requires_evidence_fields(self) -> None:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        with self.assertRaises(ValidationError):
            self.ledger.checkpoint(session["session_id"], "", "evidence", "next", 10)

    def test_checkpoint_rejects_decreasing_tokens(self) -> None:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(session["session_id"], "s", "e1", "n", 100)
        with self.assertRaises(Conflict):
            self.ledger.checkpoint(session["session_id"], "s", "e2", "n", 50)

    def test_repeated_identical_evidence_blocks_for_no_progress(self) -> None:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(session["session_id"], "s", "same", "n", 10)
        self.ledger.checkpoint(session["session_id"], "s", "same", "n", 20)
        result = self.ledger.checkpoint(session["session_id"], "s", "same", "n", 30)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.ledger.task("M-1001")["status"], "BLOCKED")
        categories = [item["category"] for item in self.ledger.failures("M-1001")]
        self.assertIn("no_progress", categories)

    def test_fail_records_fingerprint_and_blocks(self) -> None:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        result = self.ledger.fail(session["session_id"], "tool", "Compiler missing")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(len(result["fingerprint"]), 16)
        self.assertEqual(self.ledger.task("M-1001")["status"], "BLOCKED")

    def test_fail_rejects_unknown_category(self) -> None:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        with self.assertRaises(ValidationError):
            self.ledger.fail(session["session_id"], "mystery", "?")

    def test_finish_requires_a_checkpoint(self) -> None:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        with self.assertRaises(Conflict):
            self.ledger.finish(session["session_id"], "evidence")

    def test_finish_moves_to_review_not_done(self) -> None:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(session["session_id"], "s", "e", "n", 10)
        result = self.ledger.finish(session["session_id"], "all endpoints tested")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(self.ledger.task("M-1001")["status"], "REVIEW")


class AcceptanceGateTests(LedgerTestCase):
    def _reviewable(self) -> str:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(session["session_id"], "s", "e", "n", 10)
        self.ledger.finish(session["session_id"], "done")
        return session["session_id"]

    def test_accept_moves_to_done_and_records_both_events(self) -> None:
        self._reviewable()
        result = self.ledger.decide("M-1001", "accept", "Reviewed diff and tests")
        self.assertEqual(result["status"], "DONE")
        types = [event["type"] for event in self.ledger.events()]
        self.assertIn("operator.accept", types)
        self.assertIn("task.done", types)

    def test_retry_returns_task_to_ready_and_keeps_tokens(self) -> None:
        session = self._reviewable()
        self.ledger.decide("M-1001", "retry", "Need more evidence")
        self.assertEqual(self.ledger.task("M-1001")["status"], "READY")
        self.assertEqual(self.ledger.used_tokens("M-1001"), 10)
        again = self.ledger.start("M-1001", "terminal-2")
        self.assertNotEqual(again["session_id"], session)

    def test_cancel_is_terminal(self) -> None:
        self._reviewable()
        self.ledger.decide("M-1001", "cancel", "Superseded")
        self.assertEqual(self.ledger.task("M-1001")["status"], "CANCELLED")

    def test_accept_is_refused_outside_review(self) -> None:
        self.make_task()
        with self.assertRaises(Conflict):
            self.ledger.decide("M-1001", "accept", "premature")

    def test_decision_requires_reason(self) -> None:
        self._reviewable()
        with self.assertRaises(ValidationError):
            self.ledger.decide("M-1001", "accept", "")

    def test_decision_rejects_unknown_value(self) -> None:
        self._reviewable()
        with self.assertRaises(ValidationError):
            self.ledger.decide("M-1001", "shrug", "reason")

    def test_acceptance_gate_is_never_bypassed_by_finish(self) -> None:
        self._reviewable()
        self.assertNotEqual(self.ledger.task("M-1001")["status"], "DONE")


class BudgetTests(LedgerTestCase):
    def test_budget_exhaustion_blocks_the_task(self) -> None:
        self.make_task(token_budget=100)
        session = self.ledger.start("M-1001", "terminal-1")
        result = self.ledger.checkpoint(session["session_id"], "s", "e", "n", 100)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.ledger.task("M-1001")["status"], "BLOCKED")
        categories = [item["category"] for item in self.ledger.failures("M-1001")]
        self.assertIn("budget", categories)

    def test_start_refuses_when_budget_exhausted(self) -> None:
        self.make_task(token_budget=100)
        session = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(session["session_id"], "s", "e", "n", 100)
        self.ledger.decide("M-1001", "retry", "operator asked for a second attempt")
        with self.assertRaises(BudgetExceeded):
            self.ledger.start("M-1001", "terminal-2")

    def test_budget_is_cumulative_across_attempts(self) -> None:
        self.make_task(token_budget=100)
        session = self.ledger.start("M-1001", "terminal-1")
        self.ledger.checkpoint(session["session_id"], "s", "e", "n", 60)
        self.ledger.fail(session["session_id"], "tool", "transient tool failure")
        self.ledger.decide("M-1001", "retry", "operator asked for a second attempt")
        second = self.ledger.start("M-1001", "terminal-2")
        result = self.ledger.checkpoint(second["session_id"], "s", "e2", "n", 60)
        self.assertEqual(result["status"], "blocked")
        categories = [item["category"] for item in self.ledger.failures("M-1001")]
        self.assertIn("budget", categories)

    def test_tokens_must_be_nonnegative(self) -> None:
        self.make_task()
        session = self.ledger.start("M-1001", "terminal-1")
        with self.assertRaises(ValidationError):
            self.ledger.checkpoint(session["session_id"], "s", "e", "n", -1)


class ContextTests(LedgerTestCase):
    def test_context_is_content_addressed(self) -> None:
        self.make_task()
        first = self.ledger.add_context("M-1001", "same body", "source-a")
        second = self.ledger.add_context("M-1001", "same body", "source-b")
        self.assertEqual(first["hash"], second["hash"])
        self.assertEqual(len(self.ledger.context_entries("M-1001")), 1)

    def test_pack_reports_omitted_context(self) -> None:
        self.make_task()
        self.ledger.add_context("M-1001", "x" * 400, "big", priority=10)
        self.ledger.add_context("M-1001", "y" * 400, "small", priority=1)
        result = self.ledger.pack("M-1001", 600)
        self.assertTrue(result["omitted"])
        self.assertEqual(result["omitted"][0]["reason"], "byte_budget")
        self.assertLessEqual(result["packet_bytes"], 600)

    def test_pack_refuses_when_mandatory_content_cannot_fit(self) -> None:
        self.make_task(objective="o" * 5000, acceptance="a" * 5000)
        with self.assertRaises(Conflict):
            self.ledger.pack("M-1001", 100)

    def test_pack_marks_context_as_untrusted(self) -> None:
        self.make_task()
        self.ledger.add_context("M-1001", "body", "source")
        result = self.ledger.pack("M-1001", 12000)
        self.assertIn("never authorization", result["packet"]["trust"])

    def test_expired_context_is_omitted_with_reason(self) -> None:
        self.make_task()
        self.ledger.add_context("M-1001", "body", "source", ttl=1)
        time.sleep(1.05)
        result = self.ledger.pack("M-1001", 12000)
        self.assertEqual(result["omitted"][0]["reason"], "expired")


class ProfileAndRoutingTests(LedgerTestCase):
    def test_profile_requires_known_cost_and_kind(self) -> None:
        with self.assertRaises(ValidationError):
            self.ledger.profile("p", ["code"], 1000, "free")
        with self.assertRaises(ValidationError):
            self.ledger.profile("p", ["imaginary"], 1000, "zero_incremental")

    def test_route_marks_paid_profiles_ineligible(self) -> None:
        self.make_task(kind="code")
        self.ledger.profile("paid", ["code"], 64000, "paid")
        result = self.ledger.route("M-1001", 4096)
        option = result["options"][0]
        self.assertFalse(option["eligible"])
        self.assertFalse(result["execution_authorized"])
        self.assertTrue(any("spending" in reason for reason in option["reasons"]))

    def test_route_accepts_a_declared_zero_cost_profile(self) -> None:
        self.make_task(kind="code")
        self.ledger.profile("local", ["code"], 64000, "zero_incremental")
        result = self.ledger.route("M-1001", 4096)
        self.assertTrue(result["options"][0]["eligible"])
        self.assertFalse(result["execution_authorized"])

    def test_expired_profile_is_ineligible(self) -> None:
        self.make_task(kind="code")
        self.ledger.profile("local", ["code"], 64000, "zero_incremental", ttl=1)
        time.sleep(1.05)
        result = self.ledger.route("M-1001", 4096)
        self.assertFalse(result["options"][0]["eligible"])

    def test_route_rejects_nonpositive_context(self) -> None:
        self.make_task()
        with self.assertRaises(ValidationError):
            self.ledger.route("M-1001", 0)


class ReportingTests(LedgerTestCase):
    def test_events_are_ordered_and_auditable(self) -> None:
        self.make_task()
        self.ledger.start("M-1001", "terminal-1")
        events = self.ledger.events()
        self.assertEqual(events[0]["type"], "task.created")
        self.assertEqual(events[1]["type"], "session.started")
        self.assertTrue(all(event["seq"] < events[0]["seq"] + 100 for event in events))

    def test_status_reports_unmeasured_metrics_as_none(self) -> None:
        self.make_task()
        status = self.ledger.status()
        self.assertIsNone(status["metrics"]["measured_cash_cost"])
        self.assertIsNone(status["metrics"]["measured_token_savings"])
        self.assertEqual(status["metrics"]["reported_tokens"], 0)

    def test_failure_summary_groups_by_fingerprint(self) -> None:
        self.make_task("M-1001", scope=["repo/api"])
        self.make_task("M-1002", scope=["repo/web"])
        for task in ("M-1001", "M-1002"):
            session = self.ledger.start(task, "terminal-1")
            self.ledger.fail(session["session_id"], "tool", "Compiler missing")
        summary = self.ledger.failure_summary()
        self.assertEqual(summary[0]["occurrences"], 2)

    def test_handoff_is_recorded(self) -> None:
        self.make_task()
        result = self.ledger.record_handoff("M-1001", {"nodes": []}, {"packet": True})
        self.assertEqual(len(self.ledger.handoffs("M-1001")), 1)
        self.assertEqual(result["task_id"], "M-1001")


class StorageTests(LedgerTestCase):
    def test_ledger_file_is_not_world_readable(self) -> None:
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode & 0o077, 0)

    def test_schema_version_is_recorded(self) -> None:
        self.assertGreaterEqual(self.ledger.schema_version, 3)

    def test_reopening_a_ledger_keeps_state(self) -> None:
        self.make_task()
        self.ledger.close()
        reopened = create_ledger(self.path)
        try:
            self.assertEqual(len(reopened.tasks()), 1)
        finally:
            reopened.close()
        self.ledger = create_ledger(self.path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
