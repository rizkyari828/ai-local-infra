# Operational Data Contract V0.2

A stable, versioned, queryable representation of what the orchestration engine is
doing and has done. It is **data infrastructure, not UI**, and lives in its own SQLite
database separate from learning and shared knowledge.

```text
$LAI_STATE_DIR/orchestration/orchestration.db   operational run state (this contract)
$LAI_STATE_DIR/learning/evidence.db             learning evidence (outcome learning)
$LAI_STATE_DIR/cache/result-cache.db            reusable local task results (result cache)
knowledge/*                                     shared portable knowledge (Git)
```

Do not merge these responsibilities into one database. The operational DB may
**reference** stable ids/names/versions but never duplicates the authoritative
definitions from `models/roles.yaml`, `config/*`, `knowledge/*`, or the task-family
catalogue.

## Entity hierarchy

```text
Project ──< Run ──< Task ──< Attempt
                 │      └──< TaskDependency (task → depends_on task)
                 └──< RunEvent (append timeline: event_id is the cursor)
```

| Entity | Identity | Notes |
|---|---|---|
| Project | `project_id` (stable repository identity, not absolute path) | one logical repository; same on Work Mac and Home WSL |
| Run | `run_id` | one master-objective lifecycle |
| Task | `(run_id, task_id)` | one DAG node; `executor_type` ∈ {TOOL_ONLY, AI} |
| TaskDependency | `(run_id, task_id, depends_on_task_id)` | relational; no self/dangling refs |
| Attempt | `attempt_id` | one real execution attempt of one task |
| RunEvent | `event_id` (monotonic cursor) | bounded operational timeline |

Indexes cover `runs(project_id|machine_id|status, created_at)`,
`tasks(run_id, status)`, `attempts(task_id, attempt_no)`, `attempts(provider, model)`,
`attempts(status, completed_at)`, `run_events(run_id|task_id, event_id)`.

## Tier vs effort (independent axes)

`required_tier` is the capability requirement; `native_effort` is the provider-native
value; `normalized_effort` is derived **only** from the native value. Tier never
overwrites effort. E.g. a MAX task run on Codex at native `high` is:

```text
required_tier = MAX
native_effort = high
normalized_effort = HIGH
```

## TOOL_ONLY representation

TOOL_ONLY execution is represented as a **Task with `executor_type=TOOL_ONLY`** (option
A), not as an Attempt. Rationale: it has no provider/model/effort and consumes zero AI
calls, so a Task result is the simplest coherent model. TOOL_ONLY tasks carry
`tool_operation` instead of routing, with `provider`/`model`/effort fields null; future
telemetry can count them by `executor_type`, and failures are traced at task level.
Attempts exist only for AI execution, so AI-call accounting stays accurate.

## Retry / failover

One task can have many attempts; failover stays on the same node:

```text
Task T2
  Attempt 1: codex / gpt-6.1-sol / HIGH   FAILED  RATE_LIMIT
  Attempt 2: opencode / deepseek-flash / HIGH  SUCCESS  is_fallback=true
```

`attempt_no` increments; `is_retry` / `is_fallback` are explicit. Successful siblings
are not rerun unless they actually were.

## Events and transactions

A small append-only `run_events` table provides the operational timeline (not full
event sourcing). `event_id` is a monotonic cursor; poll with `events after cursor N`
(never `OFFSET`). Event types are bounded: `RUN_CREATED/STARTED/COMPLETED/FAILED`,
`TASK_CREATED/STARTED/COMPLETED/FAILED/NO_CHANGE_REQUIRED/AI_REQUIRED`,
`ATTEMPT_STARTED/SUCCEEDED/FAILED`, `FAILOVER_SELECTED`, `RETRY_SELECTED`,
`CALL_BUDGET_UPDATED`, `READ_ONLY_VIOLATION`, `CALL_BUDGET_EXHAUSTED`, and the Result
Cache reuse events `CACHE_HIT`, `CACHE_MISS`, `CACHE_REJECTED`, `CACHE_ENTRY_WRITTEN`,
`TASK_REUSED`. State changes and their event are written in one SQLite transaction where
practical.

