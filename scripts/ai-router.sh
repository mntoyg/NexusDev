#!/usr/bin/env bash
# ai-router.sh — decide who executes a task, claim it, and hand off.
#
# Owner: Node 6 (Antigravity). Specified by MASTER_PLAN.md §6.
#
# The router is the load balancer of the whole system. It never reimplements the
# lock: ADR-004 put that in scripts/task_lock.py and this file shells out to it.
#
# One deliberate deviation from §6.1, which that section now records: the task is
# parsed and validated BEFORE the lock is claimed, not after. Claiming first
# would create and immediately delete a ref for a task id that turns out not to
# exist, and it buys nothing — the claim is still the atomic gate, so two runners
# that both parse a ready task still race on the push and still produce exactly
# one winner.
#
# Exit codes (§6.2):
#   0  routed, or deliberately parked; a park is not an error
#   1  task not found or malformed
#   2  no backend available and parking failed
#   3  the lock could not be acquired
#   4  configuration error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${NEXUS_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

TASK_ID=""
DRY_RUN="${NEXUS_DRY_RUN:-0}"
FORCE_BACKEND=""
MAX_RUNS_PER_HOUR="${NEXUS_MAX_RUNS_PER_HOUR:-10}"
HERMES_ENDPOINT="${HERMES_ENDPOINT:-}"
HERMES_MODEL="${HERMES_MODEL:-hermes-local}"
METRICS_FILE="$REPO_ROOT/metrics/runs.jsonl"
STATE_FILE="$REPO_ROOT/context/STATE.json"

RUN_ID="router-$(date -u +%Y%m%dT%H%M%SZ)-$$"
LOCK_HELD=0
STARTED_AT="$(date -u +%s)"
BACKEND=""
ROUTED_MODEL=""
OUTCOME="aborted"

log()  { printf '%s\n' "ai-router: $*" >&2; }
die()  { log "$1"; exit "${2:-1}"; }

usage() {
  cat >&2 <<'USAGE'
usage: ai-router.sh --task-id TASK-042 [--dry-run] [--force-backend hermes|claude]

  --dry-run         decide and report, change nothing
  --force-backend   skip routing and use this backend
USAGE
}

# ---------------------------------------------------------------- telemetry ---
# §8: metadata only. Never a prompt body, a diff, or an environment value. This
# file is gitignored, and the schema is published, so a field that could carry
# source content would be a leak.
emit_telemetry() {
  local outcome="$1" exit_code="$2"
  local duration=$(( $(date -u +%s) - STARTED_AT ))
  mkdir -p "$(dirname "$METRICS_FILE")"
  # Every value is passed as an ARGUMENT and the heredoc is quoted, so none of it
  # is ever part of the program text.
  #
  # This used to be an unquoted heredoc that interpolated $TASK_ID straight into
  # the Python source. A --task-id crafted as
  #     X", "injected": __import__("os").environ.get("ANTHROPIC_API_KEY"), "pad": "Y
  # therefore wrote the API key into this very file - breaking 6.3 ("never echoes
  # a secret"), 8.2 ("metadata only") and the rule in SECURITY.md 4.1 that this
  # project states and must itself obey. Found 2026-10-01 while auditing after the
  # first test, which had passed without catching it.
  "$PYTHON" - "$METRICS_FILE" "$RUN_ID" "$TASK_ID" "$BACKEND" "$ROUTED_MODEL" \
             "$outcome" "$duration" "$exit_code" "$DRY_RUN" <<'PY' || true
import json
import sys

path, run_id, task_id, backend, model, outcome, duration, exit_code, dry_run = sys.argv[1:10]
record = {
    "schema_version": "1.0.0",
    "run_id": run_id,
    "task_id": task_id,
    "node": "ai-router",
    "backend": backend or None,
    "model": model or None,
    "outcome": outcome,
    "duration_seconds": int(duration or 0),
    "exit_code": int(exit_code or 0),
    "dry_run": bool(int(dry_run or 0)),
}
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")
PY
}

