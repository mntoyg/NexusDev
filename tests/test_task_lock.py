"""Tests for scripts/task_lock.py (TASK-004, implementing ADR-004).

Standard library only, and no network: every test builds a bare repository in a
temporary directory and uses it as `origin`, with two clones racing against it.

ADR-004 requires the experiment that produced it to live here, so the no-op push
hazard cannot regress unnoticed. Two tests carry it:

- `test_identical_sha_push_is_a_silent_no_op` asserts git's actual behaviour, so
  if a future git changes it we find out from a failure rather than from a
  duplicated task.
- `test_lock_object_is_unique_per_call` asserts the module never relies on that
  behaviour being safe.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import task_lock  # noqa: E402  (path shim must come first)

IDENTITY = ["-c", "user.email=t@x.invalid", "-c", "user.name=t"]


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


class LockFixture(unittest.TestCase):
    """A bare `origin` plus two clones, `a` and `b`."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.a, self.b = root / "a", root / "b"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.a)], check=True,
                       capture_output=True)
        run_git(self.a, *IDENTITY, "commit", "-q", "--allow-empty", "-m", "init")
        run_git(self.a, "push", "-q", "origin", "HEAD:refs/heads/main")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.b)], check=True,
                       capture_output=True)
        self._cwd = Path.cwd()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def cli(self, clone: Path, *argv: str) -> tuple[int, str, str]:
        os.chdir(clone)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = task_lock.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def lock_exists(self) -> bool:
        listed = run_git(self.origin, "show-ref", "--verify", "--quiet", task_lock.lock_ref("TASK-004"))
        return listed.returncode == 0


class Racing(LockFixture):
    def test_exactly_one_winner(self) -> None:
        first = self.cli(self.a, "claim", "TASK-004", "--run-id", "run-a")
        second = self.cli(self.b, "claim", "TASK-004", "--run-id", "run-b")
        self.assertEqual(task_lock.EXIT_OK, first[0], first[2])
        self.assertEqual(task_lock.EXIT_HELD, second[0], second[2])
        self.assertIn("already held", second[2])
        self.assertTrue(self.lock_exists())

    def test_identical_sha_push_is_a_silent_no_op(self) -> None:
        """Git's own behaviour, asserted so a change to it surfaces as a failure.

        This is why the lock object must be unique per run: pushing the same sha
        twice makes the second push succeed, and both runs would hold the lock.
        """
        ref = task_lock.lock_ref("TASK-004")
        lease = f"--force-with-lease={ref}:"
        first = run_git(self.a, "push", lease, "origin", f"HEAD:{ref}")
        self.assertEqual(0, first.returncode, first.stderr)
        run_git(self.b, "fetch", "-q", "origin")
        second = run_git(self.b, "push", lease, "origin", f"HEAD:{ref}")
        self.assertEqual(
            0, second.returncode,
            "git no longer treats an identical push as success; ADR-004's first hazard may be obsolete",
        )

    def test_lock_object_is_unique_per_call(self) -> None:
        os.chdir(self.a)
        first, problem_a = task_lock.build_lock_object("TASK-004", "same-run")
        second, problem_b = task_lock.build_lock_object("TASK-004", "same-run")
        self.assertIsNotNone(first, problem_a)
        self.assertIsNotNone(second, problem_b)
        self.assertNotEqual(first, second, "identical inputs produced the same sha, so a push would be a no-op")


class EmptyShaGuard(LockFixture):
    def test_empty_sha_never_reaches_git_push(self) -> None:
        """`git push ":$ref"` deletes the ref, so an empty sha would release someone else's lock."""
        self.cli(self.a, "claim", "TASK-004", "--run-id", "run-a")
        self.assertTrue(self.lock_exists())

        pushes: list[tuple[str, ...]] = []
        real_git = task_lock.git

        def spy(*args: str, **kwargs):
            if args and args[0] == "push":
                pushes.append(args)
            return real_git(*args, **kwargs)

        task_lock.git = spy
        task_lock.build_lock_object = lambda task_id, run_id: ("", "")
        try:
            code, _, err = self.cli(self.b, "claim", "TASK-004", "--run-id", "run-b")
        finally:
            task_lock.git = real_git
            import importlib
            importlib.reload(task_lock)

        self.assertEqual(3, code)
        self.assertIn("empty sha", err)
        self.assertEqual([], pushes, "a push was attempted with an empty object")
        self.assertTrue(self.lock_exists(), "the held lock was released")


class Lifecycle(LockFixture):
    def test_release_then_reclaim(self) -> None:
        self.assertEqual(task_lock.EXIT_OK, self.cli(self.a, "claim", "TASK-004", "--run-id", "run-a")[0])
        self.assertEqual(task_lock.EXIT_OK, self.cli(self.a, "release", "TASK-004")[0])
        self.assertFalse(self.lock_exists())
        code, _, err = self.cli(self.b, "claim", "TASK-004", "--run-id", "run-b")
        self.assertEqual(task_lock.EXIT_OK, code, err)

    def test_release_is_idempotent(self) -> None:
        code, _, err = self.cli(self.a, "release", "TASK-004")
        self.assertEqual(task_lock.EXIT_OK, code, err)
        self.assertIn("was not locked", err)

    def test_status_free_and_held(self) -> None:
        code, out, _ = self.cli(self.a, "status", "TASK-004")
        self.assertEqual(task_lock.EXIT_OK, code)
        self.assertIn("free", out)

        self.cli(self.a, "claim", "TASK-004", "--run-id", "run-a")
        code, out, err = self.cli(self.b, "status", "TASK-004")
        self.assertEqual(task_lock.EXIT_OK, code, err)
        self.assertIn("held", out)
        self.assertIn("run-a", out)
        self.assertIn("age=", out)


class Breaking(LockFixture):
    def test_young_lock_is_left_alone(self) -> None:
        self.cli(self.a, "claim", "TASK-004", "--run-id", "run-a")
        code, _, err = self.cli(self.b, "break", "TASK-004", "--older-than", "3600")
        self.assertEqual(task_lock.EXIT_HELD, code)
        self.assertIn("younger than", err)
        self.assertTrue(self.lock_exists())

    def test_old_enough_lock_is_broken(self) -> None:
        self.cli(self.a, "claim", "TASK-004", "--run-id", "run-a")
        code, _, err = self.cli(self.b, "break", "TASK-004", "--older-than", "0")
        self.assertEqual(task_lock.EXIT_OK, code, err)
        self.assertIn("broke a", err)
        self.assertFalse(self.lock_exists())

    def test_breaking_an_absent_lock_is_not_an_error(self) -> None:
        code, _, err = self.cli(self.a, "break", "TASK-004", "--older-than", "0")
        self.assertEqual(task_lock.EXIT_OK, code)
        self.assertIn("not locked", err)


class Arguments(LockFixture):
    def test_bad_task_id_refused(self) -> None:
        code, _, err = self.cli(self.a, "claim", "TASK4", "--run-id", "run-a")
        self.assertEqual(3, code)
        self.assertIn("is not a task id", err)

    def test_unreachable_remote_is_distinguished_from_held(self) -> None:
        code, _, err = self.cli(self.a, "claim", "TASK-004", "--run-id", "run-a", "--remote", "nowhere")
        self.assertEqual(3, code, f"expected a git error, got {code}: {err}")


if __name__ == "__main__":
    unittest.main()
