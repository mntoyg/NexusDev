"""Tests for scripts/validate_state.py (TASK-001).

Standard library only, same as the module under test. Each case starts from the
example document the module itself ships and breaks exactly one thing, so a
failure names the rule rather than the fixture.
"""

from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_state  # noqa: E402  (path shim must come first)

SCHEMA_PATH = REPO_ROOT / "schemas" / "state.schema.json"


def schema() -> dict:
    return validate_state.load_schema(SCHEMA_PATH)


def broken(**changes) -> dict:
    """The shipped example with top-level keys replaced."""
    document = validate_state.example_document()
    document.update(changes)
    return document


def with_task(task: dict) -> dict:
    document = validate_state.example_document()
    document["tasks"] = {"TASK-042": task}
    return document


VALID_TASK = {
    "status": "in_progress",
    "route": "hermes",
    "assigned_to": "aider",
    "branch": "agent/task-042",
    "started_at": "2026-09-08T10:30:02Z",
    "attempts": 1,
    "pr": None,
}


class SchemaItself(unittest.TestCase):
    def test_schema_loads(self) -> None:
        self.assertEqual("object", schema()["type"])

    def test_unsupported_keyword_is_refused(self) -> None:
        """The guard that makes a single source of truth safe.

        A validator that ignores a keyword it does not implement is worse than
        none, because the schema would look like it enforces something it does
        not. This proves the refusal is real rather than aspirational.
        """
        loaded = schema()
        loaded["properties"]["schema_version"]["maxLength"] = 5
        with self.assertRaises(validate_state.SchemaError) as caught:
            validate_state._check_schema_keywords(loaded)
        self.assertIn("maxLength", str(caught.exception))

    def test_shipped_example_is_valid(self) -> None:
        self.assertEqual([], validate_state.validate_document(validate_state.example_document(), schema()))


class DocumentRules(unittest.TestCase):
    def problems(self, document: dict) -> str:
        return " | ".join(p.render() for p in validate_state.validate_document(document, schema()))

    def test_valid_task_entry(self) -> None:
        self.assertEqual("", self.problems(with_task(copy.deepcopy(VALID_TASK))))

    def test_unknown_task_status_names_the_field(self) -> None:
        task = copy.deepcopy(VALID_TASK)
        task["status"] = "almost_done"
        report = self.problems(with_task(task))
        self.assertIn("/tasks/TASK-042/status", report)
        self.assertIn("almost_done", report)

    def test_stale_lock_is_not_a_schema_error(self) -> None:
        """MASTER_PLAN 9: breaking a stale lock is the router's call, not a schema failure."""
        document = broken(lock={"held_by": "run-1", "acquired_at": "2020-01-01T00:00:00Z", "ttl_seconds": 1})
        self.assertEqual("", self.problems(document))

    def test_missing_required_property(self) -> None:
        document = validate_state.example_document()
        del document["quota"]
        self.assertIn("missing required property 'quota'", self.problems(document))

    def test_unexpected_property_rejected(self) -> None:
        self.assertIn("unexpected property 'mood'", self.problems(broken(mood="cheerful")))

    def test_task_id_pattern_enforced(self) -> None:
        document = validate_state.example_document()
        document["tasks"] = {"TASK42": copy.deepcopy(VALID_TASK)}
        self.assertIn("does not match", self.problems(document))

    def test_attempt_cap_enforced(self) -> None:
        task = copy.deepcopy(VALID_TASK)
        task["attempts"] = 4
        self.assertIn("above the maximum of 3", self.problems(with_task(task)))

    def test_branch_pattern_enforced(self) -> None:
        task = copy.deepcopy(VALID_TASK)
        task["branch"] = "main"
        self.assertIn("does not match", self.problems(with_task(task)))

    def test_null_is_allowed_where_declared(self) -> None:
        task = copy.deepcopy(VALID_TASK)
        task.update(assigned_to=None, branch=None, started_at=None, pr=None)
        self.assertEqual("", self.problems(with_task(task)))

    def test_bad_timestamp_rejected(self) -> None:
        self.assertIn("does not match", self.problems(broken(updated_at="2026-09-08 10:32:11")))

    def test_bad_version_rejected(self) -> None:
        self.assertIn("does not match", self.problems(broken(schema_version="1.0")))

    def test_boolean_is_not_accepted_as_integer(self) -> None:
        """JSON distinguishes true from 1; Python's bool-is-an-int does not."""
        document = validate_state.example_document()
        document["lock"]["ttl_seconds"] = True
        self.assertIn("expected integer", self.problems(document))

    def test_every_problem_is_reported_not_just_the_first(self) -> None:
        document = broken(schema_version="1.0", updated_at="nope", updated_by="nobody")
        self.assertEqual(3, len(validate_state.validate_document(document, schema())))


class CommandLine(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = validate_state.main(argv)
        return code, out.getvalue(), err.getvalue()

    def write(self, directory: str, document) -> str:
        path = Path(directory) / "STATE.json"
        path.write_text(json.dumps(document) if not isinstance(document, str) else document, encoding="utf-8")
        return str(path)

    def test_valid_file_exits_0(self) -> None:
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, validate_state.example_document())
            code, _, err = self.run_main(["--file", path, "--schema", str(SCHEMA_PATH)])
            self.assertEqual(0, code, err)

    def test_invalid_file_exits_1(self) -> None:
        task = copy.deepcopy(VALID_TASK)
        task["status"] = "almost_done"
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, with_task(task))
            code, _, err = self.run_main(["--file", path, "--schema", str(SCHEMA_PATH)])
            self.assertEqual(1, code)
            self.assertIn("/tasks/TASK-042/status", err)

    def test_absent_file_exits_0(self) -> None:
        with TemporaryDirectory() as tmp:
            code, _, err = self.run_main(["--file", str(Path(tmp) / "nope.json"), "--schema", str(SCHEMA_PATH)])
            self.assertEqual(0, code)
            self.assertIn("absent", err)

    def test_malformed_json_exits_1(self) -> None:
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, "{ not json")
            code, _, err = self.run_main(["--file", path, "--schema", str(SCHEMA_PATH)])
            self.assertEqual(1, code)
            self.assertIn("invalid JSON", err)

    def test_missing_schema_exits_3(self) -> None:
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, validate_state.example_document())
            code, _, err = self.run_main(["--file", path, "--schema", str(Path(tmp) / "nope.json")])
            self.assertEqual(3, code)
            self.assertIn("cannot read schema", err)

    def test_example_flag_round_trips(self) -> None:
        """--example must emit something --file accepts, or it is a trap."""
        code, out, _ = self.run_main(["--example"])
        self.assertEqual(0, code)
        with TemporaryDirectory() as tmp:
            path = self.write(tmp, json.loads(out))
            code, _, err = self.run_main(["--file", path, "--schema", str(SCHEMA_PATH)])
            self.assertEqual(0, code, err)

    def test_repo_state_file_if_present(self) -> None:
        """If someone commits a context/STATE.json, it must be valid."""
        state = REPO_ROOT / "context" / "STATE.json"
        code, _, err = self.run_main(["--file", str(state), "--schema", str(SCHEMA_PATH)])
        self.assertEqual(0, code, err)


if __name__ == "__main__":
    unittest.main()