release_lock() {
  local code=$?
  if [ "$LOCK_HELD" = "1" ] && [ "$DRY_RUN" != "1" ]; then
    "$PYTHON" "$SCRIPT_DIR/task_lock.py" release "$TASK_ID" >/dev/null 2>&1 || log "warning: could not release the lock on $TASK_ID"
    # Clear the ledger's lock fields too, so a reader does not see a holder that
    # the refs say is gone. The ref is the truth; this keeps the cache honest.
    "$PYTHON" "$SCRIPT_DIR/state_ledger.py" unlock --file "$STATE_FILE" >/dev/null 2>&1 || true
    LOCK_HELD=0
  fi
  emit_telemetry "$OUTCOME" "$code"
}
trap release_lock EXIT

# ------------------------------------------------------------------- parsing ---
while [ $# -gt 0 ]; do
  case "$1" in
    --task-id)        TASK_ID="${2:-}"; shift 2 ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --force-backend)  FORCE_BACKEND="${2:-}"; shift 2 ;;
    -h|--help)        usage; OUTCOME="help"; exit 0 ;;
    *)                usage; die "unknown argument: $1" 4 ;;
  esac
done

[ -n "$TASK_ID" ] || { usage; die "--task-id is required" 4; }

# Validate the task id before anything else touches it. It reaches a git ref name,
# a branch name, a QUEUE.md entry and the telemetry record, and on a public
# repository it can arrive from a workflow_dispatch input or an issue - so it is
# untrusted until proven otherwise. The grammar is the one task_parser.py enforces.
if ! [[ "$TASK_ID" =~ ^TASK-[0-9]+[a-z]?$ ]]; then
  # Report a stripped, truncated copy: echoing the raw value would let a crafted
  # id forge log lines with embedded newlines or control characters.
  safe_id="$(printf '%s' "$TASK_ID" | tr -cd '[:alnum:]_.-' | cut -c1-40)"
  # Keep the raw value out of the telemetry record too. It is inert there now that
  # values are passed as arguments, but 8 says metadata only, and letting an
  # arbitrary string be stuffed into a published-schema file is still wrong.
  TASK_ID="(invalid)"
  die "--task-id must look like TASK-42; got something else (sanitised: '$safe_id')" 4
fi
case "$FORCE_BACKEND" in
  ""|hermes|claude) ;;
  *) die "--force-backend must be hermes or claude, got '$FORCE_BACKEND'" 4 ;;
esac
case "$MAX_RUNS_PER_HOUR" in
  ''|*[!0-9]*) die "NEXUS_MAX_RUNS_PER_HOUR must be a whole number, got '$MAX_RUNS_PER_HOUR'" 4 ;;
esac

# ------------------------------------------------------------- the run cap ----
# §9: ten agent runs an hour. Counted from the telemetry file, which is the only
# record that survives a runner, so the cap holds across machines.
runs_this_hour() {
  [ -f "$METRICS_FILE" ] || { echo 0; return; }
  "$PYTHON" - "$METRICS_FILE" <<'PY'
import json, sys, time
cutoff = time.time() - 3600
count = 0
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("dry_run"):
                continue
            stamp = record.get("run_id", "")
            # run_id carries the UTC start: router-YYYYmmddTHHMMSSZ-pid
            parts = stamp.split("-")
            if len(parts) < 3:
                continue
            try:
                started = time.mktime(time.strptime(parts[1], "%Y%m%dT%H%M%SZ")) - time.timezone
            except ValueError:
                continue
            if started >= cutoff:
                count += 1
except OSError:
    pass
print(count)
PY
}

if [ "$DRY_RUN" != "1" ]; then
  used="$(runs_this_hour)"
  if [ "$used" -ge "$MAX_RUNS_PER_HOUR" ]; then
    log "run cap reached: $used runs in the last hour, limit $MAX_RUNS_PER_HOUR"
    OUTCOME="capped"
    exit 0
  fi
fi

# ------------------------------------------------- parse and validate first ---
task_json="$("$PYTHON" "$SCRIPT_DIR/task_parser.py" --file "$REPO_ROOT/context/TODO.md" --task-id "$TASK_ID" --compact 2>/dev/null)" || {
  OUTCOME="not_found"
  die "no task $TASK_ID in context/TODO.md, or the file is malformed" 1
}

read_field() { "$PYTHON" -c 'import json,sys; print(json.loads(sys.argv[1]).get(sys.argv[2], "") or "")' "$task_json" "$1"; }

STATUS="$(read_field status)"
COMPLEXITY="$(read_field complexity)"
ROUTE="$(read_field route)"
OWNER="$(read_field owner)"

