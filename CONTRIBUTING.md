# Contributing to NexusDev

Owned by Node 5 (OpenCode). Written by Node 1 during the Phase 1 bootstrap,
because OpenCode is not wired up yet.

This page should take about five minutes. It does not repeat
[`context/MASTER_PLAN.md`](context/MASTER_PLAN.md) — that document is the single
source of truth for architecture and file contracts, and this one only tells you
how to get a pull request landed.

---

## The one idea you need

NexusDev agents do not talk to each other. They read and write files under
`context/`, and Git is the transport. A plan, a task, a decision and a result are
all Markdown or JSON committed to the repository, which is why work survives a
closed session and why `git log` is a complete audit trail of every AI decision.

The practical consequence for you: **`context/TODO.md` is parsed by a program**,
so its formatting is an API. A malformed block is a failed build, not a tidiness
complaint.

---

## Setup

```bash
git clone https://github.com/mntoyg/NexusDev.git
cd NexusDev
python --version          # 3.10 or newer
python -m unittest discover tests
```

There is nothing to install. Everything in this repository is standard library
by design, and adding a dependency needs an ADR
([`MASTER_PLAN.md`](context/MASTER_PLAN.md) §11) whose default answer is no.

---

## Claiming a task

1. Open [`context/TODO.md`](context/TODO.md) and find a block with
   `status: ready` whose `depends_on` tasks are all `done`.
2. Comment on the matching issue, or open one naming the task id, so two people
   do not start the same work.
3. Branch as `agent/task-<id>`, for example `agent/task-004`.

If nothing is `ready`, an issue labelled `good-first-task` is also a fine place
to start. If you want to propose new work, add a task block rather than a prose
issue — the block is what an agent can execute.

---

## The task block grammar

This is the whole format. There are **no inline `#` comments**: a `#` is never
valid in a task id or a path, so the parser rejects it.

```markdown
### [TASK-042] Add retry-with-backoff to the Comet emitter
- **status:** ready
- **complexity:** low
- **route:** hermes
- **files:** scripts/telemetry.py, tests/test_telemetry.py
- **depends_on:** none
- **owner:** aider

**Goal**
One sentence. What must be true when this is done.

**Constraints**
- Bulleted, testable, unambiguous.
- Name the exact functions, files and signatures.

**Acceptance criteria**
- [ ] Objectively verifiable statement
- [ ] `python -m unittest discover tests` passes
- [ ] No new dependency added
```

Accepted values, and the limits the parser enforces:

| Field | Accepted values |
| :--- | :--- |
| `status` | `ready` · `in_progress` · `blocked` · `review` · `done` |
| `complexity` | `low` · `medium` · `high` |
| `route` | `hermes` · `claude` · `any` |
| `owner` | `aider` · `cursor` · `claude` · `opencode` · `human` |
| `depends_on` | Task ids, comma separated, or the word `none` |
| `files` | Repository-relative paths, comma separated. **At most 5.** No absolute paths, no `..` |

The parser also checks the task graph as a whole: duplicate ids, a `depends_on`
pointing at a task that does not exist, and dependency cycles. A `#` or `##`
heading closes the task list, so there is no footer section.

**Always check your edit before committing:**

```bash
python scripts/task_parser.py --validate
```

It exits `0` when every block is valid, and otherwise prints `file:line` for each
problem. The same command runs in CI, so a broken block fails the pull request.

---

## Running the gates locally

Every gate's shell lives in `scripts/ci/`, not inside the workflow YAML, so you
can run the real thing on your own machine instead of pushing to find out:

```bash
python -m unittest discover -s tests          # the whole suite, no install needed
bash scripts/ci/selftest-validators.sh        # proves the validators still reject bad input
bash scripts/ci/lint-shell.sh                 # shellcheck, plus the no-shell-in-YAML rule
bash scripts/ci/selftest-gitleaks.sh          # needs gitleaks, jq, openssl, ssh-keygen
bash scripts/ci/sweep-locks.sh --older-than 3600
```

On Windows, pass `NEXUS_PYTHON=python` if `python3` is not on your PATH.

Two rules these scripts exist to keep, both in `MASTER_PLAN.md` §7.2:

- **No multi-line `run:` in a workflow.** Shell inside YAML is shell no linter
  reads and no test drives. `lint-shell.sh` fails the build if a `run: |` body
  reappears; move it into `scripts/ci/` and call it instead.
- **No `${{ }}` in a shell body.** Pass values through `env:`, where they are
  data rather than code.

---

## Commits

Conventional Commits, with the component as the scope
([`.cursorrules`](.cursorrules) §6.1):

```
feat(router): add Hermes fallback when Claude quota is exhausted
fix(parser): tolerate CRLF line endings in TODO.md
docs(context): record ADR-004 on state-file locking
chore(ci): pin actions to commit SHAs
```

One task per commit, and the body cites the task: `Refs: TASK-042`.

Never force-push a shared branch — another agent may be mid-run on it. Never
commit `.env`, keys, credentials or anything else in `.gitignore`. Before you
commit, read the diff as a stranger on the internet would, because this
repository is public and they will.

---

## Review, and who merges

Two checks run on every pull request and both must pass:

| Check | What it does |
| :--- | :--- |
| `gitleaks` | Scans the full history for secrets, including a rule for credentials embedded in a URL. It self-tests on every run, so it cannot silently go blind. |
| `validate-context` | Runs the task grammar check, the `STATE.json` schema check and the unit suite — after proving both validators still reject bad input. |

Then:

- A pull request that touches `context/`, security or workflow files gets the
  `needs-architect` label and Claude reviews the design.
- **A human merges every pull request. Always.** That includes one opened by
  Aider, by Dependabot, or by any other agent. It is invariant **I4** in the
  MASTER_PLAN, and it is enforced by branch protection on `main`, not by
  politeness. Agents may write, commit and open pull requests; they may never
  merge.

`main` requires linear history, so pull requests are squashed or rebased, never
merged with a merge commit.

---

## Security

Do not open a public issue for a vulnerability. Everything about reporting,
scope, safe harbour and this project's threat surface lives in
[`SECURITY.md`](SECURITY.md) — including an honest inventory of which controls
are live today and which are not.

The one rule worth repeating here: **no secrets, ever.** Not in code, not in
Markdown, not in a commit message, not in a task block. Configuration is
injected from the environment and documented in `.env.example` with empty values.

---

## Where to look next

| If you want to | Read |
| :--- | :--- |
| Understand the architecture | [`context/MASTER_PLAN.md`](context/MASTER_PLAN.md) |
| Know what to build | [`context/TODO.md`](context/TODO.md) |
| Understand the agent boundaries | [`.cursorrules`](.cursorrules) |
| Report a vulnerability | [`SECURITY.md`](SECURITY.md) |

Thanks for contributing. If something here is wrong or unclear, that is itself a
bug worth a pull request.
