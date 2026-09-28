# ADR-004 — State-file locking strategy

- **Status:** Accepted
- **Date:** 2026-09-28
- **Deciders:** human operator, Node 1 (Claude)
- **Supersedes:** nothing
- **Drains:** QUEUE-007
- **Affects:** `MASTER_PLAN.md` §6.1, §6.3, §9; `schemas/state.schema.json`; `scripts/ai-router.sh` (unwritten)

> ADRs are immutable once merged. To change this decision, write a new ADR that
> supersedes it.

## Context

Two router runs can try to claim the same task at the same moment. Invariant I2
says no node may block on another, so the mechanism must not introduce a
component every node waits on, and GitHub Actions runners are ephemeral, so a run
that dies mid-claim must not wedge the pipeline permanently.

`schemas/state.schema.json` already models an advisory lock — `held_by`,
`acquired_at`, `ttl_seconds` — and `scripts/validate_state.py` deliberately
treats a lock past its TTL as *valid*, because breaking it is the router's
decision and not a schema error.

The problem with that lock alone is that it cannot actually exclude anyone. Two
runners both read `STATE.json`, both see `held_by: null`, both write themselves
in, and the later write wins silently. A file gives no compare-and-swap.

The problem with a Git ref alone is the opposite: pushing a ref is atomic, but a
ref carries no TTL, no timestamp a human can read, and no way to see the whole
pipeline's state at a glance.

## Decision

**Use both, with a clear division of authority.**

1. **A Git ref is the mutex.** A claim on task `<id>` is
   `refs/nexus/lock/<id>`, created with

   ```bash
   git push --force-with-lease="refs/nexus/lock/<id>:" origin "<sha>:refs/nexus/lock/<id>"
   ```

   The empty right-hand side of the lease means *the ref must not exist*, which
   is a compare-and-swap on absence. Losing the race is a rejected push, not a
   corrupted file. Releasing is `git push origin --delete refs/nexus/lock/<id>`.

2. **`STATE.json` is the ledger, never the mutex.** After winning the push, the
   holder records `held_by`, `acquired_at` and `ttl_seconds` so a human, and
   Comet, can see who holds what and since when. A disagreement between the ref
   and the ledger is resolved in favour of **the ref**: the ledger is part of a
   cache that `MASTER_PLAN.md` §3.4 already declares rebuildable.

3. **A lock past its TTL may be broken by any later run**, which deletes the ref
   and emits a telemetry event. This is what keeps a killed runner from wedging
   the pipeline. `trap release_lock EXIT` covers a crash; only the TTL covers a
   `SIGKILL`.

### Two hazards that are part of the decision, not footnotes

Both were found by experiment before this ADR was written, not reasoned about.

**The lock object must be unique per claimant.** If two runners push the *same*
commit sha to the lock ref, the second push is a no-op that Git reports as
success — **both runners then believe they hold the lock**, and the mutex has
silently failed. Verified: two clones pushing an identical sha both exited 0.
The claim must therefore push an object unique to the run, for example

```bash
sha=$(git commit-tree "$(git rev-parse HEAD^{tree})" -p HEAD -m "lock $TASK_ID by $RUN_ID")
```

**An empty sha turns a claim into a theft.** `git push "$sha:$ref"` with `$sha`
empty is `git push ":$ref"`, which *deletes* the ref. A failure to build the lock
object would therefore release someone else's lock instead of failing. The claim
must refuse to push an empty left-hand side.

## Consequences

**Good**

- Mutual exclusion is real, and provided by the transport the project already
  depends on. No new service, no new dependency, no violation of I2.
- Nothing new is required of a runner: it already has a Git identity.
- The lock is visible in two places at once — `git ls-remote origin 'refs/nexus/lock/*'`
  for the truth, `STATE.json` for the human-readable story.
- A killed runner costs one TTL of delay, never a permanent stall.

**Bad, and accepted**

- A claim costs a network round trip, so the router must not claim inside a tight
  loop.
- Two sources of information can disagree. The tie-break is written down above,
  and the ledger being wrong is already an expected condition.
- `refs/nexus/lock/*` accumulates if a release is missed. A sweeper for refs
  older than any plausible TTL is follow-up work, tracked in `context/TODO.md`.
- Anyone with push access can delete a lock ref. That is the same trust boundary
  as the repository itself, so it adds no new exposure.

**Rejected outright**

- A single-writer daemon. It contradicts invariant I2 by making every node wait
  on one component, and it would need hosting that this project does not have.

## Alternatives considered

| Option | Why not |
| :--- | :--- |
| **Advisory lock in `STATE.json` only** | Cannot exclude. Two runners read `held_by: null` and both write themselves in; the later write wins silently. This is the option that looked cheapest and is the one the hybrid keeps, but only as the ledger. |
| **Git ref only** | Atomic, but opaque: no TTL, no timestamp, nothing a human or Comet can read without shelling out to `ls-remote` per task. |
| **`flock` or a lock file on the runner** | Runners are ephemeral and isolated. A lock local to one machine excludes nothing. |
| **GitHub Actions `concurrency` groups** | Already used for workflow-level serialisation, but the key is a ref rather than a task id, and it cannot express a TTL or survive being queried by a non-Actions caller such as a developer running the router locally. |
| **Single-writer daemon** | Violates I2, needs hosting. |

## How this decision gets verified

The mechanism was tested before adoption, with a bare repository and two clones
racing. The first attempt at that experiment was itself wrong — the bare
repository defaulted to `master` while the test pushed `main`, so a clone had no
usable `HEAD` and the push failed for an unrelated reason that *looked* like the
lock working correctly. The corrected run is what the hazards above are based on.

The implementing task must carry that experiment into the repository as tests, so
the no-op-push hazard in particular cannot regress unnoticed.
