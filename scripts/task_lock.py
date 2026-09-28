#!/usr/bin/env python3
"""Claim, release and break task locks (TASK-004, implementing ADR-004).

Owner: Node 6 (Antigravity). Called by ``scripts/ai-router.sh``.

ADR-004 decided that a Git ref is the mutex and ``context/STATE.json`` is only
the ledger. A claim on task ``<id>`` is the ref ``refs/nexus/lock/<id>``, pushed
with ``--force-with-lease=<ref>:``. The empty expected value means *the ref must
not exist*, which is a compare-and-swap on absence — the thing a file can never
give, because two runners both reading ``held_by: null`` and both writing
themselves in is a race no amount of care removes.

Three things here are load-bearing rather than stylistic:

1. **The lock object is unique per run.** Two runs pushing the *same* sha both
   succeed, because the second push is a no-op that Git reports as success, and
   both then believe they hold the lock. This was verified by experiment before
   ADR-004 was written. Each claim therefore builds a fresh commit naming the
   run.

2. **An empty sha is never pushed.** ``git push "$sha:$ref"`` with an empty
   ``$sha`` is ``git push ":$ref"``, which *deletes* the ref. A failure to build
   the lock object would otherwise release another run's lock instead of failing.

3. **The committer identity is injected, never inherited.** ``git commit-tree``
   refuses to run without one, and a GitHub Actions runner has none configured.
   Depending on ambient config would mean the lock works locally and fails in CI.

The lock's age comes from its commit timestamp, since the ref has to be breakable
without consulting the ledger. That is an implementation detail consistent with
ADR-004, not a change to it.

Usage::

    task_lock.py claim   <task-id> --run-id <id> [--remote origin]
    task_lock.py release <task-id> [--remote origin]
    task_lock.py status  <task-id> [--remote origin]
    task_lock.py break   <task-id> --older-than <seconds> [--remote origin]

Exit codes::

    0   claimed, released, broken, or status reported
    1   the lock is held by another run, or is too young to break
    2   usage error, emitted by argparse itself
    3   git is unavailable, the remote is unreachable, or the lock object
        could not be built

Codes match ``scripts/task_parser.py`` and ``scripts/validate_state.py`` so the
router can treat all three the same way. ``release`` is idempotent and exits 0
on an absent lock, because it runs from a ``trap ... EXIT`` that may fire after a
claim that never succeeded.
"""

from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
import time
from typing import NamedTuple

LOCK_NAMESPACE = "refs/nexus/lock"
TASK_ID_RE = re.compile(r"^TASK-[0-9]+[a-z]?$")

# Injected so a claim never depends on the caller's git config. The .invalid TLD
# is reserved by RFC 2606 and can never be routed, so this is not a real address.
LOCK_IDENTITY = {
    "GIT_AUTHOR_NAME": "nexusdev-lock",
    "GIT_AUTHOR_EMAIL": "lock@nexusdev.invalid",
    "GIT_COMMITTER_NAME": "nexusdev-lock",
    "GIT_COMMITTER_EMAIL": "lock@nexusdev.invalid",
}

EXIT_OK, EXIT_HELD, EXIT_GIT = 0, 1, 3


class Git(NamedTuple):
    """A git invocation's outcome, kept whole so callers can tell why it failed."""

    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def git(*args: str, env_extra: dict[str, str] | None = None) -> Git:
    import os

    env = {**os.environ, **(env_extra or {})}
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, env=env, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return Git(3, "", f"git {' '.join(args)}: {error}")
    return Git(done.returncode, done.stdout.strip(), done.stderr.strip())


def lock_ref(task_id: str) -> str:
    return f"{LOCK_NAMESPACE}/{task_id}"


def remote_sha(remote: str, ref: str) -> tuple[str | None, Git]:
    """The sha the lock ref points at on the remote, or None when it is free."""
    result = git("ls-remote", "--exit-code", remote, ref)
    if result.code == 0 and result.out:
        return result.out.split()[0], result
    if result.code == 2:  # ls-remote's documented "no matching refs"
        return None, result
    return None, result


def build_lock_object(task_id: str, run_id: str) -> tuple[str | None, str]:
    """Create a commit unique to this run, to be the lock's value.

    Uniqueness is the whole point: an identical sha would make the second push a
    no-op that git calls success, and two runs would both hold the lock.
    """
    tree = git("rev-parse", "-q", "--verify", "HEAD^{tree}")
    if not tree.ok or not tree.out:
        return None, f"cannot resolve HEAD^{{tree}}: {tree.err or 'no HEAD'}"
    # The nonce is not decoration. commit-tree is deterministic, so the same
    # tree, parent, message and second would produce the same sha - and an
    # identical sha makes the push a no-op that git reports as success. Two runs
    # normally differ by run_id, but a retry reusing one would not, so the
    # uniqueness is made unconditional here rather than assumed of the caller.
    nonce = secrets.token_hex(8)
    message = (
        f"lock {task_id} by {run_id} "
        f"at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} nonce {nonce}"
    )
    commit = git("commit-tree", tree.out, "-p", "HEAD", "-m", message, env_extra=LOCK_IDENTITY)
    if not commit.ok or not commit.out:
        return None, f"cannot build the lock object: {commit.err or 'empty sha'}"
    return commit.out, ""


def fetch_lock(remote: str, task_id: str) -> tuple[str | None, str]:
    """Bring another run's lock commit local so its message and age can be read."""
    ref = lock_ref(task_id)
    local = f"refs/nexus/inspect/{task_id}"
    fetched = git("fetch", "--quiet", "--force", remote, f"+{ref}:{local}")
    if not fetched.ok:
        return None, fetched.err
    return local, ""


