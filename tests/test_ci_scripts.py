"""Tests for scripts/ci/*.sh — the shell that CI runs (P1 #3).

Until now the bash that drives every gate lived inside `.github/workflows/*.yml`,
where `shellcheck -S warning scripts/*.sh` could not see it and no test could run
it. Roughly a hundred lines, including the gitleaks self-test and the lock sweep,
which are the two gates the rest of the repository trusts.

Moving that shell into files made it lintable. These tests are the other half of
the win: the gate scripts can now be *driven*, on a laptop, with no runner.

Two of them matter more than the rest:

  * `LintShell.test_a_script_in_a_new_directory_is_still_linted` — the reason the
    linter asks git for the file list instead of expanding a glob. A glob is a
    guess about where shell lives; `git ls-files` is the answer.
  * `Workflows.test_no_workflow_hides_multi_line_shell` — the same assertion the
    CI gate makes, repeated here so the convention breaks on a laptop rather
    than in a pull request.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_DIR = REPO_ROOT / "scripts" / "ci"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
BASH = shutil.which("bash")

# A block scalar (`|`, `|-`, `>`) after `run:` is shell inside YAML. This is the
# same expression scripts/ci/lint-shell.sh greps for, kept in step with it by
# test_the_guard_pattern_matches_the_one_the_linter_uses below.
BLOCK_SCALAR_RUN = re.compile(r"^[ \t]*run:[ \t]*[|>]", re.MULTILINE)


def workflows() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def ci_scripts() -> list[Path]:
    return sorted(CI_DIR.glob("*.sh"))


@unittest.skipIf(BASH is None, "bash is not available on this machine")
class Inventory(unittest.TestCase):
    def test_there_is_shell_to_test(self) -> None:
        """A guard on the tests themselves: an empty directory would pass everything."""
        self.assertTrue(ci_scripts(), "scripts/ci/ holds no shell scripts")
        self.assertTrue(workflows(), "no workflows found to check")

    def test_every_ci_script_parses(self) -> None:
        for script in ci_scripts():
            with self.subTest(script=script.name):
                done = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(0, done.returncode, done.stderr)

    def test_every_ci_script_is_tracked_by_git(self) -> None:
        """An untracked script is invisible to `git ls-files`, so the linter would skip it."""
        tracked = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "scripts/ci"],
                                 capture_output=True, text=True, check=True).stdout.split()
        for script in ci_scripts():
            with self.subTest(script=script.name):
                self.assertIn(f"scripts/ci/{script.name}", tracked)


class Workflows(unittest.TestCase):
    def test_no_comment_accidentally_becomes_a_shellcheck_directive(self) -> None:
        """A line beginning `# shellcheck` is a directive, not prose.

        shellcheck rejects the whole file with SC1072 and points at the comment
        rather than at the mistake. It is not installed on every contributor's
        machine, so the rule is asserted here too. This caught lint-shell.sh
        describing itself in its own header, on the gate's first real run.
        """
        allowed = ("disable=", "shell=", "source=", "source-path=", "external-sources=")
        offenders = []
        for script in sorted(REPO_ROOT.glob("scripts/**/*.sh")):
            for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
                match = re.match(r"^\s*#\s*shellcheck\s+(\S*)", line)
                if match and not match.group(1).startswith(allowed):
                    offenders.append(f"{script.relative_to(REPO_ROOT).as_posix()}:{number}")
        self.assertEqual([], offenders,
                         f"prose read as a shellcheck directive: {offenders}")

    def test_no_workflow_hides_multi_line_shell(self) -> None:
        """The convention P1 #3 established, asserted where it is cheap to notice."""
        offenders = []
        for path in workflows():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if BLOCK_SCALAR_RUN.match(line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(
            [], offenders,
            "multi-line shell in a workflow is unlinted; move the body into "
            f"scripts/ci/ and call it from run:. Offenders: {offenders}",
        )

    def test_the_guard_pattern_matches_the_one_the_linter_uses(self) -> None:
        """Two copies of a rule drift. This fails the moment they disagree."""
        source = (CI_DIR / "lint-shell.sh").read_text(encoding="utf-8")
        self.assertIn(r"'^[[:space:]]*(-[[:space:]]+)?run:[[:space:]]*[|>]'", source)

    def test_every_script_a_workflow_calls_exists(self) -> None:
        referenced = set()
        for path in workflows():
            referenced.update(re.findall(r"scripts/ci/[A-Za-z0-9_.-]+\.sh",
                                         path.read_text(encoding="utf-8")))
        self.assertTrue(referenced, "no workflow calls any script in scripts/ci/")
        for reference in sorted(referenced):
            with self.subTest(reference=reference):
                self.assertTrue((REPO_ROOT / reference).exists(), f"{reference} does not exist")

    def test_no_ci_script_is_dead_code(self) -> None:
        """The other direction: a gate script nothing calls is a gate nothing runs."""
        text = "\n".join(path.read_text(encoding="utf-8") for path in workflows())
        for script in ci_scripts():
            with self.subTest(script=script.name):
                self.assertIn(f"scripts/ci/{script.name}", text,
                              f"{script.name} is called by no workflow")


@unittest.skipIf(BASH is None, "bash is not available on this machine")
class ShellFixture(unittest.TestCase):
    """A throwaway git repository holding the CI scripts, so they run for real."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "work"
        self.work.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)], check=True)

        (self.work / "scripts" / "ci").mkdir(parents=True)
        for script in ci_scripts():
            shutil.copy(script, self.work / "scripts" / "ci" / script.name)
        self.workflows = self.work / ".github" / "workflows"
        self.workflows.mkdir(parents=True)
        (self.workflows / "fine.yml").write_text(
            "jobs:\n  j:\n    steps:\n      - run: bash scripts/ci/lint-shell.sh\n",
            encoding="utf-8")
        self.commit()

    def commit(self) -> None:
        subprocess.run(["git", "-C", str(self.work), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.work), "-c", "user.email=t@x.invalid",
                        "-c", "user.name=t", "commit", "-qm", "fixture", "--allow-empty"],
                       check=True, capture_output=True)

    def stub_shellcheck(self, exit_code: int = 0) -> Path:
        """A stub on PATH, so the linter's own logic is testable without shellcheck.

        It records every path it was handed, which is what lets a test assert
        that discovery really did reach a given file.
        """
        bin_dir = Path(self._tmp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        self.seen = bin_dir / "seen.txt"
        stub = bin_dir / "shellcheck"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "--version" ]; then echo "stub shellcheck"; exit 0; fi\n'
            f'for arg in "$@"; do echo "$arg" >> "{self.seen.as_posix()}"; done\n'
            f"exit {exit_code}\n",
            encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return bin_dir

    def run_script(self, name: str, *argv: str, path_prefix: Path | None = None,
                   **env_extra: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "NEXUS_PYTHON": sys.executable}
        if path_prefix is not None:
            env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
        env.update(env_extra)
        return subprocess.run([BASH, f"scripts/ci/{name}", *argv], cwd=self.work,
                              capture_output=True, text=True, env=env, timeout=120)


class LintShell(ShellFixture):
    def test_a_clean_repository_passes(self) -> None:
        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck())
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("no multi-line shell", done.stdout)

    def test_a_block_scalar_in_a_workflow_fails_the_gate(self) -> None:
        (self.workflows / "bad.yml").write_text(
            "jobs:\n  j:\n    steps:\n      - run: |\n          echo hello\n", encoding="utf-8")
        self.commit()
        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck())
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("bad.yml", done.stdout)
        self.assertIn("Move the body into scripts/ci/", done.stderr)

    def test_a_folded_scalar_is_caught_too(self) -> None:
        """`>` folds newlines into spaces, which changes shell meaning as well as hiding it."""
        (self.workflows / "folded.yml").write_text(
            "jobs:\n  j:\n    steps:\n      - run: >\n          echo hello\n", encoding="utf-8")
        self.commit()
        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck())
        self.assertEqual(1, done.returncode, done.stdout)

    def test_a_one_line_run_is_allowed(self) -> None:
        (self.workflows / "oneline.yml").write_text(
            "jobs:\n  j:\n    steps:\n      - run: python3 --version\n", encoding="utf-8")
        self.commit()
        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck())
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def test_a_script_in_a_new_directory_is_still_linted(self) -> None:
        """Why discovery asks git instead of expanding scripts/*.sh.

        A glob is a guess about where shell lives. Put a script somewhere the old
        glob never looked and it must still be handed to shellcheck.
        """
        hidden = self.work / "tools" / "deploy" / "release.sh"
        hidden.parent.mkdir(parents=True)
        hidden.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        self.commit()

        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck())
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        handed = self.seen.read_text(encoding="utf-8").split()
        self.assertIn("tools/deploy/release.sh", handed)

    def test_an_untracked_script_is_reported_as_unlinted(self) -> None:
        """Not a silent pass: git cannot see it, so the operator must be told."""
        (self.work / "scripts" / "ci" / "stray.sh").write_text("#!/usr/bin/env bash\n:\n",
                                                               encoding="utf-8")
        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck())
        handed = self.seen.read_text(encoding="utf-8").split()
        self.assertNotIn("scripts/ci/stray.sh", handed)
        self.assertEqual(0, done.returncode,
                         "an untracked file must not be a hard failure, only an unlinted one")

    def test_shellcheck_findings_fail_the_gate(self) -> None:
        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck(exit_code=1))
        self.assertEqual(1, done.returncode)

    def test_a_repository_where_git_sees_no_shell_is_a_failure_not_a_pass(self) -> None:
        """Discovery returning nothing means the gate broke, not that the repo is clean."""
        # No commit afterwards: `git ls-files` reads the index, and re-adding
        # would put every script straight back.
        subprocess.run(["git", "-C", str(self.work), "rm", "-q", "--cached", "-r", "scripts"],
                       check=True, capture_output=True)
        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck())
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("gone blind", done.stderr)

    def test_a_missing_workflow_directory_is_a_failure_not_a_pass(self) -> None:
        done = self.run_script("lint-shell.sh", "nowhere/at/all",
                               path_prefix=self.stub_shellcheck())
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("checked nothing", done.stderr)

    def test_a_missing_shellcheck_is_exit_three_not_a_pass(self) -> None:
        """The gate says "I could not run" rather than "nothing to report".

        Driven through SHELLCHECK rather than by emptying PATH, because an empty
        PATH also removes git and the script would then fail for the wrong
        reason — which is how this test first passed while proving nothing.
        """
        done = self.run_script("lint-shell.sh", path_prefix=self.stub_shellcheck(),
                               SHELLCHECK="shellcheck-that-is-not-installed")
        self.assertEqual(3, done.returncode, done.stdout + done.stderr)
        self.assertIn("shellcheck is not installed", done.stderr)

class SelftestValidators(ShellFixture):
    def setUp(self) -> None:
        super().setUp()
        for name in ("task_parser.py", "validate_state.py"):
            shutil.copy(REPO_ROOT / "scripts" / name, self.work / "scripts" / name)
        (self.work / "schemas").mkdir(exist_ok=True)
        shutil.copy(REPO_ROOT / "schemas" / "state.schema.json",
                    self.work / "schemas" / "state.schema.json")

    def test_the_real_validators_pass_their_own_self_test(self) -> None:
        done = self.run_script("selftest-validators.sh")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("task_parser rejects a malformed block", done.stdout)
        self.assertIn("validate_state rejects a malformed document", done.stdout)

    def test_a_blind_validator_is_caught(self) -> None:
        """Break the control: a parser that accepts anything must fail the self-test."""
        (self.work / "scripts" / "task_parser.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8")
        done = self.run_script("selftest-validators.sh")
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("the gate is blind", done.stdout)


class SweepLocks(ShellFixture):
    def test_a_non_numeric_threshold_is_refused_before_any_remote_call(self) -> None:
        done = self.run_script("sweep-locks.sh", "--older-than", "abc")
        self.assertEqual(1, done.returncode)
        self.assertIn("whole number of seconds", done.stderr)

    def test_a_threshold_with_no_value_is_refused(self) -> None:
        done = self.run_script("sweep-locks.sh", "--older-than")
        self.assertEqual(1, done.returncode)
        self.assertIn("needs a value", done.stderr)

    def test_an_unknown_argument_is_refused(self) -> None:
        done = self.run_script("sweep-locks.sh", "--force")
        self.assertEqual(1, done.returncode)
        self.assertIn("unknown argument", done.stderr)

    def test_an_unreachable_remote_is_a_failure_not_an_empty_sweep(self) -> None:
        """"The remote was down" must never read as "there are no abandoned locks"."""
        done = self.run_script("sweep-locks.sh", "--older-than", "3600")
        self.assertNotEqual(0, done.returncode,
                            "a missing origin reported a clean sweep")
        self.assertNotIn("nothing to sweep", done.stdout)


class InstallGitleaks(ShellFixture):
    REQUIRED = ("GITLEAKS_VERSION", "GITLEAKS_SHA256", "RUNNER_TEMP", "GITHUB_PATH")

    def test_each_required_variable_is_checked_before_the_download(self) -> None:
        for missing in self.REQUIRED:
            with self.subTest(missing=missing):
                env = {name: "placeholder" for name in self.REQUIRED}
                env[missing] = ""
                done = self.run_script("install-gitleaks.sh", **env)
                self.assertEqual(1, done.returncode, done.stdout + done.stderr)
                self.assertIn(missing, done.stderr)


if __name__ == "__main__":
    unittest.main()
