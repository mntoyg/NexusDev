#!/usr/bin/env bash
# sweep-locks.sh — break the lock refs left behind by runners that died.
#
# Owner: Node 6 (Antigravity).
#
# ADR-004 makes `refs/nexus/lock/<task-id>` the mutex, which means a runner
# killed mid-task leaves a ref nobody will ever release. This is the only thing
# in the repository that needs `contents: write`, so it stays one narrow script:
# list the locks, ask task_lock.py to break the ones past the age threshold, and
# count the outcomes.
#
# Runnable locally against a real remote, which is how the hourly schedule
# should be rehearsed before it is trusted:
#   bash scripts/ci/sweep-locks.sh --older-than 3600
#
# Exit codes: 0 every lock reached a decision · 1 bad threshold, or at least one
# sweep failed for a reason that is not "too young to break".

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

PYTHON="${NEXUS_PYTHON:-python3}"
older_than="${OLDER_THAN:-3600}"

while [ $# -gt 0 ]; do
  case "$1" in
    --older-than)
      if [ $# -lt 2 ]; then
        echo "::error::--older-than needs a value" >&2
        exit 1
      fi
      older_than="$2"
      shift 2
      ;;
    -h|--help) echo "usage: sweep-locks.sh [--older-than SECONDS]" >&2; exit 0 ;;
    *) echo "::error::unknown argument '$1'" >&2; exit 1 ;;
  esac
done

case "$older_than" in
  ''|*[!0-9]*)
    echo "::error::older_than must be a whole number of seconds, got '$older_than'" >&2
    exit 1
    ;;
esac

# Captured into a variable rather than piped into the loop: a failed `ls-remote`
# inside a process substitution is invisible to `set -e`, and "the remote was
# unreachable" would read as "there are no locks" — the sweep would report a
# clean run while every abandoned lock stayed put.
listing="$(git ls-remote origin 'refs/nexus/lock/*')"

tasks=()
while read -r _ ref; do
  [ -n "${ref:-}" ] || continue
  tasks+=("${ref#refs/nexus/lock/}")
done <<< "$listing"

if [ "${#tasks[@]}" -eq 0 ]; then
  echo "no locks present; nothing to sweep"
  exit 0
fi

echo "locks found:"
printf '  %s\n' "${tasks[@]}"

broken=0
kept=0
failed=0
for task in "${tasks[@]}"; do
  set +e
  "$PYTHON" scripts/task_lock.py break "$task" --older-than "$older_than"
  code=$?
  set -e
  case "$code" in
    0) broken=$((broken + 1)) ;;
    1) kept=$((kept + 1)) ;;
    *) echo "::error::sweeping $task failed with exit $code"; failed=$((failed + 1)) ;;
  esac
done

# Counted rather than merely logged: a rising number of breaks means runners are
# dying, which is a real signal for Comet and not noise.
echo "swept: broken=$broken kept=$kept failed=$failed"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  echo "lock_sweep broken=$broken kept=$kept failed=$failed" >> "$GITHUB_STEP_SUMMARY"
fi
[ "$failed" -eq 0 ]
