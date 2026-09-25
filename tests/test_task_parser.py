"""Tests for scripts/task_parser.py.

Written with unittest rather than bare pytest assertions so the suite runs with
`python -m unittest discover tests` and no third-party install at all, while
still being collected by pytest for anyone who prefers it. MASTER_PLAN.md §11
sets the default answer on new dependencies to "no", and a test runner is not
an exception worth making.

Most cases mutate one line of a known-good block, so a failure points at the one
rule under test instead of at the fixture.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import task_parser  # noqa: E402  (path shim must come first)

VALID = """# preamble, ignored

### [TASK-042] Add retry-with-backoff to the Comet emitter
- **status:** ready
- **complexity:** low
- **route:** hermes
- **files:** scripts/telemetry.py, tests/test_telemetry.py
- **depends_on:** none
- **owner:** aider

**Goal**
The telemetry emitter survives a transient Comet outage without losing events.

**Constraints**
- Use exponential backoff: 3 attempts, base 0.5s, jitter.

**Acceptance criteria**
- [ ] `pytest tests/test_telemetry.py` passes
"""


def swap(original: str, replacement: str, text: str = VALID) -> str:
    """Replace exactly one line of the fixture, failing loudly if it moved."""
    if text.count(original) != 1:
        raise AssertionError(f"fixture line not unique: {original!r}")
    return text.replace(original, replacement)


class ParseValidBlock(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks, self.problems = task_parser.parse_text(VALID)

    def test_no_problems(self) -> None:
        self.assertEqual([], [p.message for p in self.problems])

    def test_fields(self) -> None:
        task = self.tasks[0]
        self.assertEqual("TASK-042", task["id"])
        self.assertEqual("Add retry-with-backoff to the Comet emitter", task["title"])
        self.assertEqual("ready", task["status"])
        self.assertEqual(["scripts/telemetry.py", "tests/test_telemetry.py"], task["files"])
        self.assertEqual([], task["depends_on"])
        self.assertEqual("aider", task["owner"])

    def test_sections(self) -> None:
        task = self.tasks[0]
        self.assertIn("survives a transient Comet outage", task["goal"])
        self.assertEqual(1, len(task["constraints"]))
        self.assertEqual([{"text": "`pytest tests/test_telemetry.py` passes", "checked": False}],
                         task["acceptance_criteria"])

    def test_source_location(self) -> None:
        self.assertEqual(3, self.tasks[0]["source"]["line"])


class FieldRules(unittest.TestCase):
    def assertProblem(self, text: str, needle: str) -> None:
        _, problems = task_parser.parse_text(text)
        joined = " | ".join(p.message for p in problems)
        self.assertIn(needle, joined, f"expected {needle!r} among: {joined}")

    def test_missing_field(self) -> None:
        self.assertProblem(VALID.replace("- **owner:** aider\n", ""), "missing required field 'owner'")

    def test_unknown_field(self) -> None:
        self.assertProblem(swap("- **owner:** aider", "- **owner:** aider\n- **priority:** high"), "unknown field 'priority'")

    def test_duplicate_field(self) -> None:
        self.assertProblem(swap("- **owner:** aider", "- **owner:** aider\n- **owner:** cursor"), "duplicate field 'owner'")

    def test_bad_status(self) -> None:
        self.assertProblem(swap("- **status:** ready", "- **status:** almost"), "'almost' is not one of")

    def test_review_status_is_valid(self) -> None:
        # MASTER_PLAN.md 3.2 lists five statuses; .cursorrules once listed four.
        _, problems = task_parser.parse_text(swap("- **status:** ready", "- **status:** review"))
        self.assertEqual([], [p.message for p in problems])

    def test_opencode_owner_is_valid(self) -> None:
        _, problems = task_parser.parse_text(swap("- **owner:** aider", "- **owner:** opencode"))
        self.assertEqual([], [p.message for p in problems])

    def test_inline_comment_rejected_with_a_useful_message(self) -> None:
        self.assertProblem(swap("- **status:** ready", "- **status:** ready   # ready | done"), "inline '#' comments")


class FileScopeRules(unittest.TestCase):
    """files: is handed to Aider as its edit scope, so it is a security boundary."""

    def problems_for(self, files: str) -> str:
        _, problems = task_parser.parse_text(swap("- **files:** scripts/telemetry.py, tests/test_telemetry.py", f"- **files:** {files}"))
        return " | ".join(p.message for p in problems)

    def test_limit_of_five(self) -> None:
        self.assertIn("exceeds the limit of 5", self.problems_for("a.py, b.py, c.py, d.py, e.py, f.py"))

    def test_five_is_allowed(self) -> None:
        self.assertEqual("", self.problems_for("a.py, b.py, c.py, d.py, e.py"))

    def test_parent_escape_rejected(self) -> None:
        self.assertIn("escapes the repository", self.problems_for("../../etc/passwd"))

    def test_posix_absolute_rejected(self) -> None:
        self.assertIn("is absolute", self.problems_for("/etc/passwd"))

    def test_windows_absolute_rejected(self) -> None:
        self.assertIn("is absolute", self.problems_for("C:/Windows/system32/drivers/etc/hosts"))

    def test_duplicate_path_rejected(self) -> None:
        self.assertIn("listed more than once", self.problems_for("a.py, a.py"))

    def test_empty_list_rejected(self) -> None:
        self.assertIn("must name at least one file", self.problems_for(""))


class SectionRules(unittest.TestCase):
    def assertProblem(self, text: str, needle: str) -> None:
        _, problems = task_parser.parse_text(text)
        self.assertIn(needle, " | ".join(p.message for p in problems))

    def test_missing_goal(self) -> None:
        text = VALID.replace("**Goal**\nThe telemetry emitter survives a transient Comet outage without losing events.\n", "")
        self.assertProblem(text, "missing required section '**Goal**'")

    def test_unknown_section(self) -> None:
        self.assertProblem(swap("**Constraints**", "**Notes**"), "unknown section 'Notes'")

    def test_acceptance_criteria_must_be_checkboxes(self) -> None:
        self.assertProblem(swap("- [ ] `pytest tests/test_telemetry.py` passes", "- just make it work"),
                           "must be '- [ ] ...' checkboxes")

    def test_checked_criterion_is_recorded(self) -> None:
        tasks, problems = task_parser.parse_text(swap("- [ ] `pytest tests/test_telemetry.py` passes", "- [x] done already"))
        self.assertEqual([], [p.message for p in problems])
        self.assertTrue(tasks[0]["acceptance_criteria"][0]["checked"])


class GraphRules(unittest.TestCase):
    def messages(self, text: str) -> str:
        _, problems = task_parser.parse_text(text)
        return " | ".join(p.message for p in problems)

    def two_blocks(self, first_depends: str, second_depends: str) -> str:
        block = VALID.split("### ", 1)[1]
        first = "### " + block.replace("- **depends_on:** none", f"- **depends_on:** {first_depends}")
        second = first.replace("TASK-042", "TASK-043").replace(f"- **depends_on:** {first_depends}", f"- **depends_on:** {second_depends}")
        return first + "\n" + second

    def test_self_dependency(self) -> None:
        self.assertIn("depends on itself", self.messages(swap("- **depends_on:** none", "- **depends_on:** TASK-042")))

    def test_unknown_dependency(self) -> None:
        self.assertIn("unknown task 'TASK-999'", self.messages(swap("- **depends_on:** none", "- **depends_on:** TASK-999")))

    def test_malformed_dependency(self) -> None:
        self.assertIn("is not a task id or 'none'", self.messages(swap("- **depends_on:** none", "- **depends_on:** the other one")))

    def test_duplicate_task_id(self) -> None:
        block = "### " + VALID.split("### ", 1)[1]
        self.assertIn("duplicate task id", self.messages(block + "\n" + block))

    def test_dependency_cycle(self) -> None:
        self.assertIn("dependency cycle", self.messages(self.two_blocks("TASK-043", "TASK-042")))

    def test_valid_dependency_chain(self) -> None:
        self.assertEqual("", self.messages(self.two_blocks("none", "TASK-042")))


class StructureRules(unittest.TestCase):
    def messages(self, text: str) -> str:
        _, problems = task_parser.parse_text(text)
        return " | ".join(p.message for p in problems)

    def test_footer_section_after_the_last_block_is_rejected(self) -> None:
        self.assertIn("content outside any task block", self.messages(VALID + "\n## Notes\n\nA stray footer note.\n"))

    def test_trailing_prose_stays_inside_the_last_block(self) -> None:
        # Without a heading there is no block boundary, so this line is still
        # part of the last section and fails as a malformed acceptance
        # criterion rather than as stray content. Documented so the difference
        # is a decision, not a surprise.
        self.assertIn("checkboxes", self.messages(VALID + "\nA stray footer note.\n"))

    def test_malformed_heading_is_rejected(self) -> None:
        self.assertIn("malformed task heading", self.messages(swap("### [TASK-042] Add retry-with-backoff to the Comet emitter",
                                                                   "### [TASK42] Add retry-with-backoff to the Comet emitter")))

    def test_preamble_is_ignored(self) -> None:
        self.assertEqual("", self.messages("Some intro.\n\n## A heading\n\n" + VALID.split("# preamble, ignored\n", 1)[1]))


class DocumentedExamples(unittest.TestCase):
    """The examples in the docs must parse.

    This suite exists because they did not. `.cursorrules` told Cursor to "emit
    EXACTLY this shape" and the shape carried inline `#` comments that
    MASTER_PLAN's grammar never allowed, so an agent following its own
    instruction file would have produced a block the parser rejects. Extracting
    the examples and running them through the parser makes that drift a test
    failure instead of a surprise at the first real hand-off.
    """

    def assertExampleParses(self, block: str, source: str) -> None:
        tasks, problems = task_parser.parse_text(block, source)
        self.assertEqual([], [p.render(source) for p in problems])
        self.assertEqual(1, len(tasks), f"expected exactly one example task in {source}")

    def test_master_plan_example(self) -> None:
        text = (REPO_ROOT / "context" / "MASTER_PLAN.md").read_text(encoding="utf-8")
        marker = "```markdown"
        start = text.index(marker) + len(marker)
        block = text[start:text.index("```", start)]
        self.assertIn("### [TASK-", block, "MASTER_PLAN 3.2 example moved")
        self.assertExampleParses(block, "context/MASTER_PLAN.md")

    def test_cursorrules_example(self) -> None:
        import textwrap

        text = (REPO_ROOT / ".cursorrules").read_text(encoding="utf-8")
        start = text.index("Emit EXACTLY this shape")
        heading = text.index("### [TASK-", start)
        # Start at the beginning of that line: dedent needs the leading spaces
        # on the first line too, or the common prefix is empty and nothing moves.
        block_start = text.rindex("\n", 0, heading) + 1
        block = textwrap.dedent(text[block_start:text.index("Accepted values", start)])
        self.assertIn("### [TASK-", block, ".cursorrules example moved")
        self.assertExampleParses(block, ".cursorrules")

    def test_examples_carry_no_inline_comments(self) -> None:
        for name in (".cursorrules", "context/MASTER_PLAN.md"):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("- **") and ":**" in line:
                    self.assertNotIn("#", line, f"{name}: inline comment in a documented task field")


class CommandLine(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = task_parser.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_repo_todo_validates(self) -> None:
        """The checked-in TODO.md must satisfy the parser that reads it."""
        code, _, err = self.run_main(["--file", str(REPO_ROOT / "context" / "TODO.md"), "--validate"])
        self.assertEqual(0, code, err)

    def test_unknown_task_id_exits_2(self) -> None:
        code, _, err = self.run_main(["--file", str(REPO_ROOT / "context" / "TODO.md"), "--task-id", "TASK-999"])
        self.assertEqual(2, code)
        self.assertIn("no task 'TASK-999'", err)

    def test_unreadable_file_exits_3(self) -> None:
        code, _, err = self.run_main(["--file", str(REPO_ROOT / "context" / "does-not-exist.md")])
        self.assertEqual(3, code)
        self.assertIn("cannot read", err)

    def test_status_filter(self) -> None:
        import json

        code, out, err = self.run_main(["--file", str(REPO_ROOT / "context" / "TODO.md"), "--status", "blocked"])
        self.assertEqual(0, code, err)
        tasks = json.loads(out)
        self.assertTrue(tasks, "expected at least one blocked task in the seeded TODO.md")
        self.assertTrue(all(task["status"] == "blocked" for task in tasks))

    def test_compact_output_is_one_line(self) -> None:
        code, out, _ = self.run_main(["--file", str(REPO_ROOT / "context" / "TODO.md"), "--compact"])
        self.assertEqual(0, code)
        self.assertEqual(1, len(out.strip().splitlines()))


if __name__ == "__main__":
    unittest.main()
