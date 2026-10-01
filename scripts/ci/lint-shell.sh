#!/usr/bin/env bash
# lint-shell.sh — shellcheck every tracked shell script, then refuse any
# workflow that hides shell where shellcheck cannot reach it.
#
# Owner: Node 6 (Antigravity).
#
# The problem this closes: `shellcheck -S warning scripts/*.sh` left the bash
# embedded in .github/workflows/*.yml unlinted — roughly a hundred lines of it,
# including the gitleaks self-test and the lock sweep, which are the two gates
# the rest of the repo trusts.
#
# The fix is NOT a YAML parser that pulls `run:` blocks out and lints them. That
# was the first design and it was wrong: hand-rolling enough YAML to find block
# scalars, strip their indentation and keep `${{ }}` away from the shell is a
# lot of code whose failure mode is silence — miss a block and the gate reports
# green over unlinted shell, which is the exact fault being fixed.
#
# So the shell moved instead. Every multi-line `run:` body now lives in a file
# under scripts/ci/, which makes it linted by the pass below, runnable on a
# laptop, and testable from tests/. Step 2 is what stops the gap reopening.
#
# actionlint would do all of this and more (it checks `${{ }}` expressions and
# `needs:` references too). It is deliberately not used: it is a third-party
# binary that would have to be downloaded and checksum-pinned on every run of a
# required check, and the problem it would solve here is three files we own.
#
# Exit codes: 0 clean · 1 findings · 3 shellcheck unavailable.

set -euo pipefail

WORKFLOWS="${1:-.github/workflows}"
# Overridable so a machine with shellcheck under another name can still run the
# gate, and so a test can prove the "not installed" branch without emptying PATH.
SHELLCHECK="${SHELLCHECK:-shellcheck}"

cd "$(git rev-parse --show-toplevel)"

if ! command -v "$SHELLCHECK" >/dev/null 2>&1; then
  echo "::error::shellcheck is not installed, so this gate cannot run. Install it (apt: shellcheck, brew: shellcheck) rather than skipping it." >&2
  exit 3
fi

# ------------------------------------------- 1. lint every tracked script ----
# `git ls-files` rather than a glob: a script added under a new directory cannot
# escape the linter by living somewhere a hardcoded path never looked.
#
# Captured into a variable first: a failed `git ls-files` inside a process
# substitution is invisible to `set -e`, and an empty listing would then read as
# "there is nothing to lint" rather than as a broken gate.
listing="$(git ls-files '*.sh')"

scripts=()
while IFS= read -r path; do
  [ -n "$path" ] || continue
  scripts+=("$path")
done <<< "$listing"

if [ "${#scripts[@]}" -eq 0 ]; then
  echo "::error::no tracked shell scripts found at all; this lint step has gone blind" >&2
  exit 1
fi

"$SHELLCHECK" --version
printf 'shellchecking %d tracked script(s):\n' "${#scripts[@]}"
printf '  %s\n' "${scripts[@]}"
"$SHELLCHECK" -S warning "${scripts[@]}"
echo "ok: shellcheck clean across ${#scripts[@]} script(s)"

# --------------------------------- 2. refuse unlinted shell in a workflow ----
# A block scalar after `run:` is shell living inside YAML, where neither step 1
# nor any linter can see it. One-liners stay allowed: there is nothing in
# `python3 --version` for a linter to find, and forbidding them would only push
# people into writing worse YAML.
#
# Mind the wording of comments in this file. A line beginning `# shellcheck`
# is read as a DIRECTIVE, not prose, and shellcheck fails the file with SC1072.
# This gate caught that in its own source on its first real run.
if [ ! -d "$WORKFLOWS" ]; then
  echo "::error::$WORKFLOWS does not exist, so the workflow guard checked nothing" >&2
  exit 1
fi

# The `-` alternative matters: `- run: |` is a step whose first key is run, and
# a pattern anchored straight at `run:` walks straight past it.
if grep -rnE '^[[:space:]]*(-[[:space:]]+)?run:[[:space:]]*[|>]' "$WORKFLOWS"; then
  echo "::error::the lines above are multi-line shell inside a workflow, where no linter can read them. Move the body into scripts/ci/ and call it from run:." >&2
  exit 1
fi
echo "ok: no multi-line shell in $WORKFLOWS"
