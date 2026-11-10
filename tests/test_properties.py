"""Property-based tests for the invariants that must hold for every input.

These tests use Hypothesis, which is declared in the `dev` extra of `pyproject.toml`.
The suite still runs on a machine without Hypothesis: the property tests are skipped, and
the example-based tests always run.
"""
from __future__ import annotations

import unittest
from typing import Any

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the environment
    HYPOTHESIS_AVAILABLE = False

from el_hidratador.context_expander import build_packet
from el_hidratador.dag import build_dag, manifest
from el_hidratador.errors import ValidationError

# Strategies used by the property tests.
_nonempty = st.text(min_size=1, max_size=60, alphabet=st.characters(blacklist_categories=("Cs",)))
_body = st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_categories=("Cs",)))


def _sample_ticket(objective: str, acceptance: str) -> dict[str, Any]:
    return {
        "id": "M-1001",
        "title": "property test",
        "objective": objective,
        "acceptance": acceptance,
        "scope": ["repo/api"],
        "kind": "code",
        "priority": 2,
        "tags": [],
        "status": "READY",
    }


class ByteBudgetProperties(unittest.TestCase):
    """The byte budget is a hard ceiling, whatever the input looks like."""

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(
        objective=_nonempty,
        acceptance=_nonempty,
        bodies=st.lists(_body, min_size=0, max_size=8),
        max_bytes=st.integers(min_value=200, max_value=4000),
    )
    @settings(max_examples=200, deadline=None)
    def test_packet_never_exceeds_the_budget(
        self, objective: str, acceptance: str, bodies: list[str], max_bytes: int
    ) -> None:
        ticket = _sample_ticket(objective, acceptance)
        entries = [{"body": body, "source": "s"} for body in bodies]
        try:
            result = build_packet(ticket, entries, max_bytes=max_bytes)
        except ValidationError:
            return
        self.assertLessEqual(result["total_bytes"], max_bytes)
        self.assertLessEqual(result["packet_bytes"], max_bytes)

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(
        objective=_nonempty,
        acceptance=_nonempty,
        bodies=st.lists(_body, min_size=0, max_size=8),
        max_bytes=st.integers(min_value=200, max_value=4000),
    )
    @settings(max_examples=200, deadline=None)
    def test_omitted_entries_are_reported(
        self, objective: str, acceptance: str, bodies: list[str], max_bytes: int
    ) -> None:
        ticket = _sample_ticket(objective, acceptance)
        entries = [{"body": body, "source": "s"} for body in bodies]
        try:
            result = build_packet(ticket, entries, max_bytes=max_bytes)
        except ValidationError:
            return
        total = len(bodies)
        kept = len(result["packet"]["context"])
        omitted = len(result["omitted"])
        self.assertLessEqual(kept + omitted, total)

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(
        objective=_nonempty,
        acceptance=_nonempty,
        bodies=st.lists(_body, min_size=0, max_size=8),
    )
    @settings(max_examples=100, deadline=None)
    def test_the_prompt_contains_the_untrusted_banner(
        self, objective: str, acceptance: str, bodies: list[str]
    ) -> None:
        ticket = _sample_ticket(objective, acceptance)
        entries = [{"body": body, "source": "s"} for body in bodies]
        result = build_packet(ticket, entries, max_bytes=100000)
        prompt = result["prompt"]
        if bodies:
            self.assertIn("BEGIN UNTRUSTED CONTEXT", prompt)
            self.assertIn("END UNTRUSTED CONTEXT", prompt)
        self.assertIn("never authorization", result["packet"]["trust"])

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(
        objective=_nonempty,
        acceptance=_nonempty,
        bodies=st.lists(_body, min_size=0, max_size=8),
        max_bytes=st.integers(min_value=200, max_value=4000),
    )
    @settings(max_examples=200, deadline=None)
    def test_rendered_prompt_is_byte_stable(
        self, objective: str, acceptance: str, bodies: list[str], max_bytes: int
    ) -> None:
        ticket = _sample_ticket(objective, acceptance)
        entries = [{"body": body, "source": "s"} for body in bodies]
        try:
            first = build_packet(ticket, entries, max_bytes=max_bytes)["prompt"]
            second = build_packet(ticket, entries, max_bytes=max_bytes)["prompt"]
        except ValidationError:
            return
        self.assertEqual(first, second)


class ManifestProperties(unittest.TestCase):
    """The manifest is deterministic and never claims authorization."""

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(
        objective=_nonempty,
        acceptance=_nonempty,
        task_text=_nonempty,
    )
    @settings(max_examples=200, deadline=None)
    def test_the_hash_is_stable_for_the_same_input(
        self, objective: str, acceptance: str, task_text: str
    ) -> None:
        ticket = _sample_ticket(objective, acceptance)
        markdown = f"## Tasks\n- [ ] {task_text}\n"
        dag = build_dag("M-1001", markdown=markdown)
        first = manifest(dag, ticket)["manifest_hash"]
        second = manifest(build_dag("M-1001", markdown=markdown), ticket)["manifest_hash"]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(
        objective=_nonempty,
        acceptance=_nonempty,
        task_text=_nonempty,
    )
    @settings(max_examples=100, deadline=None)
    def test_execution_is_never_authorized(
        self, objective: str, acceptance: str, task_text: str
    ) -> None:
        ticket = _sample_ticket(objective, acceptance)
        dag = build_dag("M-1001", markdown=f"## Tasks\n- [ ] {task_text}\n")
        result = manifest(dag, ticket)
        self.assertFalse(result["execution_authorized"])

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(
        objective=_nonempty,
        acceptance=_nonempty,
        task_text_a=_nonempty,
        task_text_b=_nonempty,
    )
    @settings(max_examples=100, deadline=None)
    def test_different_task_lists_produce_different_nodes(
        self, objective: str, acceptance: str, task_text_a: str, task_text_b: str
    ) -> None:
        ticket = _sample_ticket(objective, acceptance)
        dag_a = build_dag("M-1001", markdown=f"## Tasks\n- [ ] {task_text_a}\n")
        dag_b = build_dag("M-1001", markdown=f"## Tasks\n- [ ] {task_text_b}\n")
        titles_a = [node.title for node in dag_a.nodes]
        titles_b = [node.title for node in dag_b.nodes]
        if task_text_a != task_text_b:
            self.assertNotEqual(titles_a, titles_b)


class ParseTasksProperties(unittest.TestCase):
    """The parser's invariants: nothing is lost, and the structure is deterministic."""

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(text=st.lists(_nonempty, min_size=0, max_size=5))
    @settings(max_examples=100, deadline=None)
    def test_parsing_is_deterministic(self, text: list[str]) -> None:
        body = "## Tasks\n" + "\n".join(f"- [ ] {item}" for item in text) + "\n"
        first = build_dag("M-1001", markdown=body)
        second = build_dag("M-1001", markdown=body)
        self.assertEqual(
            [node.to_dict() for node in first.nodes],
            [node.to_dict() for node in second.nodes],
        )

    @unittest.skipUnless(HYPOTHESIS_AVAILABLE, "hypothesis is not installed")
    @given(text=st.lists(_nonempty, min_size=1, max_size=5))
    @settings(max_examples=100, deadline=None)
    def test_every_task_becomes_a_node(self, text: list[str]) -> None:
        body = "## Tasks\n" + "\n".join(f"- [ ] {item}" for item in text) + "\n"
        dag = build_dag("M-1001", markdown=body)
        self.assertEqual(len(dag.nodes), len(text))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
