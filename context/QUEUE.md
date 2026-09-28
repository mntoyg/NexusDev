# QUEUE — the escalation buffer

Work that needs Claude-grade reasoning but arrived while its quota was gone is
**parked here, not downgraded and not dropped**. Claude drains this file
oldest-first within a priority band, converts each entry into either a
`MASTER_PLAN.md` amendment or one or more `TODO.md` task blocks, and deletes the
drained entry in the same commit.

Writers are Cursor, Aider and the router. The reader is Claude. The format is
specified in `MASTER_PLAN.md` §3.3.

QUEUE-007 was drained on 2026-09-28 into [ADR-004](decisions/ADR-004-state-file-locking.md) and TASK-004.

This file is **not** machine-parsed today — see QUEUE-008, which asks whether it
should be. Unlike `context/TODO.md`, a malformed entry here degrades a human
handover rather than breaking a build.

## [QUEUE-008] Decide whether QUEUE.md should be machine-parsed
- **Raised by:** claude
- **Blocked on:** claude
- **Priority:** low
- **Raised at:** 2026-09-28T00:00:00Z
- **Reason:** `context/TODO.md` is parsed and CI-enforced while this file is not, so the two halves of the context bus have different guarantees. Whether that asymmetry is correct is a design question, not a coding task.
- **Context:** `MASTER_PLAN.md` §7.1 describes `validate-context.yml` as enforcing the bus, and the workflow currently validates `TODO.md` and `STATE.json` only. A parser would add roughly one file and a test file. The argument against is that this file is drained by a reasoning model rather than executed by Aider, so strictness buys less here and costs a second grammar to keep in step.
- **Options considered:** (a) leave it prose and keep the asymmetry documented, as it is in this file's preamble; (b) parse it with the same strictness as `TODO.md`; (c) validate only the field header and leave the prose sections free.
- **Proposed direction:** (c), if anything. Deferred until a real entry is mangled badly enough to lose information, because the cost of a wrong grammar here is a second contract to maintain for little gain. Labelled as a proposal.
