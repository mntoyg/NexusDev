# First test + security round 1 — 2026-10-01

**Result: PASSED.** Every check in the agreed scope ran against real
infrastructure, not a local stand-in. Nothing was skipped, and the one gap found
was already a documented gap rather than a surprise.

Repository at `181e587`. The demo test is **2026-10-13** and is a separate event;
this report covers neither Aider nor the full auto-dev loop, which do not exist yet.

---

## Round 1 — the bus on real infrastructure

### 1. Lock mutual exclusion, two separate clones

A second working copy was cloned from GitHub, so this is two independent
repositories racing a real remote rather than one repository claiming twice.

| Check | Result |
| :--- | :--- |
| Clone A claims `TASK-006` | exit 0 — `claimed TASK-006 as firsttest-A` |
| Clone B claims the same task | **exit 1** — `TASK-006 is already held` |
| Clone B reads the holder | `held 9c60dcb657b9 age=7s lock TASK-006 by firsttest-A ... nonce 320a0d5a1c7770a0` |
| Clone B breaks it at `--older-than 3600` | **exit 1** — `lock is 8s old, younger than 3600s; leaving it` |
| Clone A releases | `released TASK-006` |
| Refs left behind | **0** |

Exactly one winner, and the nonce from ADR-004 is visible in the lock's own commit
message — the mechanism that stops two runs pushing an identical sha and both
believing they hold the lock.

### 2. Validators on the committed files

```
task_parser:    7 task(s) valid in context/TODO.md            exit 0
validate_state: context/STATE.json absent; nothing to validate exit 0
unit suite:     108 tests ... OK
```

`STATE.json` being absent is a pass, not a skip: §3.4 calls it a rebuildable
cache, so absence is a normal state.

### 3. Gate self-tests fired in real runs, read from the logs

The green tick was not taken as evidence. Both workflows' most recent runs on
`main` were opened and read:

`secret-scan`
```
ok: private-key detected in id_canary
ok: generic-api-key detected in api.env
ok: url-embedded-credentials detected in endpoint.env
ok: no false positive on benign controls
```

`validate-context`
```
ok: task_parser rejects a malformed block
ok: validate_state rejects a malformed document
shellcheck version: 0.9.0
Ran 108 tests ... OK
```

---

## Security round 1

Only controls that currently guard something were tested. Prompt injection, task
scope escape at execution time, cost exhaustion and quota fallback all need an
agent that actually runs, so they are **round 2**, after Phase 3. Testing them now
would be testing controls with nothing behind them.

### 4. gitleaks blocks a credential embedded in a URL

A branch was pushed carrying a deliberately fake credential-bearing URL, a pull
request opened, and the result observed.

| Stage | Result |
| :--- | :--- |
| `git push` to GitHub | **succeeded** — push protection did not detect it |
| `gitleaks` check on the PR | **failed**, as required |
| Rule that caught it | `url-embedded-credentials` — the custom rule in `.gitleaks.toml` |
| Location reported | `SECURITY-TEST-CANARY.txt:13` |
| Value in the public log | `Secret: REDACTED`; the fake value appears nowhere in the run log |

**The push succeeding is the finding, and it is the expected one.** It confirms
what `SECURITY.md` §5.1 records: GitHub's free secret scanning matches provider
patterns only, so a generic credential in a URL passes it untouched. That gap is
precisely why the custom rule exists, and the custom rule is what stopped it.

Redaction matters as much as detection here. CI logs on a public repository are
public, so a scanner that printed the value it found would itself be the leak.

The canary PR was closed without merging and the branch deleted. Verified
afterwards: no `security-test/*` branch on the remote, the canary file is not on
`main`, no open pull requests, no stray lock refs.

### 5. The autonomy boundary holds

```
actions can approve PRs : false
default token           : read
required checks         : gitleaks, validate-context
approvals required      : 1
last-push approval      : true
dismiss stale reviews   : true
linear history          : true
force push / deletions  : false / false
```

The canary PR reported `mergeStateStatus=BLOCKED`, `reviewDecision=REVIEW_REQUIRED`
— so a pull request that fails a required check and has no approval cannot be
merged by an ordinary path. Invariant **I4** is enforced by GitHub, not by
convention.

Admin bypass remains deliberately enabled, for the reason recorded in
`SECURITY.md` §6: a solo maintainer cannot approve their own pull request, and
enforcing review against admins would make `main` permanently unmergeable. No
agent is an administrator, so the boundary that matters still holds.

### 6. The parser rejects path escapes

A task block declaring three hostile paths was fed to the parser:

```
files: '../../etc/passwd' escapes the repository with '..'
files: '/etc/shadow' is absolute; paths are relative to the repository root
files: 'C:/Windows/System32/config/SAM' is absolute; paths are relative to the repository root
3 problem(s)                                                              exit 1
```

All three rejected, including the Windows-style absolute path — the case that
`Path.is_absolute()` would have missed on a Windows developer machine while
failing in CI, which is why that check judges the string rather than asking the
platform.

---

## Still untested, and why

| Control | Blocked on |
| :--- | :--- |
| Prompt injection via issue and PR bodies | Phase 3 — no agent reads them yet |
| A task escaping its declared `files:` scope at execution time | Phase 3 — the parser rejects bad declarations, but nothing executes one yet |
| Cost exhaustion and the retry cap | Phase 3 — needs real executor runs to burn against |
| Quota fallback to Hermes | TASK-006 plus a local model |
| A bare high-entropy secret with no key-like name near it | Not covered by design; `SECURITY.md` §5.1 states this openly |

These are **round 2**, after Phase 3 lands.
