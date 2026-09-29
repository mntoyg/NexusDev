#!/usr/bin/env python3
"""Record pipeline state in ``context/STATE.json`` (MASTER_PLAN.md §6.1 step 7).

Owner: Node 6 (Antigravity). Called by ``scripts/ai-router.sh``.

ADR-004 settled what this file is: the **ledger**, never the mutex. The git ref is
the mutex, and the ref wins any disagreement. So nothing here is allowed to gate a
claim — a failure to write the ledger must never make a held lock look free, and a
ledger that disagrees with the refs is a stale cache rather than a crisis.

Two properties matter more than the feature set:

1. **The write is validated against the schema before it is kept.** A writer that
   can produce a document its own validator rejects would poison the cache for
   every reader. The new document is checked in memory and the file is only
   replaced if it passes.
2. **The replace is atomic.** Write to a temporary file in the same directory and
   ``os.replace`` it, so a reader never sees a half-written document and a crash
   mid-write leaves the previous version intact.

``STATE.json`` is a rebuildable cache (§3.4), so an absent file is created rather
than treated as an error.

Usage::

    state_ledger.py claim   <task-id> --run-id <id> --branch <ref> --route <r> [--ttl 900]
    state_ledger.py finish  <task-id> --status <status> [--pr N]
    state_ledger.py unlock
    state_ledger.py show

Exit codes::

    0   written, or shown
    1   the resulting document would be invalid, so nothing was written
    3   the file or the schema could not be read or written
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = REPO_ROOT / "context" / "STATE.json"
DEFAULT_SCHEMA = REPO_ROOT / "schemas" / "state.schema.json"
SCHEMA_VERSION = "1.0.0"
DEFAULT_TTL = 900

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_state  # noqa: E402  (path shim must come first)

EXIT_OK, EXIT_INVALID, EXIT_IO = 0, 1, 3


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def empty_document() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now(),
        "updated_by": "ai-router",
        "lock": {"held_by": None, "acquired_at": None, "ttl_seconds": DEFAULT_TTL},
        "quota": {
            "claude": {"state": "unknown", "resets_at": None},
            "hermes": {"state": "unknown", "endpoint_healthy": False},
        },
        "tasks": {},
    }


def load(path: Path) -> dict:
    if not path.exists():
        return empty_document()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # A corrupt ledger is recoverable by definition: 3.4 says the file can be
        # rebuilt from TODO.md plus git history. Starting fresh beats refusing to
        # route because a cache went bad.
        print(f"state_ledger: {path.name} unreadable ({error}); starting a fresh document", file=sys.stderr)
        return empty_document()
    if not isinstance(document, dict):
        print(f"state_ledger: {path.name} is not an object; starting a fresh document", file=sys.stderr)
        return empty_document()
    return document


def save(document: dict, path: Path, schema_path: Path) -> int:
    """Validate, then replace atomically. Never keep a document we would reject."""
    try:
        schema = validate_state.load_schema(schema_path)
    except (OSError, json.JSONDecodeError, validate_state.SchemaError) as error:
        print(f"state_ledger: unusable schema: {error}", file=sys.stderr)
        return EXIT_IO

    problems = validate_state.validate_document(document, schema)
    if problems:
        for problem in problems:
            print(f"state_ledger: would have written an invalid document: {problem.render()}", file=sys.stderr)
        return EXIT_INVALID

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", suffix=".tmp", delete=False)
        with handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(handle.name, path)
    except OSError as error:
        print(f"state_ledger: cannot write {path}: {error}", file=sys.stderr)
        return EXIT_IO
    return EXIT_OK


def cmd_claim(args) -> int:
    document = load(args.file)
    document["updated_at"] = now()
    document["updated_by"] = "ai-router"
    document["lock"] = {
        "held_by": args.run_id,
        "acquired_at": now(),
        "ttl_seconds": args.ttl,
    }
    entry = document.setdefault("tasks", {}).get(args.task_id, {})
    document["tasks"][args.task_id] = {
        "status": "in_progress",
        "route": args.route,
        "assigned_to": args.assigned_to,
        "branch": args.branch,
        "started_at": now(),
        # Attempts accumulate across runs: 4 records that three attempts force a
        # task to blocked, so the count has to survive a re-route.
        "attempts": min(int(entry.get("attempts", 0)) + 1, 3),
        "pr": entry.get("pr"),
    }
    return save(document, args.file, args.schema)


def cmd_finish(args) -> int:
    document = load(args.file)
    entry = document.setdefault("tasks", {}).get(args.task_id)
    if entry is None:
        print(f"state_ledger: no record of {args.task_id} to finish", file=sys.stderr)
        return EXIT_OK  # nothing to do is not a failure
    entry["status"] = args.status
    if args.pr is not None:
        entry["pr"] = args.pr
    document["updated_at"] = now()
    document["updated_by"] = "ai-router"
    return save(document, args.file, args.schema)


def cmd_unlock(args) -> int:
    document = load(args.file)
    document["lock"] = {"held_by": None, "acquired_at": None,
                        "ttl_seconds": document.get("lock", {}).get("ttl_seconds", DEFAULT_TTL)}
    document["updated_at"] = now()
    document["updated_by"] = "ai-router"
    return save(document, args.file, args.schema)


def cmd_show(args) -> int:
    if not args.file.exists():
        print("{}")
        return EXIT_OK
    print(json.dumps(load(args.file), indent=2, ensure_ascii=False))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record pipeline state in context/STATE.json (the ledger, never the mutex).")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(child):
        child.add_argument("--file", type=Path, default=DEFAULT_STATE)
        child.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)

    claim = sub.add_parser("claim", help="record that a task is in progress")
    claim.add_argument("task_id")
    claim.add_argument("--run-id", required=True)
    claim.add_argument("--branch", required=True)
    claim.add_argument("--route", required=True, choices=["hermes", "claude", "any"])
    claim.add_argument("--assigned-to", default="aider",
                       choices=["aider", "cursor", "claude", "opencode", "human"])
    claim.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    common(claim)

    finish = sub.add_parser("finish", help="record a task's outcome")
    finish.add_argument("task_id")
    finish.add_argument("--status", required=True,
                        choices=["ready", "in_progress", "blocked", "review", "done"])
    finish.add_argument("--pr", type=int)
    common(finish)

    unlock = sub.add_parser("unlock", help="clear the ledger's lock fields")
    common(unlock)

    show = sub.add_parser("show", help="print the ledger")
    common(show)

    args = parser.parse_args(argv)
    return {"claim": cmd_claim, "finish": cmd_finish, "unlock": cmd_unlock, "show": cmd_show}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
