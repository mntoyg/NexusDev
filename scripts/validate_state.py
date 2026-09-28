#!/usr/bin/env python3
"""Validate ``context/STATE.json`` against ``schemas/state.schema.json``.

Owner: Node 6 (Antigravity). Authored by Node 1 during the Phase 1 bootstrap.
Specified by TASK-001 in ``context/TODO.md``; the shape lives in
``MASTER_PLAN.md`` §3.4.

Why this interprets the schema instead of restating it
------------------------------------------------------
TASK-001 asked for a schema file *and* a hand-written validator. Writing the
rules twice would create two sources of truth for one contract, which is the
exact failure this project keeps running into: a document that says one thing
while the code enforces another. So the schema file is the only statement of the
rules and this module reads it.

That trade has one sharp edge, and it is handled deliberately: a validator that
silently ignores a keyword it does not implement is worse than no validator,
because the schema would appear to enforce something it does not. Every keyword
is therefore either implemented or a hard error — see ``SUPPORTED``.

Only the subset of JSON Schema that this project's contract needs is supported.
Adding a keyword to the schema without implementing it here fails loudly.

Usage::

    validate_state.py [--file PATH] [--schema PATH] [--example]

Exit codes::

    0   valid, or the file is absent (STATE.json is a rebuildable cache, so its
        absence is a normal state and not a failure)
    1   invalid: malformed JSON, or one or more rule violations on stderr
    3   the file exists but could not be read

Exit codes match ``scripts/task_parser.py`` so the router can treat both the
same way.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

DEFAULT_STATE = "context/STATE.json"
DEFAULT_SCHEMA = "schemas/state.schema.json"

# Keywords this validator enforces. Anything else in the schema is a hard error.
SUPPORTED = {
    "type", "required", "properties", "additionalProperties", "propertyNames",
    "enum", "pattern", "minimum", "maximum", "minLength", "items",
}
# Annotations carry no constraint and are safe to skip.
ANNOTATIONS = {"$schema", "$id", "title", "description", "$comment", "examples", "default"}

TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


class Problem(NamedTuple):
    """One rule violation, addressed by JSON pointer so it can be found fast."""

    pointer: str
    message: str

    def render(self) -> str:
        return f"{self.pointer or '/'}: {self.message}"


class SchemaError(Exception):
    """The schema itself is unusable. Never raised because of the data."""


def _check_schema_keywords(schema: dict, pointer: str = "") -> None:
    """Refuse a schema carrying rules this validator would not enforce."""
    for keyword, value in schema.items():
        if keyword in ANNOTATIONS:
            continue
        if keyword not in SUPPORTED:
            raise SchemaError(
                f"{pointer or '/'}: schema uses unsupported keyword {keyword!r}; "
                f"implement it in scripts/validate_state.py or remove it from the schema"
            )
        if keyword == "properties":
            for name, sub in value.items():
                _check_schema_keywords(sub, f"{pointer}/properties/{name}")
        elif keyword in ("additionalProperties", "propertyNames", "items") and isinstance(value, dict):
            _check_schema_keywords(value, f"{pointer}/{keyword}")


def _type_names(schema: dict) -> list[str]:
    declared = schema.get("type")
    if declared is None:
        return []
    return [declared] if isinstance(declared, str) else list(declared)


def _check_type(value: Any, names: list[str], pointer: str, problems: list[Problem]) -> bool:
    for name in names:
        expected = TYPE_MAP.get(name)
        if expected is None:
            raise SchemaError(f"{pointer or '/'}: unknown type {name!r} in schema")
        # bool is a subclass of int in Python; JSON treats them as distinct.
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, expected):
            return True
    problems.append(Problem(pointer, f"expected {' or '.join(names)}, got {type(value).__name__}"))
    return False


def _validate(value: Any, schema: dict, pointer: str, problems: list[Problem]) -> None:
    names = _type_names(schema)
    if names and not _check_type(value, names, pointer, problems):
        return

    # A null that the schema permits carries no further constraints: a pattern or
    # a minimum cannot meaningfully apply to it.
    if value is None and ("null" in names or None in schema.get("enum", [])):
        return

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join("null" if item is None else repr(item) for item in schema["enum"])
        problems.append(Problem(pointer, f"{value!r} is not one of: {allowed}"))

    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        problems.append(Problem(pointer, f"{value!r} does not match {schema['pattern']}"))

    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        problems.append(Problem(pointer, f"shorter than the minimum of {schema['minLength']} characters"))

    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(Problem(pointer, f"{value} is below the minimum of {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(Problem(pointer, f"{value} is above the maximum of {schema['maximum']}"))

    if isinstance(value, dict):
        _validate_object(value, schema, pointer, problems)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{pointer}/{index}", problems)


def _validate_object(value: dict, schema: dict, pointer: str, problems: list[Problem]) -> None:
    for name in schema.get("required", []):
        if name not in value:
            problems.append(Problem(pointer, f"missing required property {name!r}"))

    properties = schema.get("properties", {})
    extra = schema.get("additionalProperties", True)
    names = schema.get("propertyNames")

    for key, item in value.items():
        child = f"{pointer}/{key}"
        if names and "pattern" in names and not re.search(names["pattern"], key):
            problems.append(Problem(child, f"property name {key!r} does not match {names['pattern']}"))
        if key in properties:
            _validate(item, properties[key], child, problems)
        elif isinstance(extra, dict):
            _validate(item, extra, child, problems)
        elif extra is False:
            problems.append(Problem(child, f"unexpected property {key!r}"))


def load_schema(path: Path) -> dict:
    schema = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema must be a JSON object")
    _check_schema_keywords(schema)
    return schema


def validate_document(document: Any, schema: dict) -> list[Problem]:
    problems: list[Problem] = []
    _validate(document, schema, "", problems)
    problems.sort(key=lambda problem: problem.pointer)
    return problems


def example_document() -> dict:
    """A minimal valid state file.

    Lives here rather than in a fixture file so there is one example, which the
    test suite validates — an example that drifts out of spec is a trap for the
    next person to bootstrap STATE.json.
    """
    return {
        "schema_version": "1.0.0",
        "updated_at": "2026-01-01T00:00:00Z",
        "updated_by": "ai-router",
        "lock": {"held_by": None, "acquired_at": None, "ttl_seconds": 900},
        "quota": {
            "claude": {"state": "available", "resets_at": None},
            "hermes": {"state": "available", "endpoint_healthy": True},
        },
        "tasks": {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate context/STATE.json against its schema.")
    parser.add_argument("--file", default=DEFAULT_STATE, help=f"state file (default: {DEFAULT_STATE})")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, help=f"schema file (default: {DEFAULT_SCHEMA})")
    parser.add_argument("--example", action="store_true", help="print a minimal valid state file and exit")
    args = parser.parse_args(argv)

    if args.example:
        print(json.dumps(example_document(), indent=2))
        return 0

    schema_path, state_path = Path(args.schema), Path(args.file)
    try:
        schema = load_schema(schema_path)
    except OSError as error:
        print(f"validate_state: cannot read schema {schema_path}: {error}", file=sys.stderr)
        return 3
    except (json.JSONDecodeError, SchemaError) as error:
        print(f"validate_state: unusable schema: {error}", file=sys.stderr)
        return 3

    if not state_path.exists():
        # MASTER_PLAN.md §3.4: STATE.json is a cache that can be rebuilt from
        # TODO.md plus git history, so absence is a normal state.
        print(f"validate_state: {state_path.as_posix()} absent; nothing to validate", file=sys.stderr)
        return 0

    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"validate_state: cannot read {state_path}: {error}", file=sys.stderr)
        return 3

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"{state_path.as_posix()}:{error.lineno}: invalid JSON: {error.msg}", file=sys.stderr)
        return 1

    problems = validate_document(document, schema)
    for problem in problems:
        print(f"{state_path.as_posix()}{problem.render()}", file=sys.stderr)
    if problems:
        print(f"validate_state: {len(problems)} problem(s) in {state_path.as_posix()}", file=sys.stderr)
        return 1

    print(f"validate_state: {state_path.as_posix()} valid", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
