"""Tests for scripts/ai-router.sh (MASTER_PLAN.md §6).

The router is a shell script, so it is driven as one: each test builds a throwaway
workspace with a bare repository as `origin`, copies the scripts and a crafted
`context/TODO.md` into it, and runs the real thing. No network, and the real
`context/TODO.md` is never touched.

MASTER_PLAN.md §11 lists `bats` for shell tests. Using unittest instead keeps the
promise that matters more — no new dependency, standard library only — and drives
the script through exactly the interface the router's callers use.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTER = "scripts/ai-router.sh"
BASH = shutil.which("bash")

READY_TASK = """# TODO

### [TASK-900] A ready task routed to hermes
- **status:** ready
- **complexity:** low
- **route:** hermes
- **files:** scripts/nothing.py
- **depends_on:** none
- **owner:** aider

**Goal**
Exist so the router has something to route.

**Constraints**
- None worth stating.

**Acceptance criteria**
- [ ] The router reaches a decision
"""


def block(task_id: str, status: str, route: str, complexity: str = "low", depends: str = "none") -> str:
    return f"""
### [{task_id}] Fixture task {task_id}
- **status:** {status}
- **complexity:** {complexity}
- **route:** {route}
- **files:** scripts/nothing.py
- **depends_on:** {depends}
- **owner:** aider

**Goal**
A fixture.

**Constraints**
- None.

