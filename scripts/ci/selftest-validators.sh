#!/usr/bin/env bash
# selftest-validators.sh — prove the context validators still reject bad input.
#
# Owner: Node 6 (Antigravity).
#
# A validator that has stopped validating reports the same green tick as one
# that works, so validate-context.yml runs this first and refuses to trust the
# real run until both validators have failed on purpose.
#
# Runnable locally: bash scripts/ci/selftest-validators.sh
#
# Exit codes: 0 both validators rejected bad input · 1 at least one is blind.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

PYTHON="${NEXUS_PYTHON:-python3}"
tmp="${RUNNER_TEMP:-$(mktemp -d)}/bad-input"
mkdir -p "$tmp"
fail=0

printf '### [TASK-001] deliberately broken\n- **status:** not-a-status\n' > "$tmp/TODO.md"
if "$PYTHON" scripts/task_parser.py --file "$tmp/TODO.md" --validate 2>/dev/null; then
  echo "::error::task_parser accepted a malformed task block; the gate is blind"
  fail=1
else
  echo "ok: task_parser rejects a malformed block"
fi

printf '{"schema_version": "not-a-version"}\n' > "$tmp/STATE.json"
if "$PYTHON" scripts/validate_state.py --file "$tmp/STATE.json" 2>/dev/null; then
  echo "::error::validate_state accepted a malformed document; the gate is blind"
  fail=1
else
  echo "ok: validate_state rejects a malformed document"
fi

exit "$fail"