if [ "$STATUS" != "ready" ]; then
  # Not an error: the bus is telling us this task is not claimable yet.
  log "$TASK_ID is '$STATUS', not 'ready'; nothing to route"
  OUTCOME="not_ready"
  exit 0
fi

# A dependency check needs every task, not just this one, so the parser is
# imported rather than shelled out to twice.
unmet="$("$PYTHON" - "$REPO_ROOT/context/TODO.md" "$TASK_ID" "$SCRIPT_DIR/task_parser.py" <<'PY'
import importlib.util
import sys
from pathlib import Path

todo, task_id, parser_path = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("task_parser", parser_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
tasks, _ = module.parse_file(Path(todo))
by_id = {task["id"]: task for task in tasks}
target = by_id.get(task_id, {})
print(",".join(d for d in target.get("depends_on", []) if by_id.get(d, {}).get("status") != "done"))
PY
)"

if [ -n "$unmet" ]; then
  log "$TASK_ID depends on unfinished work: $unmet"
  OUTCOME="blocked"
  exit 0
fi

# ------------------------------------------------------------ classification ---
hermes_healthy() {
  [ -n "$HERMES_ENDPOINT" ] || return 1
  # Explicit timeout, documented degradation: unreachable Hermes means cloud-only
  # routing, never a hard stop (§6.3, I2).
  curl -fsS --max-time 10 "$HERMES_ENDPOINT/api/tags" >/dev/null 2>&1
}

claude_available() {
  [ -n "${ANTHROPIC_API_KEY:-}" ]
}

if [ -n "$FORCE_BACKEND" ]; then
  BACKEND="$FORCE_BACKEND"
  log "backend forced to $BACKEND"
else
  case "$ROUTE" in
    hermes|claude)
      BACKEND="$ROUTE"
      log "honouring the task's explicit route: $BACKEND"
      ;;
    any|"")
      case "$COMPLEXITY" in
        low)  BACKEND="hermes" ;;
        high) BACKEND="claude" ;;
        *)    BACKEND="claude" ;;
      esac
      log "route is 'any'; complexity '$COMPLEXITY' selects $BACKEND"
      ;;
  esac
fi

# --------------------------------------------------------------- health tiers ---
park_task() {
  local reason="$1"
  local queue="$REPO_ROOT/context/QUEUE.md"
  [ -f "$queue" ] || return 1
  if [ "$DRY_RUN" = "1" ]; then
    log "would park $TASK_ID in context/QUEUE.md: $reason"
    return 0
  fi
  {
    printf '\n## [QUEUE-%s] Parked: %s\n' "$(date -u +%Y%m%d%H%M%S)" "$TASK_ID"
    printf -- '- **Raised by:** router\n'
    printf -- '- **Blocked on:** claude\n'
    printf -- '- **Priority:** high\n'
    printf -- '- **Raised at:** %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf -- '- **Reason:** %s\n' "$reason"
    printf -- '- **Context:** routed by %s; task complexity %s, requested route %s, owner %s.\n' "$RUN_ID" "$COMPLEXITY" "$ROUTE" "$OWNER"
    printf -- '- **Proposed direction:** rerun the router once quota returns. No plan change is implied.\n'
  } >> "$queue" || return 1
  log "parked $TASK_ID in context/QUEUE.md: $reason"
  return 0
}

if [ "$BACKEND" = "claude" ] && ! claude_available; then
  if [ "$COMPLEXITY" = "high" ]; then
    # §6.1 step 5: high complexity is Claude-only. Downgrading it would produce
    # confidently wrong work, which is more expensive than waiting.
    if park_task "Claude quota unavailable and complexity is high, so Hermes is not an acceptable substitute"; then
      OUTCOME="parked"
      exit 0
    fi
    OUTCOME="no_backend"
    die "no backend available and parking failed" 2
  fi
  if hermes_healthy; then
    log "Claude unavailable; falling back to Hermes"
    BACKEND="hermes"
  else
    if park_task "Claude quota unavailable and Hermes is unreachable"; then
      OUTCOME="parked"
      exit 0
    fi
    OUTCOME="no_backend"
    die "no backend available and parking failed" 2
  fi
fi

