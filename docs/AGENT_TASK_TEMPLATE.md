# Agent task template

Use this template to describe only the task-specific delta. Stable operating rules
live in [`../AGENTS.md`](../AGENTS.md) and are not repeated here.

```text
Repository / Branch
Scope
Read
Goal
Required changes
Non-goals
Validation
Git
Stop condition
```

## Section guidance

- **Repository / Branch**: name the repository and the exact branch to work on.
- **Scope**: the single directory or solution that may change.
- **Read**: the minimal files or guides to read first; avoid repo-wide scans.
- **Goal**: one or two sentences describing the intended outcome.
- **Required changes**: concrete, bounded deliverables.
- **Non-goals**: what must not be added or changed in this task.
- **Validation**: the smallest commands and checks that prove the change.
- **Git**: commit message, branch to push, and merge/force-push prohibitions.
- **Stop condition**: when to stop; do not expand beyond the requested delta.