def lock_details(local_ref: str) -> tuple[str, int]:
    """Subject line and age in seconds of a fetched lock commit."""
    shown = git("log", "-1", "--format=%ct%n%s", local_ref)
    if not shown.ok or "\n" not in shown.out:
        return "(unreadable)", -1
    stamp, subject = shown.out.split("\n", 1)
    try:
        age = int(time.time()) - int(stamp)
    except ValueError:
        return subject, -1
    return subject, age


def cmd_claim(task_id: str, run_id: str, remote: str) -> int:
    sha, problem = build_lock_object(task_id, run_id)
    if sha is None:
        print(f"task_lock: {problem}", file=sys.stderr)
        return EXIT_GIT
    if not sha.strip():
        # Belt and braces. An empty left-hand side would delete the ref, which
        # means releasing whoever does hold the lock.
        print("task_lock: refusing to push an empty sha", file=sys.stderr)
        return EXIT_GIT

    ref = lock_ref(task_id)
    pushed = git("push", f"--force-with-lease={ref}:", remote, f"{sha}:{ref}")
    if pushed.ok:
        print(f"task_lock: claimed {task_id} as {run_id}", file=sys.stderr)
        return EXIT_OK

    # A rejected push means either someone holds it or the remote is unreachable.
    # Ask the remote which, rather than guessing from stderr wording.
    held, probe = remote_sha(remote, ref)
    if held:
        print(f"task_lock: {task_id} is already held", file=sys.stderr)
        return EXIT_HELD
    if not probe.ok and probe.code != 2:
        print(f"task_lock: remote unreachable: {probe.err or pushed.err}", file=sys.stderr)
        return EXIT_GIT
    print(f"task_lock: push rejected but no lock present: {pushed.err}", file=sys.stderr)
    return EXIT_GIT


def cmd_release(task_id: str, remote: str) -> int:
    ref = lock_ref(task_id)
    held, probe = remote_sha(remote, ref)
    if held is None:
        if not probe.ok and probe.code != 2:
            print(f"task_lock: remote unreachable: {probe.err}", file=sys.stderr)
            return EXIT_GIT
        print(f"task_lock: {task_id} was not locked", file=sys.stderr)
        return EXIT_OK  # idempotent: release runs from a trap
    deleted = git("push", "--quiet", remote, "--delete", ref)
    if not deleted.ok:
        print(f"task_lock: cannot release {task_id}: {deleted.err}", file=sys.stderr)
        return EXIT_GIT
    print(f"task_lock: released {task_id}", file=sys.stderr)
    return EXIT_OK


def cmd_status(task_id: str, remote: str) -> int:
    held, probe = remote_sha(remote, lock_ref(task_id))
    if held is None:
        if not probe.ok and probe.code != 2:
            print(f"task_lock: remote unreachable: {probe.err}", file=sys.stderr)
            return EXIT_GIT
        print(f"{task_id} free")
        return EXIT_OK
    local, problem = fetch_lock(remote, task_id)
    if local is None:
        print(f"{task_id} held {held[:12]} (details unavailable: {problem})")
        return EXIT_OK
    subject, age = lock_details(local)
    print(f"{task_id} held {held[:12]} age={age}s {subject}")
    return EXIT_OK


def cmd_break(task_id: str, older_than: int, remote: str) -> int:
    held, probe = remote_sha(remote, lock_ref(task_id))
    if held is None:
        if not probe.ok and probe.code != 2:
            print(f"task_lock: remote unreachable: {probe.err}", file=sys.stderr)
            return EXIT_GIT
        print(f"task_lock: {task_id} is not locked", file=sys.stderr)
        return EXIT_OK
    local, problem = fetch_lock(remote, task_id)
    if local is None:
        print(f"task_lock: cannot inspect the lock on {task_id}: {problem}", file=sys.stderr)
        return EXIT_GIT
    subject, age = lock_details(local)
    if age < 0:
        print(f"task_lock: cannot read the age of the lock on {task_id}", file=sys.stderr)
        return EXIT_GIT
    if age < older_than:
        print(f"task_lock: {task_id} lock is {age}s old, younger than {older_than}s; leaving it", file=sys.stderr)
        return EXIT_HELD
    deleted = git("push", "--quiet", remote, "--delete", lock_ref(task_id))
    if not deleted.ok:
        print(f"task_lock: cannot break {task_id}: {deleted.err}", file=sys.stderr)
        return EXIT_GIT
    print(f"task_lock: broke a {age}s old lock on {task_id} ({subject})", file=sys.stderr)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim, release and break NexusDev task locks (ADR-004).")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("claim", "take the lock for a task"),
        ("release", "give the lock back; idempotent"),
        ("status", "report who holds the lock and for how long"),
        ("break", "delete a lock older than a given age"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("task_id", help="task id, e.g. TASK-004")
        # Declared per subcommand so the documented usage works: a parent
        # option would have to precede the subcommand, which the docstring
        # does not say and nobody would guess.
        child.add_argument("--remote", default="origin", help="git remote holding the locks (default: origin)")
        if name == "claim":
            child.add_argument("--run-id", required=True, help="identifier unique to this run")
        if name == "break":
            child.add_argument("--older-than", type=int, required=True, metavar="SECONDS")

    args = parser.parse_args(argv)
    if not TASK_ID_RE.match(args.task_id):
        print(f"task_lock: {args.task_id!r} is not a task id", file=sys.stderr)
        return EXIT_GIT

    if args.command == "claim":
        return cmd_claim(args.task_id, args.run_id, args.remote)
    if args.command == "release":
        return cmd_release(args.task_id, args.remote)
    if args.command == "status":
        return cmd_status(args.task_id, args.remote)
    return cmd_break(args.task_id, args.older_than, args.remote)


if __name__ == "__main__":
    sys.exit(main())
