# Direct Mode Foundation + Automatic Surface Hooks V0.1

A direct local entry path and automatic lifecycle hooks. This is **not** a UI, an AI
planner, or a bypass around safety: it is the same deterministic engine and guardrails
reached without a chat controller and without manual `learn start` / `learn finish`
bookkeeping.

```text
ENTRY
├─ CHAT_HANDOFF   (external controller initiated; cost unknown to the engine)
└─ DIRECT_CLI     (this path)
       ↓
objective/input
       ↓
deterministic preflight + planner decision (NONE / DETERMINISTIC / AI)
       ↓
TOOL_ONLY / NO_CHANGE_REQUIRED / result-cache reuse / AI worker
       ↓
automatic lifecycle hooks (Run / Task / Attempt / evidence / events / provenance)
       ↓
Efficiency query layer
```

## CLI

```bash
./scripts/orchestrate direct --objective "Report current branch, HEAD, and clean state." \
    --backend fake --confirm --allow-dirty
./scripts/orchestrate direct --objective-file objective.txt --confirm
./scripts/orchestrate run ...        # equivalent engine path; run is also DIRECT_CLI by default
./scripts/orchestrate run ... --entry-mode chat-handoff   # an integration marking a chat origin
```

`direct` accepts a bounded objective from a CLI argument or `--objective-file`, following
existing orchestration conventions. The raw objective is processed in memory and never
persisted; the operational DB stores only a bounded `objective_summary`.

## Safety (a new entry path, not a bypass)

Direct mode preserves every existing guarantee: READ_ONLY/MUTATING/MIXED intent, hard
`--max-ai-calls` budget, TOOL_ONLY security (whitelisted operations, no arbitrary shell,
no path escape, secret rejection), result-cache policy, context retrieval,
availability-aware resolution, capability-preserving failover, circuit breaker, outcome
learning, operational data, and Git guardrails (no commit/push/reset/rebase/checkout).
An invalid plan (for example a mutating node under `--read-only`) is refused before any
worker launches.

## Zero-additional-AI vs true-zero-AI

- **DIRECT_CLI** with only deterministic/cache work → the local engine launches no model
  call and there is no controller, so `true_zero_ai = true`.
- **CHAT_HANDOFF** with only deterministic/cache work → `zero_additional_ai = true` (no
  local worker/planner calls), but `true_zero_ai` is **not** claimed because an external
  controller may already have spent calls the engine cannot see. Its cost is recorded as
  `None` (unknown), never inferred.

`controller_ai_calls` is `0` for a direct/automation path (a true fact about the local
path) and `None` for a chat handoff unless a trusted integration supplies an explicit
value via `--controller-ai-calls`.

## Proven offline (fake adapters only)

| Scenario | Entry | Result |
|---|---|---|
| deterministic objective | `DIRECT_CLI` | TOOL_ONLY only, 0 real calls, `true_zero_ai` |
| two logical AI tasks, warm cache | `DIRECT_CLI` | 2 cache hits, 0 real calls, `true_zero_ai` |
| one logical AI task, cold cache | `DIRECT_CLI` | cache miss, 1 fake invocation, `true_zero_ai = false` |
| deterministic/cache work | `CHAT_HANDOFF` | 0 local worker calls, `zero_additional_ai`, not `true_zero_ai` |

## Automatic surface hooks

A supported orchestrated execution records the whole lifecycle automatically — Run,
Task, Attempt, bounded outcome evidence, operational events, cache provenance, and
context metadata — with no manual learning commands. `scripts/common/lifecycle.py` is
that boundary:

- `record_run(plan, report, …)` — persist the operational lifecycle + context metadata
  (best-effort; a storage failure never turns a successful run into a failure).
- `run_direct(objective, backend, …)` — objective → plan → execute → record in one call.

OpenCode and Codex adapters flow through the **same** `execution.execute_plan` lifecycle
(per-node `evidence.start`/`finish` and per-attempt recording), so both surfaces produce
the same Run/Task/Attempt/evidence shape without surface-specific learning commands.

### Scope

V0.1 automatic hooks apply only to executions launched through this orchestration system
or supported wrappers. It does **not** scrape a UI, monitor arbitrary terminal sessions,
parse unrelated chat history, install background daemons, or hook provider traffic
globally, and it does not attempt to intercept arbitrary OpenCode/Codex conversations.

## Idempotence and crash behavior

- Re-recording a run is a no-op (`persist_run` checks the run id first): no duplicate
  run, task, attempt, or event rows.
- `evidence.finish` refuses a duplicate finalization; a decision id is finalized once.
- A cache hit creates no `Attempt` and no `observed_runtime` evidence.
- A TOOL_ONLY / NO_CHANGE_REQUIRED task records a non-model outcome only.
- A backend/adapter exception is caught and classified as a bounded `FAILURE`
  (`UNKNOWN_FAILURE`) — never a fabricated `SUCCESS` — and its evidence decision is
  finalized (no orphan pending row).
- Timeout, retry, and failover each record their real attempts; infra failures keep
  non-intelligence semantics.
- An interrupt/cancellation does not fabricate outcomes: downstream nodes are `NOT_RUN`
  and an in-flight evidence decision remains detectably `pending` (recoverable), per the
  existing semantics. Process exit alone is never treated as success.

## Outcome learning hooks

Fresh AI execution → normal `observed_runtime` evidence. Cache hit → no fresh
model-performance evidence. TOOL_ONLY → non-model outcome only. No-op → no model
evidence. Infra failure → non-intelligence failure semantics. All of this is handled by
the orchestrated lifecycle, not by manual commands.

## Deferred

UI, HTTP API/WebSocket/SSE, cross-machine shared caches, arbitrary chat interception,
provider-traffic hooks, retention scheduler, and parallel workers.