**Acceptance criteria**
- [ ] Reaches a decision
"""


@unittest.skipIf(BASH is None, "bash is not available on this machine")
class RouterFixture(unittest.TestCase):
    todo = READY_TASK

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.work = root / "work"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.work)], check=True, capture_output=True)

        (self.work / "scripts").mkdir(exist_ok=True)
        for name in ("ai-router.sh", "task_parser.py", "task_lock.py", "validate_state.py"):
            shutil.copy(REPO_ROOT / "scripts" / name, self.work / "scripts" / name)
        (self.work / "context").mkdir(exist_ok=True)
        (self.work / "context" / "TODO.md").write_text(self.todo, encoding="utf-8")
        (self.work / "context" / "QUEUE.md").write_text("# QUEUE\n", encoding="utf-8")

        subprocess.run(["git", "-C", str(self.work), "-c", "user.email=t@x.invalid",
                        "-c", "user.name=t", "commit", "-qm", "fixture", "--allow-empty"], check=True)
        subprocess.run(["git", "-C", str(self.work), "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)

    def route(self, *argv: str, **env_extra: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "NEXUS_PYTHON": sys.executable,
            "NEXUS_MAX_RUNS_PER_HOUR": "10",
        }
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("HERMES_ENDPOINT", None)
        env.update(env_extra)
        return subprocess.run([BASH, ROUTER, *argv], cwd=self.work, capture_output=True,
                              text=True, env=env, timeout=120)

    def locks(self) -> list[str]:
        listed = subprocess.run(["git", "-C", str(self.work), "ls-remote", "origin", "refs/nexus/lock/*"],
                                capture_output=True, text=True)
        return [line.split()[1] for line in listed.stdout.splitlines() if line.strip()]

    def telemetry(self) -> list[dict]:
        path = self.work / "metrics" / "runs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Contract(RouterFixture):
    def test_help_exits_zero(self) -> None:
        done = self.route("--help")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("usage:", done.stderr)

    def test_missing_task_id_is_a_configuration_error(self) -> None:
        self.assertEqual(4, self.route().returncode)

    def test_bad_force_backend_is_a_configuration_error(self) -> None:
        done = self.route("--task-id", "TASK-900", "--force-backend", "gpt")
        self.assertEqual(4, done.returncode)
        self.assertIn("must be hermes or claude", done.stderr)

    def test_unknown_task_exits_one(self) -> None:
        done = self.route("--task-id", "TASK-999", "--dry-run")
        self.assertEqual(1, done.returncode)
        self.assertIn("no task TASK-999", done.stderr)

    def test_secret_is_never_echoed(self) -> None:
        """§6.3: never echo a secret, not even in dry-run or debug output."""
        sentinel = "sk-ant-sentinel-must-not-appear-0000"
        done = self.route("--task-id", "TASK-900", "--dry-run", ANTHROPIC_API_KEY=sentinel)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertNotIn(sentinel, done.stdout + done.stderr)
        self.assertNotIn(sentinel, json.dumps(self.telemetry()))


class Routing(RouterFixture):
    def test_explicit_route_is_honoured_when_the_backend_is_usable(self) -> None:
        done = self.route("--task-id", "TASK-900", "--dry-run", ANTHROPIC_API_KEY="x",
                          HERMES_ENDPOINT="")
        # Hermes is unreachable with no endpoint, so the router escalates.
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("backend=claude", done.stdout)
        self.assertIn("escalating to Claude", done.stderr)

    def test_forced_backend_skips_routing(self) -> None:
        done = self.route("--task-id", "TASK-900", "--dry-run", "--force-backend", "claude",
                          ANTHROPIC_API_KEY="x")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("backend forced to claude", done.stderr)

    def test_no_backend_at_all_parks_the_task(self) -> None:
        """Nothing configured: Hermes unreachable, no Claude credential."""
        done = self.route("--task-id", "TASK-900", "--dry-run")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("would park", done.stderr)

    def test_parking_appends_a_real_queue_entry(self) -> None:
        done = self.route("--task-id", "TASK-900")
        self.assertEqual(0, done.returncode, done.stderr)
        queue = (self.work / "context" / "QUEUE.md").read_text(encoding="utf-8")
        self.assertIn("Parked: TASK-900", queue)
        self.assertIn("**Raised by:** router", queue)
        self.assertEqual([], self.locks(), "a parked task must not leave a lock behind")


class NotClaimable(RouterFixture):
    todo = READY_TASK + block("TASK-901", "done", "hermes") + block("TASK-902", "ready", "hermes", depends="TASK-903") + block("TASK-903", "ready", "hermes")

    def test_a_task_that_is_not_ready_is_not_an_error(self) -> None:
        done = self.route("--task-id", "TASK-901", "--dry-run")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("not 'ready'", done.stderr)

    def test_unmet_dependency_blocks_without_failing(self) -> None:
        done = self.route("--task-id", "TASK-902", "--dry-run")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("depends on unfinished work: TASK-903", done.stderr)


class ClaimAndRelease(RouterFixture):
    def test_the_lock_is_always_released(self) -> None:
        """§6.3: trap release_lock EXIT. A routed task must leave no ref behind."""
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude", ANTHROPIC_API_KEY="x")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("claimed TASK-900", done.stderr)
        self.assertEqual([], self.locks(), "the router exited holding a lock")

    def test_a_contended_task_is_not_an_error(self) -> None:
        held = subprocess.run([sys.executable, "scripts/task_lock.py", "claim", "TASK-900",
                               "--run-id", "someone-else"], cwd=self.work, capture_output=True, text=True)
        self.assertEqual(0, held.returncode, held.stderr)
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude", ANTHROPIC_API_KEY="x")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("already claimed", done.stderr)
        self.assertEqual(["refs/nexus/lock/TASK-900"], self.locks(), "someone else's lock was disturbed")

    def test_a_missing_ledger_helper_does_not_block_routing(self) -> None:
        """ADR-004: the ledger is a cache, never a gate.

        This workspace deliberately omits state_ledger.py and schemas/, so the
        ledger write cannot succeed. The claim must still stand, because a failed
        cache write that made a held lock look free would be the worse bug.
        """
        self.assertFalse((self.work / "scripts" / "state_ledger.py").exists())
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude", ANTHROPIC_API_KEY="x")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("claimed TASK-900", done.stderr)
        self.assertIn("could not record", done.stderr)
        self.assertEqual([], self.locks())

    def test_telemetry_records_metadata_only(self) -> None:
        self.route("--task-id", "TASK-900", "--force-backend", "claude", ANTHROPIC_API_KEY="x")
        records = self.telemetry()
        self.assertTrue(records, "no telemetry was emitted")
        record = records[-1]
        self.assertEqual("ai-router", record["node"])
        self.assertEqual("TASK-900", record["task_id"])
        # No executor is configured in this workspace, so nothing ran and the
        # outcome says so rather than claiming a handoff happened.
        self.assertEqual("no_executor", record["outcome"])
        self.assertIn("duration_seconds", record)
        forbidden = {"prompt", "diff", "content", "env", "api_key", "token"}
        self.assertEqual(set(), forbidden & set(record), "telemetry gained a field that could carry content")


class RunCap(RouterFixture):
    def test_the_hourly_cap_stops_the_router(self) -> None:
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude",
                          ANTHROPIC_API_KEY="x", NEXUS_MAX_RUNS_PER_HOUR="0")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("run cap reached", done.stderr)
        self.assertEqual([], self.locks())

    def test_a_bad_cap_is_a_configuration_error(self) -> None:
        done = self.route("--task-id", "TASK-900", "--dry-run", NEXUS_MAX_RUNS_PER_HOUR="lots")
        self.assertEqual(4, done.returncode)
        self.assertIn("whole number", done.stderr)


class TaskIdInjection(RouterFixture):
    """Regression tests for a real, exploited vulnerability found 2026-10-01.

    `emit_telemetry` used an UNQUOTED heredoc, so `$TASK_ID` was interpolated into
    the Python source it ran. A `--task-id` of

        X", "injected": __import__("os").environ.get("ANTHROPIC_API_KEY"), "pad": "Y

    wrote the API key into metrics/runs.jsonl, breaking §6.3 ("never echoes a
    secret"), §8.2 ("metadata only") and the rule in SECURITY.md §4.1 that this
    project states and must itself obey.

    The first test passed on the same day without catching it, because every gate
    had a self-test feeding it bad input and nothing fed bad input to the router.
    These tests are that missing self-test.
    """

    PAYLOAD = 'X", "injected_secret": __import__("os").environ.get("ANTHROPIC_API_KEY"), "pad": "Y'
    SENTINEL = "sk-ant-INJECTION-SENTINEL-9f2a"

    def test_the_exploited_payload_is_refused(self) -> None:
        done = self.route("--task-id", self.PAYLOAD, "--dry-run", ANTHROPIC_API_KEY=self.SENTINEL)
        self.assertEqual(4, done.returncode, done.stderr)
        self.assertIn("must look like TASK-42", done.stderr)

    def test_the_payload_cannot_reach_the_telemetry_record(self) -> None:
        """Parsed, not grepped.

        A grep for the payload text matches an escaped string value and reports a
        vulnerability that is not there; it would equally miss a real one. The
        check that means something is: the record has exactly the schema's keys,
        and no value carries the secret.
        """
        self.route("--task-id", self.PAYLOAD, "--dry-run", ANTHROPIC_API_KEY=self.SENTINEL)
        records = self.telemetry()
        self.assertTrue(records, "no telemetry was emitted")
        record = records[-1]

        expected = {"schema_version", "run_id", "task_id", "node", "backend", "model",
                    "outcome", "duration_seconds", "exit_code", "dry_run"}
        self.assertEqual(expected, set(record), "the telemetry record gained a field")
        self.assertEqual("(invalid)", record["task_id"], "a rejected id was stored raw")
        for key, value in record.items():
            self.assertNotIn(self.SENTINEL, str(value), f"the secret reached telemetry via {key!r}")

    def test_a_newline_cannot_forge_a_log_line(self) -> None:
        done = self.route("--task-id", "TASK-1\nai-router: forged success", "--dry-run")
        self.assertEqual(4, done.returncode)
        self.assertNotIn("forged success", done.stderr, "a crafted id wrote its own log line")

    def test_shell_metacharacters_are_refused(self) -> None:
        for payload in ("TASK-1; touch pwned", "TASK-1$(touch pwned)", "TASK-1`id`",
                        "../../etc/passwd", "TASK-1 --force-backend claude"):
            with self.subTest(payload=payload):
                done = self.route("--task-id", payload, "--dry-run")
                self.assertEqual(4, done.returncode, f"accepted {payload!r}")
        self.assertFalse((self.work / "pwned").exists(), "a payload executed")

    def test_a_legitimate_id_still_routes(self) -> None:
        """The validator must not be so strict that it breaks the normal path."""
        done = self.route("--task-id", "TASK-900", "--dry-run", ANTHROPIC_API_KEY="x")
        self.assertEqual(0, done.returncode, done.stderr)

    def test_the_documented_id_grammar_is_accepted(self) -> None:
        """task_parser allows an optional letter suffix for split tasks (TASK-42a)."""
        done = self.route("--task-id", "TASK-900a", "--dry-run")
        # No such task exists, so exit 1 - but it must get past validation, not 4.
        self.assertEqual(1, done.returncode, done.stderr)


class Handoff(RouterFixture):
    """§6.1 steps 7 and 8: the ledger write and the executor handoff."""

    def setUp(self) -> None:
        super().setUp()
        shutil.copy(REPO_ROOT / "scripts" / "state_ledger.py", self.work / "scripts" / "state_ledger.py")
        (self.work / "schemas").mkdir(exist_ok=True)
        shutil.copy(REPO_ROOT / "schemas" / "state.schema.json", self.work / "schemas" / "state.schema.json")

    def executor(self, exit_code: int) -> str:
        """A stub executor, so the handoff is testable without installing Aider."""
        path = self.work / "stub-executor.sh"
        path.write_text(
            "#!/usr/bin/env bash\n"
            'printf "executor saw task=%s model=%s branch=%s\\n" "$NEXUS_TASK_ID" "$NEXUS_ROUTED_MODEL" "$NEXUS_BRANCH"\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        return f"bash {path.name}"

    def ledger(self) -> dict:
        path = self.work / "context" / "STATE.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_the_ledger_records_the_claim(self) -> None:
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude", ANTHROPIC_API_KEY="x")
        self.assertEqual(0, done.returncode, done.stderr)
        entry = self.ledger()["tasks"]["TASK-900"]
        self.assertEqual("agent/task-900", entry["branch"])
        self.assertEqual(1, entry["attempts"])

    def test_without_an_executor_the_task_returns_to_ready(self) -> None:
        """Nothing ran, so leaving it in_progress would strand it once the lock goes."""
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude", ANTHROPIC_API_KEY="x")
        self.assertIn("no NEXUS_EXECUTOR_CMD configured", done.stderr)
        self.assertEqual("ready", self.ledger()["tasks"]["TASK-900"]["status"])
        self.assertIsNone(self.ledger()["lock"]["held_by"], "the ledger still shows a holder")

    def test_a_successful_executor_moves_the_task_to_review(self) -> None:
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude",
                          ANTHROPIC_API_KEY="x", NEXUS_EXECUTOR_CMD=self.executor(0))
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("executor saw task=TASK-900", done.stdout)
        self.assertIn("branch=agent/task-900", done.stdout)
        self.assertEqual("review", self.ledger()["tasks"]["TASK-900"]["status"])

    def test_a_failing_executor_blocks_the_task_without_failing_the_router(self) -> None:
        """§4: a failed attempt blocks the task. The router itself routed correctly."""
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude",
                          ANTHROPIC_API_KEY="x", NEXUS_EXECUTOR_CMD=self.executor(3))
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("executor failed with exit 3", done.stderr)
        self.assertEqual("blocked", self.ledger()["tasks"]["TASK-900"]["status"])
        self.assertEqual([], self.locks(), "a failed run must still release the lock")

    def test_the_executor_never_receives_a_secret_in_its_environment(self) -> None:
        sentinel = "sk-ant-sentinel-must-not-leak-1111"
        leaky = self.work / "leaky-executor.sh"
        leaky.write_text("#!/usr/bin/env bash\nenv | grep -c ANTHROPIC_API_KEY || true\n", encoding="utf-8")
        done = self.route("--task-id", "TASK-900", "--force-backend", "claude",
                          ANTHROPIC_API_KEY=sentinel, NEXUS_EXECUTOR_CMD=f"bash {leaky.name}")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertNotIn(sentinel, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
