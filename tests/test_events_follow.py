"""``events --follow`` tails the audit log without replaying history (gh #6)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from el_hidratador.cli import follow_events, main
from el_hidratador.core import create_ledger


class FollowEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.sqlite"
        self.ledger = create_ledger(self.db)
        self.ledger.add_task(
            "M-1001",
            "synthetic objective",
            "synthetic acceptance",
            scope=["repo/api"],
        )
        self.ledger.add_task(
            "M-1002",
            "synthetic objective",
            "synthetic acceptance",
            scope=["repo/web"],
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def test_follow_starts_after_the_newest_event(self) -> None:
        seen: list[int] = []

        def source(after: int) -> list[dict]:
            seen.append(after)
            return []

        def sleep(_seconds: float) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            follow_events(self.ledger, source=source, emit=lambda _line: None, sleep=sleep)

        self.assertEqual(seen, [self.ledger.latest_event_seq()])

    def test_follow_prints_only_new_events_as_ndjson(self) -> None:
        lines: list[str] = []
        first = self.ledger.latest_event_seq()
        later = {
            "seq": first + 1,
            "type": "task.updated",
            "task_id": "M-1001",
            "data": {},
            "created": 1.0,
            "created_iso": "1970-01-01T00:00:01.000Z",
        }

        def source(after: int) -> list[dict]:
            return [later] if after == first else []

        calls = 0

        def sleep(_seconds: float) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            follow_events(self.ledger, source=source, emit=lines.append, sleep=sleep)

        self.assertEqual([json.loads(line) for line in lines], [later])

    def test_sigint_exits_130_without_breaking_the_pipe(self) -> None:
        output = io.StringIO()
        with (
            patch("el_hidratador.cli.follow_events", side_effect=KeyboardInterrupt),
            redirect_stdout(output),
        ):
            code = main(["--db", str(self.db), "events", "--follow"])

        self.assertEqual(code, 130)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
