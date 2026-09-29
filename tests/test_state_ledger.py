"""Tests for scripts/state_ledger.py (MASTER_PLAN.md §6.1 step 7).

The ledger is a cache, not the mutex (ADR-004), so the tests that matter are less
about features and more about the two promises the module makes: it never keeps a
document its own validator would reject, and it never leaves a half-written file
behind.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import state_ledger  # noqa: E402
import validate_state  # noqa: E402

SCHEMA = REPO_ROOT / "schemas" / "state.schema.json"


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "STATE.json"

    def run_ledger(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state_ledger.main([*argv, "--file", str(self.state), "--schema", str(SCHEMA)])
        return code, out.getvalue(), err.getvalue()

    def claim(self, task_id: str = "TASK-900", run_id: str = "run-1") -> tuple[int, str, str]:
        return self.run_ledger("claim", task_id, "--run-id", run_id,
                               "--branch", "agent/task-900", "--route", "hermes")

    def document(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))


class Writing(LedgerCase):
    def test_claim_creates_a_document_its_own_validator_accepts(self) -> None:
        code, _, err = self.claim()
        self.assertEqual(0, code, err)
        schema = validate_state.load_schema(SCHEMA)
        self.assertEqual([], validate_state.validate_document(self.document(), schema))

    def test_claim_records_the_holder_and_the_task(self) -> None:
        self.claim(run_id="run-7")
        document = self.document()
        self.assertEqual("run-7", document["lock"]["held_by"])
        entry = document["tasks"]["TASK-900"]
        self.assertEqual("in_progress", entry["status"])
        self.assertEqual("agent/task-900", entry["branch"])
        self.assertEqual(1, entry["attempts"])

    def test_attempts_accumulate_across_runs_and_stop_at_three(self) -> None:
        """§4: three attempts force a task to blocked, so the count must survive a re-route."""
        for n, expected in enumerate([1, 2, 3, 3, 3], start=1):
            self.claim(run_id=f"run-{n}")
            self.assertEqual(expected, self.document()["tasks"]["TASK-900"]["attempts"])

    def test_finish_sets_status_and_pr(self) -> None:
        self.claim()
        code, _, err = self.run_ledger("finish", "TASK-900", "--status", "review", "--pr", "42")
        self.assertEqual(0, code, err)
        entry = self.document()["tasks"]["TASK-900"]
        self.assertEqual("review", entry["status"])
        self.assertEqual(42, entry["pr"])

    def test_finishing_an_unrecorded_task_is_not_a_failure(self) -> None:
        code, _, err = self.run_ledger("finish", "TASK-901", "--status", "blocked")
        self.assertEqual(0, code)
        self.assertIn("no record of TASK-901", err)

    def test_unlock_clears_the_holder_but_keeps_the_ttl(self) -> None:
        self.claim()
        self.run_ledger("unlock")
        lock = self.document()["lock"]
        self.assertIsNone(lock["held_by"])
        self.assertIsNone(lock["acquired_at"])
        self.assertEqual(900, lock["ttl_seconds"])

    def test_show_on_an_absent_file_prints_empty_json(self) -> None:
        code, out, _ = self.run_ledger("show")
        self.assertEqual(0, code)
        self.assertEqual({}, json.loads(out))


class Resilience(LedgerCase):
    def test_a_corrupt_ledger_is_rebuilt_rather_than_fatal(self) -> None:
        """§3.4 calls this file a rebuildable cache, so a bad one must not stop routing."""
        self.state.write_text("{ this is not json", encoding="utf-8")
        code, _, err = self.claim()
        self.assertEqual(0, code, err)
        self.assertIn("unreadable", err)
        self.assertEqual("run-1", self.document()["lock"]["held_by"])

    def test_a_document_that_would_be_invalid_is_never_written(self) -> None:
        self.claim()
        before = self.state.read_text(encoding="utf-8")

        original = state_ledger.empty_document
        state_ledger.empty_document = lambda: {"schema_version": "not-a-version"}
        self.state.unlink()
        try:
            code, _, err = self.claim()
        finally:
            state_ledger.empty_document = original

        self.assertEqual(1, code, "an invalid document was accepted")
        self.assertIn("would have written an invalid document", err)
        self.assertFalse(self.state.exists(), "an invalid document reached the disk")

        # And the valid document from before is recoverable, proving the failure
        # path does not depend on having destroyed it.
        self.state.write_text(before, encoding="utf-8")
        self.assertEqual("run-1", self.document()["lock"]["held_by"])

    def test_no_temporary_files_are_left_behind(self) -> None:
        self.claim()
        self.run_ledger("finish", "TASK-900", "--status", "review")
        self.run_ledger("unlock")
        leftovers = [p.name for p in self.dir.iterdir() if p.name != "STATE.json"]
        self.assertEqual([], leftovers, f"atomic replace left files behind: {leftovers}")


if __name__ == "__main__":
    unittest.main()
