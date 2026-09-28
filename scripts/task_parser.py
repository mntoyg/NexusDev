#!/usr/bin/env python3
"""Turn the task blocks in ``context/TODO.md`` into structured JSON.

Owner: Node 6 (Antigravity). Authored by Node 1 during the Phase 1 bootstrap.

``context/TODO.md`` is the hand-off point between Cursor, which writes tasks,
and Aider, which executes them. Its grammar is therefore an API rather than a
formatting preference: ``MASTER_PLAN.md`` §3.2 defines it and this module is its
reference implementation — the only thing allowed to interpret it.

The parser is deliberately strict, and that strictness is the whole point. A
block that cannot be parsed is never skipped, because a silently skipped task is
indistinguishable from a task nobody wrote: Aider would execute the rest of the
file and the missing work would surface only much later, as a mystery. Every
problem is reported with a line number, and the process exits non-zero.

Usage::

    task_parser.py [--file PATH] [--task-id ID] [--status STATUS]
                   [--validate] [--compact]

Exit codes::

    0   success
    1   validation failed; every problem is reported on stderr
    2   --task-id named a task that does not exist
    3   the input file could not be read
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Iterator, NamedTuple

DEFAULT_TODO = "context/TODO.md"

# Vocabulary from MASTER_PLAN.md §3.2. Kept as tuples so the error messages can
# list the accepted values in their documented order.
STATUSES = ("ready", "in_progress", "blocked", "review", "done")
COMPLEXITIES = ("low", "medium", "high")
ROUTES = ("hermes", "claude", "any")
OWNERS = ("aider", "cursor", "claude", "opencode", "human")
REQUIRED_FIELDS = ("status", "complexity", "route", "files", "depends_on", "owner")
REQUIRED_SECTIONS = ("Goal", "Constraints", "Acceptance criteria")

# MASTER_PLAN.md §3.2: a task edits at most five files. The limit exists so a
# task stays reviewable by a human in one sitting; splitting is always allowed.
MAX_FILES = 5

HEADING_RE = re.compile(r"^###\s+\[(?P<id>TASK-\d+[a-z]?)\]\s+(?P<title>\S.*?)\s*$")
ANY_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
CLOSING_HEADING_RE = re.compile(r"^#{1,2}\s+\S")
FIELD_RE = re.compile(r"^-\s+\*\*(?P<key>[A-Za-z_]+):\*\*\s*(?P<value>.*?)\s*$")
SECTION_RE = re.compile(r"^\*\*(?P<name>[A-Za-z][A-Za-z ]*)\*\*\s*$")
CHECKBOX_RE = re.compile(r"^-\s+\[(?P<mark>[ xX])\]\s+(?P<text>\S.*?)\s*$")
BULLET_RE = re.compile(r"^-\s+(?P<text>\S.*?)\s*$")
TASK_ID_RE = re.compile(r"^TASK-\d+[a-z]?$")


class Problem(NamedTuple):
    """One validation failure, addressed to a human reading a diff."""

    line: int
    message: str

    def render(self, path: str) -> str:
        return f"{path}:{self.line}: {self.message}"


class _Block(NamedTuple):
    task_id: str
    title: str
    start: int
    lines: list[tuple[int, str]]


def _split_blocks(lines: list[str]) -> tuple[list[_Block], list[Problem]]:
    """Cut the file into task blocks, refusing content that belongs to none.

    Anything before the first task heading is treated as the file's own preamble
    and ignored. After that point every non-blank line must live inside a block,
    so a stray edit cannot quietly detach a field from its task.
    """
    blocks: list[_Block] = []
    problems: list[Problem] = []
    current: _Block | None = None
    seen_first = False

    for number, raw in enumerate(lines, 1):
        heading = HEADING_RE.match(raw)
        if heading:
            seen_first = True
            current = _Block(heading.group("id"), heading.group("title"), number, [])
            blocks.append(current)
            continue

        if CLOSING_HEADING_RE.match(raw) and seen_first:
            # A top- or second-level heading ends the task list: whatever follows
            # is a footer, and a field stranded under one would never be read.
            current = None
            continue

        if ANY_HEADING_RE.match(raw) and raw.startswith("### "):
            # A '### ' heading that is not a well-formed task heading is almost
            # always a typo in the id, which would otherwise vanish silently.
            problems.append(
                Problem(number, f"malformed task heading, expected '### [TASK-<n>] <title>': {raw.strip()!r}")
            )
            current = None
            seen_first = True
            continue

        if current is None:
            if seen_first and raw.strip():
                problems.append(Problem(number, f"content outside any task block: {raw.strip()[:60]!r}"))
            continue

        current.lines.append((number, raw))

    return blocks, problems


def _parse_fields(block: _Block, problems: list[Problem]) -> dict[str, tuple[int, str]]:
    """Read the ``- **key:** value`` header of a block, rejecting duplicates."""
    fields: dict[str, tuple[int, str]] = {}
    for number, raw in block.lines:
        if SECTION_RE.match(raw):
            break  # the prose sections start here
        match = FIELD_RE.match(raw)
        if not match:
            if raw.strip():
                problems.append(Problem(number, f"expected '- **key:** value', got {raw.strip()[:60]!r}"))
            continue
        key, value = match.group("key"), match.group("value")
        if key in fields:
            problems.append(Problem(number, f"duplicate field {key!r} (first seen on line {fields[key][0]})"))
            continue
        if key not in REQUIRED_FIELDS:
            problems.append(
                Problem(number, f"unknown field {key!r}; the grammar defines {', '.join(REQUIRED_FIELDS)}")
            )
            continue
        if "#" in value:
            # .cursorrules once showed inline comments in its example. They are
            # not part of the grammar, and a '#' in a path or id is never valid.
            problems.append(
                Problem(number, f"inline '#' comments are not part of the grammar (field {key!r}); see MASTER_PLAN.md 3.2")
            )
            continue
        fields[key] = (number, value)
    return fields


def _parse_sections(block: _Block, problems: list[Problem]) -> dict[str, list[tuple[int, str]]]:
    """Collect the ``**Goal**`` / ``**Constraints**`` / ``**Acceptance criteria**`` bodies."""
    sections: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    for number, raw in block.lines:
        match = SECTION_RE.match(raw)
        if match:
            name = match.group("name").strip()
            if name not in REQUIRED_SECTIONS:
                problems.append(
                    Problem(number, f"unknown section {name!r}; the grammar defines {', '.join(REQUIRED_SECTIONS)}")
                )
                current = None
                continue
            if name in sections:
                problems.append(Problem(number, f"duplicate section {name!r}"))
                current = None
                continue
            sections[name] = []
            current = name
            continue
        if current is not None and raw.strip():
            sections[current].append((number, raw.rstrip()))
    return sections


def _validate_files(value: str, line: int, problems: list[Problem]) -> list[str]:
    """Split and sanity-check the ``files:`` list.

    Rejecting absolute paths and ``..`` segments is a security control, not
    tidiness: SECURITY.md §3 treats a task escaping its declared file scope as a
    reportable vulnerability, and the router hands this list straight to Aider.
    """
    files = [part.strip() for part in value.split(",") if part.strip()]
    if not files:
        problems.append(Problem(line, "files: must name at least one file"))
        return files
    if len(files) > MAX_FILES:
        problems.append(
            Problem(line, f"files: {len(files)} paths exceeds the limit of {MAX_FILES}; split the task (MASTER_PLAN.md 3.2)")
        )
    for path in files:
        # Judge the string itself rather than asking pathlib, whose answer is
        # platform-dependent: Path('/etc/passwd').is_absolute() is False on
        # Windows, so a developer's local check would pass where CI fails.
        normalised = path.replace("\\", "/")
        if normalised.startswith("/") or re.match(r"^[A-Za-z]:", normalised):
            problems.append(Problem(line, f"files: {path!r} is absolute; paths are relative to the repository root"))
        elif ".." in PurePosixPath(normalised).parts:
            problems.append(Problem(line, f"files: {path!r} escapes the repository with '..'"))
    duplicates = {p for p in files if files.count(p) > 1}
    for path in sorted(duplicates):
        problems.append(Problem(line, f"files: {path!r} listed more than once"))
    return files


def _validate_enum(key: str, value: str, allowed: tuple[str, ...], line: int, problems: list[Problem]) -> str:
    if value not in allowed:
        problems.append(Problem(line, f"{key}: {value!r} is not one of {' | '.join(allowed)}"))
    return value


def _build_task(block: _Block, path: str, problems: list[Problem]) -> dict | None:
    fields = _parse_fields(block, problems)
    sections = _parse_sections(block, problems)

    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    for name in missing:
        problems.append(Problem(block.start, f"{block.task_id}: missing required field {name!r}"))
    for name in REQUIRED_SECTIONS:
        if name not in sections:
            problems.append(Problem(block.start, f"{block.task_id}: missing required section '**{name}**'"))
        elif not sections[name]:
            problems.append(Problem(block.start, f"{block.task_id}: section '**{name}**' is empty"))
    if missing:
        return None

    status_line, status = fields["status"]
    complexity_line, complexity = fields["complexity"]
    route_line, route = fields["route"]
    files_line, files_raw = fields["files"]
    depends_line, depends_raw = fields["depends_on"]
    owner_line, owner = fields["owner"]

    _validate_enum("status", status, STATUSES, status_line, problems)
    _validate_enum("complexity", complexity, COMPLEXITIES, complexity_line, problems)
    _validate_enum("route", route, ROUTES, route_line, problems)
    _validate_enum("owner", owner, OWNERS, owner_line, problems)
    files = _validate_files(files_raw, files_line, problems)

    depends_on: list[str] = []
    if depends_raw != "none":
        for ref in (part.strip() for part in depends_raw.split(",")):
            if not ref:
                continue
            if not TASK_ID_RE.match(ref):
                problems.append(Problem(depends_line, f"depends_on: {ref!r} is not a task id or 'none'"))
            elif ref == block.task_id:
                problems.append(Problem(depends_line, f"depends_on: {block.task_id} depends on itself"))
            else:
                depends_on.append(ref)

    criteria = []
    for number, raw in sections.get("Acceptance criteria", []):
        checkbox = CHECKBOX_RE.match(raw)
        if checkbox:
            criteria.append({"text": checkbox.group("text"), "checked": checkbox.group("mark").lower() == "x"})
        else:
            problems.append(Problem(number, f"{block.task_id}: acceptance criteria must be '- [ ] ...' checkboxes"))
    if "Acceptance criteria" in sections and not criteria:
        problems.append(Problem(block.start, f"{block.task_id}: needs at least one acceptance criterion"))

    constraints = []
    for number, raw in sections.get("Constraints", []):
        bullet = BULLET_RE.match(raw)
        if bullet:
            constraints.append(bullet.group("text"))
        else:
            problems.append(Problem(number, f"{block.task_id}: constraints must be '- ' bullets"))

    goal = " ".join(text.strip() for _, text in sections.get("Goal", []))

    return {
        "id": block.task_id,
        "title": block.title,
        "status": status,
        "complexity": complexity,
        "route": route,
        "files": files,
        "depends_on": depends_on,
        "owner": owner,
        "goal": goal,
        "constraints": constraints,
        "acceptance_criteria": criteria,
        "source": {"file": path, "line": block.start},
    }


def _check_graph(tasks: list[dict], problems: list[Problem]) -> None:
    """Reject duplicate ids, dangling dependencies and dependency cycles.

    A cycle would leave every task in it permanently unclaimable, which the
    router would report as "nothing ready" rather than as an error.
    """
    by_id: dict[str, dict] = {}
    for task in tasks:
        if task["id"] in by_id:
            problems.append(
                Problem(task["source"]["line"], f"duplicate task id {task['id']!r} (first seen on line {by_id[task['id']]['source']['line']})")
            )
            continue
        by_id[task["id"]] = task

    for task in by_id.values():
        for ref in task["depends_on"]:
            if ref not in by_id:
                problems.append(Problem(task["source"]["line"], f"{task['id']}: depends_on names unknown task {ref!r}"))

    WHITE, GREY, BLACK = 0, 1, 2
    colour = {task_id: WHITE for task_id in by_id}

    def walk(node: str, trail: list[str]) -> None:
        colour[node] = GREY
        for ref in by_id[node]["depends_on"]:
            if ref not in by_id:
                continue
            if colour[ref] == GREY:
                cycle = " -> ".join(trail[trail.index(ref):] + [ref]) if ref in trail else f"{node} -> {ref}"
                problems.append(Problem(by_id[node]["source"]["line"], f"dependency cycle: {cycle}"))
            elif colour[ref] == WHITE:
                walk(ref, trail + [ref])
        colour[node] = BLACK

    for task_id in by_id:
        if colour[task_id] == WHITE:
            walk(task_id, [task_id])


def parse_text(text: str, path: str = DEFAULT_TODO) -> tuple[list[dict], list[Problem]]:
    """Parse TODO.md content. Returns (tasks, problems); tasks is best-effort."""
    blocks, problems = _split_blocks(text.splitlines())
    tasks = [task for task in (_build_task(block, path, problems) for block in blocks) if task is not None]
    _check_graph(tasks, problems)
    problems.sort(key=lambda problem: problem.line)
    return tasks, problems


def parse_file(path: Path) -> tuple[list[dict], list[Problem]]:
    return parse_text(path.read_text(encoding="utf-8"), path.as_posix())


def _iter_selected(tasks: list[dict], status: str | None) -> Iterator[dict]:
    for task in tasks:
        if status is None or task["status"] == status:
            yield task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse context/TODO.md task blocks into JSON.")
    parser.add_argument("--file", default=DEFAULT_TODO, help=f"task file to read (default: {DEFAULT_TODO})")
    parser.add_argument("--task-id", help="emit only this task; exit 2 if it does not exist")
    parser.add_argument("--status", choices=STATUSES, help="emit only tasks in this status")
    parser.add_argument("--validate", action="store_true", help="report problems only, emit no JSON")
    parser.add_argument("--compact", action="store_true", help="emit single-line JSON")
    args = parser.parse_args(argv)

    path = Path(args.file)
    try:
        tasks, problems = parse_file(path)
    except OSError as error:
        print(f"task_parser: cannot read {path}: {error}", file=sys.stderr)
        return 3

    for problem in problems:
        print(problem.render(path.as_posix()), file=sys.stderr)
    if problems:
        print(f"task_parser: {len(problems)} problem(s) in {path.as_posix()}", file=sys.stderr)
        return 1

    if args.validate:
        print(f"task_parser: {len(tasks)} task(s) valid in {path.as_posix()}", file=sys.stderr)
        return 0

    if args.task_id:
        for task in tasks:
            if task["id"] == args.task_id:
                selected: list[dict] | dict = task
                break
        else:
            print(f"task_parser: no task {args.task_id!r} in {path.as_posix()}", file=sys.stderr)
            return 2
    else:
        selected = list(_iter_selected(tasks, args.status))

    indent = None if args.compact else 2
    print(json.dumps(selected, indent=indent, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
