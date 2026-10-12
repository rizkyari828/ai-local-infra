# Execution Contract Stabilization V0.1

Bounded correctness hardening of the existing execution pipeline. This milestone adds
**no** new quota-saving feature, no broader cache reuse, no local routing, no UI, and no
parallel workers. Its purpose is to make the chain

```text
USER OBJECTIVE -> TASK CONTRACT -> VALIDATED CONTEXT -> DELIVERED WORKER INPUT
-> ACTUAL ATTEMPT -> INDEPENDENT ACCEPTANCE -> ACCEPTED RESULT -> CACHE -> EVIDENCE
```

truthful: no downstream stage may pretend an upstream contract was satisfied when it
was not. This document records the contracts the code now enforces.

## Task contract

Each executable node carries one bounded, structured `task_contract`:

- `contract_version`, `task_id`
- `objective` — the task-specific objective actually delivered to the worker
- `master_fingerprint` — non-reversible identity of the whole master objective
- `operation`, `targets`, `constraints`, `acceptance`, `permissions`
- `dependencies`, `context_budget`, `capability_requirement`

Simple intact AI objectives are preserved as one node carrying the real request; only
genuinely multi-part objectives are decomposed, and each stage receives the bounded
master clause that matches it. Raw chat and the raw master prompt are never persisted.

## Delivered worker input

The final worker input is built from the task contract, the validated context pack, the
known dependency facts, and the acceptance instructions. The context whose identity and
provenance are recorded **is the context delivered** (embedded in the prompt), and a
deterministic `delivered_input_fingerprint` (over the contract, context identity,
dependency **semantics**, and acceptance contract version) is stored with the context
manifest. A dependency with an accepted-result fingerprint is identified by it; a
dependency without one is identified by the bounded semantic payload actually delivered
(status and summary), so rendering and fingerprinting can never diverge and ordering is
deterministic. One explicit `workspace_root` flows end to end: repository identity, Git
state, context retrieval, deterministic tools, worker `cwd`, mutation checks, and
artifact validation.

## Context identity

Processing order is `retrieve → select → merge → sanitize/redact → normalize/bound →
validate → FINAL CONTENT → hash`. Merging re-reads the authoritative file from the same
workspace root, re-applies redaction, and re-fingerprints the merged final content, so a
change inside a merged range changes item and manifest identity, and content removed as
sensitive cannot reappear. Identity schemas are versioned (`task-semantic-v2`,
`context-v2`, `input-v2`, `result-v2`, `acceptance-v2`).

## Execution status vs task acceptance vs eligibility

Three deliberately separate concepts:

| Concept | Values |
|---|---|
| Process/backend status | completed (`SUCCESS`) or failed |
| Task acceptance | `PASSED` / `FAILED` / `UNVERIFIED` |
| Learning & cache eligibility | only `ACCEPTED` + `ACTUAL` + attributed attempts |

`SUCCESS` means the process completed. Task `status` becomes `SUCCESS` only when
acceptance is `PASSED`; acceptance `FAILED` yields `FAILURE` (`ACCEPTANCE_FAILED`); no
independent acceptance yields `UNVERIFIED` (`ACCEPTANCE_UNVERIFIED`). Exit code 0 and
non-empty stdout, model prose, or a model completion claim never imply acceptance.
Acceptance evidence is typed: an unrecognized/malformed field type, or two recognized
fields that disagree (for example `passed` versus `status`), yields `UNVERIFIED`, never
`PASSED`. No favorable-field precedence may pick the friendlier of conflicting fields.
`UNVERIFIED` work is neither success nor failure: it cannot be cached, cannot create
positive quality evidence, and blocks dependent nodes and finalization. Finalization
reflects upstream states and never manufactures success.

When deterministic validation is unavailable, acceptance stays `UNVERIFIED`
(human-required); subjective judgment is never presented as deterministic validation.

## Cache eligibility

Reuse requires an accepted result, a compatible task contract and acceptance version, a
complete input identity, matching **provenance** (`SIMULATED` never satisfies a real
lookup, and a real result never satisfies a simulated one), a compatible capability
floor, and a compatible freshness policy. A `MUTATING` task is `RUNTIME_VOLATILE`: a
cached "patch applied" summary is never reused in place of a verified current
postcondition. Identical/unchanged-input deterministic reuse remains. When in doubt, the
result is a cache **miss**. Legacy entries with older identity versions are inert.

## Learning integrity

Quality/capability evidence is derived only from actual, accepted, attributed,
qualified attempts. Simulation (`simulated` evidence rows), process-only success,
acceptance `UNKNOWN`/`UNVERIFIED`, cache reuse, and infrastructure success without task
acceptance never produce positive quality evidence. Final accepted evidence is
attributed to the **actual accepted candidate** (backend/provider/model/native and
normalized effort), not the originally proposed model after failover.

## Deterministic intent

Deterministic execution requires **complete intent coverage** and typed operations
(`repository_state`, `git_head_matches`, `config_parse`, `config_value_matches`,
`file_exists`, `run_tests`). A stated comparison that cannot be evaluated, or an
explanatory request that merely shares keywords with a probe, is not claimed
deterministic. The finalizer cannot report success when an upstream required node is
unresolved or unaccepted.

## Invocation accounting

`worker_launches` increments at the actual launch boundary. A context rejection, policy
rejection, cache hit, deterministic completion, or no-op consumes **zero**. Every actual
worker launch produces exactly one Attempt record — including cancellation, adapter
exception, read-only violation, failure and timeout; a pre-launch rejection, cache hit or
TOOL_ONLY completion produces none. The engine distinguishes logical AI tasks, worker
launches, cache reuse, planner calls, retries, and fallbacks, and does not claim to bound
model turns internal to an external coding-agent CLI (which it cannot observe).
`--max-ai-calls` bounds worker CLI launches made by this engine.

## Resume safety

State writes are atomic. Resume validates repository identity, HEAD, **and** plan/task
contract identity, refusing a changed objective or contract. A completed AI node is reused
only after its reuse identity is re-derived from the CURRENT dependency state, so a
deterministic dependency recomputed to a new fingerprint invalidates a stale descendant
result instead of silently reusing it. Any mutating AI node whose worker actually launched
without durable accepted completion — failure, timeout, adapter error or cancellation
included — refuses resume as an uncertain mutation (a fail-closed, human-required
outcome); a provably pre-launch rejection stays retry-safe. Full durable recovery belongs
to a later milestone.

## Residual limits

Read-only enforcement is post-hoc detection (worktree paths + a content/state
fingerprint), not a sandbox: it covers tracked changes and further edits to already-dirty
files, but not ignored files or untracked content outside `git status`. Provider
inference requests inside an external CLI are not observable.