if [ "$BACKEND" = "hermes" ] && ! hermes_healthy; then
  if claude_available; then
    log "Hermes unreachable; escalating to Claude"
    BACKEND="claude"
  else
    if park_task "Hermes is unreachable and no Claude credential is configured"; then
      OUTCOME="parked"
      exit 0
    fi
    OUTCOME="no_backend"
    die "no backend available and parking failed" 2
  fi
fi

case "$BACKEND" in
  hermes) ROUTED_MODEL="$HERMES_MODEL" ;;
  claude) ROUTED_MODEL="${NEXUS_CLAUDE_MODEL:-claude-sonnet-5}" ;;
esac

# ----------------------------------------------------------------- the claim ---
if [ "$DRY_RUN" = "1" ]; then
  log "dry run: would claim $TASK_ID and route it to $BACKEND ($ROUTED_MODEL)"
  printf 'task=%s backend=%s model=%s\n' "$TASK_ID" "$BACKEND" "$ROUTED_MODEL"
  OUTCOME="dry_run"
  exit 0
fi

set +e
"$PYTHON" "$SCRIPT_DIR/task_lock.py" claim "$TASK_ID" --run-id "$RUN_ID"
claim_code=$?
set -e
case "$claim_code" in
  0) LOCK_HELD=1 ;;
  1) log "$TASK_ID is already claimed by another run"; OUTCOME="contended"; exit 0 ;;
  *) OUTCOME="lock_error"; die "could not acquire the lock on $TASK_ID" 3 ;;
esac

# ------------------------------------------------------------- ledger (step 7) ---
log "claimed $TASK_ID; routed to $BACKEND"
BRANCH="agent/task-$(printf '%s' "$TASK_ID" | sed 's/^TASK-//' | tr 'A-Z' 'a-z')"

# The ledger is a cache, never a gate (ADR-004). A failed write must not make a
# held lock look free, so this warns and carries on rather than aborting.
if ! "$PYTHON" "$SCRIPT_DIR/state_ledger.py" claim "$TASK_ID" \
      --run-id "$RUN_ID" --branch "$BRANCH" --route "$BACKEND" --ttl 900 \
      --file "$STATE_FILE" >/dev/null; then
  log "warning: could not record $TASK_ID in the ledger; the ref still holds the claim"
fi

# ------------------------------------------------------------ handoff (step 8) ---
# The executor is configured, not hardcoded. §6.1 says "hand off to Aider", but a
# router that names one tool cannot be tested without installing it, and cannot be
# swapped when Phase 3 decides how Aider is actually invoked. NEXUS_EXECUTOR_CMD
# receives the decision through the environment.
printf 'task=%s backend=%s model=%s branch=%s run=%s\n' "$TASK_ID" "$BACKEND" "$ROUTED_MODEL" "$BRANCH" "$RUN_ID"

if [ -z "${NEXUS_EXECUTOR_CMD:-}" ]; then
  # Nothing ran, so the task must go back to being claimable. Leaving it
  # in_progress would strand it there for good once the lock is released.
  log "no NEXUS_EXECUTOR_CMD configured; nothing executed"
  "$PYTHON" "$SCRIPT_DIR/state_ledger.py" finish "$TASK_ID" --status ready --file "$STATE_FILE" >/dev/null || true
  OUTCOME="no_executor"
  exit 0
fi

log "handing off to the configured executor"
set +e
NEXUS_TASK_ID="$TASK_ID" \
NEXUS_ROUTED_MODEL="$ROUTED_MODEL" \
NEXUS_BACKEND="$BACKEND" \
NEXUS_BRANCH="$BRANCH" \
NEXUS_RUN_ID="$RUN_ID" \
  sh -c "$NEXUS_EXECUTOR_CMD"
executor_code=$?
set -e

if [ "$executor_code" -eq 0 ]; then
  log "executor finished; $TASK_ID moves to review"
  "$PYTHON" "$SCRIPT_DIR/state_ledger.py" finish "$TASK_ID" --status review --file "$STATE_FILE" >/dev/null || true
  OUTCOME="executed"
  exit 0
fi

# §4: a failed attempt blocks the task rather than retrying here. The attempt
# counter in the ledger is what enforces the cap of three across runs.
log "executor failed with exit $executor_code; $TASK_ID moves to blocked"
"$PYTHON" "$SCRIPT_DIR/state_ledger.py" finish "$TASK_ID" --status blocked --file "$STATE_FILE" >/dev/null || true
OUTCOME="executor_failed"
exit 0
