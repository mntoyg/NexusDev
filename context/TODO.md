# TODO — the executable queue

Machine-parsed. `scripts/task_parser.py` is the reference implementation of this
file's grammar, `MASTER_PLAN.md` §3.2 is its specification, and the two must
agree. Writers are Cursor and Claude; readers are Aider, Hermes and the router.

Two rules that trip people up:

- **Formatting is the interface.** A malformed block is a hard failure, not a
  skipped task. Check your edit with `python scripts/task_parser.py --validate`
  before committing.
- **Everything below the first task heading must live inside a task block.**
  There is no footer. Notes belong in the preamble you are reading.

Field vocabulary, limits and the canonical example live in `MASTER_PLAN.md` §3.2.
Inline `#` comments are not part of the grammar.

### [TASK-001] Define and validate the STATE.json schema
- **status:** ready
- **complexity:** medium
- **route:** any
- **files:** schemas/state.schema.json, scripts/validate_state.py, tests/test_validate_state.py
- **depends_on:** none
- **owner:** aider

**Goal**
`context/STATE.json` has a written schema and a validator that rejects a
malformed state file before the router ever reads it.

**Constraints**
- Model the shape documented in `MASTER_PLAN.md` §3.4: schema_version, updated_at, updated_by, lock, quota, tasks.
- Standard library only. Do not add `jsonschema`; a hand-written validator is smaller than the dependency review it would need.
- Report every problem with a JSON pointer to the offending field, not just the first failure.
- Exit 0 when valid, 1 when invalid, 3 when the file cannot be read, matching `scripts/task_parser.py`.
- Treat a missing STATE.json as valid absence, not an error: `MASTER_PLAN.md` §3.4 says the file is a rebuildable cache.

**Acceptance criteria**
- [ ] `python scripts/validate_state.py --file <valid sample>` exits 0
- [ ] A state file with an unknown task status exits 1 and names the field
- [ ] A stale lock past its TTL validates, since breaking it is the router's decision and not a schema error
- [ ] `python -m unittest discover tests` passes
- [ ] No new third-party dependency

### [TASK-002] Enforce the context bus in CI
- **status:** blocked
- **complexity:** low
- **route:** hermes
- **files:** .github/workflows/validate-context.yml
- **depends_on:** TASK-001
- **owner:** aider

**Goal**
A pull request that breaks the grammar of `context/TODO.md` or the shape of
`context/STATE.json` fails CI instead of reaching `main`.

**Constraints**
- Trigger on pull_request and on push to main, filtered to paths under `context/` and `scripts/`.
- Run `python scripts/task_parser.py --validate`, the TASK-001 state validator, and `python -m unittest discover tests`; fail the job if any exits non-zero.
- The unit suite needs no install, so do not add a dependency step or a requirements file.
- Follow `MASTER_PLAN.md` §7.2: top-level `permissions: contents: read`, no secrets, actions pinned to a full commit SHA with the version in a trailing comment.
- Pin the runner image, as `secret-scan.yml` does. Do not use `ubuntu-latest`.
- Blocked until TASK-001 lands, because the workflow runs both validators in one job.

**Acceptance criteria**
- [ ] A pull request with a malformed task block fails the check
- [ ] A pull request with a valid task block passes
- [ ] The job requests no permission beyond `contents: read`
- [ ] `python -m unittest discover tests` runs in the same job and its failure fails the check
- [ ] The workflow is added to the required status checks on `main`

### [TASK-003] Write CONTRIBUTING.md
- **status:** ready
- **complexity:** medium
- **route:** claude
- **files:** CONTRIBUTING.md
- **depends_on:** none
- **owner:** opencode

**Goal**
A newcomer can go from clone to a correctly formatted first pull request
without reading the whole MASTER_PLAN.

**Constraints**
- Cover: the file-based model in one paragraph, how to claim a task, the task block grammar with a worked example, the commit convention from `.cursorrules` §6.1, and how review works.
- State plainly that a human merges every pull request, including one opened by an agent.
- Link to `SECURITY.md` for anything security-related rather than restating it.
- Do not duplicate `MASTER_PLAN.md`. Link to it and keep this document short enough to read in five minutes.

**Acceptance criteria**
- [ ] The README "PRs Welcome" badge is repointed at CONTRIBUTING.md
- [ ] The worked example passes `python scripts/task_parser.py --validate` when pasted into TODO.md
- [ ] Every relative link in the file resolves
- [ ] No secret, endpoint or personal detail appears anywhere in it