Statuses are fixed sets: Run `PLANNED/RUNNING/SUCCEEDED/FAILED/BLOCKED/CANCELLED`;
Task `PENDING/RUNNING/SUCCESS/FAILED/SKIPPED/NO_CHANGE_REQUIRED/AI_REQUIRED/BLOCKED`;
Attempt `PENDING/RUNNING/SUCCESS/FAILED`. `NO_CHANGE_REQUIRED` is a status, not an
intelligence failure. Failure categories reuse the learning taxonomy (`RATE_LIMIT`,
`PROVIDER_503`, `ACCOUNT_MODEL_UNSUPPORTED`, `REASONING_INSUFFICIENT`,
`PERMISSION_REQUIRED`, `READ_ONLY_VIOLATION`, `CALL_BUDGET_EXHAUSTED`,
`TOOL_EXECUTION_FAILED`, `DETERMINISTIC_CHECK_FAILED`, `INSUFFICIENT_AI_CALL_BUDGET`,
…).

## Result cache reuse (schema v2)

Result Cache + Incremental DAG Reuse (`docs/RESULT_CACHE.md`) adds reuse **provenance only**
to the operational contract. No accepted result body is stored here; the cache remains
the cache authority. A reused task keeps `status = SUCCESS` (no invented `CACHED` status)
and adds:

| Task column (v2) | Meaning |
|---|---|
| `execution_source` | `CACHE` (reused), `EXECUTED` (fresh AI), `TOOL_ONLY` |
| `cache_entry_id` | cache entry the result came from |
| `reused_from_run_id`, `reused_from_task_id` | original provenance of the reused result |
| `input_fingerprint`, `result_fingerprint` | deterministic identities (no secrets, no prompt text) |

A reused task creates **no** `Attempt` row (no model was invoked) and consumes **zero**
AI calls. `TASK_REUSED` plus `CACHE_HIT` communicate reuse without overloading status
semantics. Migrations are additive and non-destructive (v1 → v2 `ALTER TABLE ... ADD
COLUMN`); existing v1 databases upgrade in place with no data loss.

## Migrations, concurrency, identity

Incremental, transactional migrations keyed by an explicit `meta.schema_version`; a
failed migration rolls back and there is no destructive delete-and-recreate. Fresh DBs
migrate to latest; older DBs migrate incrementally. SQLite settings: `journal_mode=WAL`,
`busy_timeout=5000`, `foreign_keys=ON`. `machine_id` uses `LAI_MACHINE`;

## Query layer

`OrchestrationStore` exposes `status`, `recent_runs`, `get_run`, `run_tasks`,
`run_dependencies`, `task_attempts`, `run_events(after=cursor)`, `execution_summary`, and
`reuse_summary` (which tasks executed, were TOOL_ONLY, were AI, and reused — and from
where). Future API/UI/telemetry call this layer, not CLI text. A small
`./scripts/orchestrate` run integration persists each lifecycle best-effort; there is
no HTTP API, WebSocket, or UI in V0.1.

## Privacy and objective storage

Never persisted: prompts, responses, transcripts, diffs, secrets, tokens, sessions,
or environment dumps. Bounded structured payloads only; secret-like values are
rejected. Runs store a bounded non-sensitive `objective_summary` (deterministic
truncation), never the raw master prompt.

## Legacy semantic evidence

Learning observation `868087de1dda47059713f076ce26a5d9` recorded a tier-mislabelled
`normalized_effort = MAX` (native `high`; correct value `HIGH`). It is **not
rewritten**; the operational contract does not copy the bad label, and
`legacy_observation_excluded(id)` flags known semantically-invalid legacy observations
for future consumers.

## Transitional resume

Existing run-state files (`$LAI_STATE_DIR/orchestration/<run-id>/state.json`) remain
**authoritative for resume** in V0.1. The operational DB is an observational contract
(task/attempt/event history) and is not yet the resume authority; there is no dual
writable source for the same field. A future milestone can promote the DB to
authoritative resume state once it fully subsumes the state file.
