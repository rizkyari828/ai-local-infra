# Task graph orchestration (V0.1)

`./scripts/orchestrate` turns ONE bounded objective into a small task graph (DAG)
with per-node symbolic routing and context budgets, so you can give a single
high-level objective instead of hand-splitting work into Low/Medium/High prompts.

**V0.1 is plan-only.** It calls no model, makes no network request, spawns no
worker, and persists nothing. The raw objective is processed in memory and is
never written to `evidence.db`, `knowledge/*`, a decision id, or any plan file.

```bash
./scripts/orchestrate plan --objective "Audit the multi-machine AI infra, fix graphify config if needed, update docs, run tests, and give the final report"
./scripts/orchestrate plan --objective-file objective.txt --json
./scripts/orchestrate plan --objective "..." --surface codex-cli --max-tier BALANCED
```

## Flow

```text
MASTER OBJECTIVE -> TASK DECOMPOSITION -> TASK GRAPH (DAG) -> PER-NODE ROUTING -> CONTEXT BUDGET -> EXECUTION PLAN
```

Decomposition reuses existing layers and never hardcodes providers/models: nodes
request symbolic tiers (`FAST`/`BALANCED`/`STRONG`/`MAX`) + normalized effort;
concrete models resolve later through discovery, exactly as plain routing does.

## Task node schema

`task_id`, `title`, `task_family`, `task_class`, `objective_summary`, `depends_on`,
`routing {tier, effort, role}`, `context {budget, include, excluded, manifest}`,
`acceptance_criteria`, `risk_signals`, `conditional`, `status`, plus bounded
`produces_artifacts` / `consumes_results_from`. The whole raw master prompt is
never stored in a node.

## DAG, not a flat list

Nodes declare dependencies. The planner validates: dependency ids exist, no
self-dependency, no cycles, and a topological order exists; invalid graphs are
rejected with diagnostics. A final integration node (`T6`) consumes the compact
outcomes of the leaves and checks upstream acceptance criteria; it does not
concatenate worker responses.

## Task-size discipline

A node is created only when at least one applies: different reasoning
requirement, independent acceptance criteria, a meaningful dependency boundary, a
different context requirement, a different risk level, or separate execution can
reduce expensive-model usage. Tool actions (read/grep/git status/open file) stay
*inside* a task. Plans with more than a handful of nodes warn
(`EXCESSIVE_TASK_SPLITTING`).

## Context budgeting (minimum sufficient context)

Each node gets a symbolic budget (`TINY`/`SMALL`/`MEDIUM`/`LARGE`, scope not token
limits) and an explicit include/exclude manifest. Defaults by class:
`MECHANICAL` small, `NORMAL` small, `CORRECTNESS_SENSITIVE` medium, `CRITICAL`
medium. `LARGE` requires justification (a recorded risk signal). Full chat, the
whole repository, and all previous outputs are **excluded** by default.

Dependent nodes receive a compact **dependency output summary** (decision,
changed assumptions, files impacted, acceptance notes) — never the full previous
model response — which prevents context multiplication. Before allocating broad
context the plan consults `knowledge/*` (Derived Learning) and records
`knowledge_refs` so a verified lesson substitutes for rediscovery.

## Per-node routing and history

Each node is routed through the existing router (classification + symbolic
tier/effort) and honored learned-default logic. Historical evidence remains
advisory unless an approved learned default exists. Model capability resolution
is shown when `--surface` is given; an unavailable tier is reported, never
silently downgraded.

## Conditional escalation and infra failures

Conditional nodes (for example an architecture-resolution node) activate only on
signals like `REASONING_INSUFFICIENT` or architecture ambiguity — unrelated nodes
are not escalated. Infrastructure failures (`PROVIDER_503`, `RATE_LIMIT`,
`TIMEOUT`, `STALE_SESSION`, `AUTH_FAILURE`, `SAFETY_GUARD`) never reclassify a
task as needing a stronger model; the plan distinguishes `RETRY_INFRA`,
`RESTART_SURFACE`, `RESOLVE_ENVIRONMENT`, and `MODEL_ESCALATION`.

## User constraints and quota guards

`--min-tier`, `--max-tier`, `--local-only`, `--no-premium` constrain the plan, but
a cost preference can never violate a safety floor (a `CRITICAL` node stays at
`MAX`; the conflict is reported). `--prefer-model`/`--prefer-provider` are
informational only. Advisory diagnostics flag `ALL_TASKS_STRONG`, `ALL_TASKS_MAX`,
`ALL_CONTEXT_LARGE`, `EXCESSIVE_TASK_SPLITTING`, and `UNJUSTIFIED_EXPENSIVE_NODE`;
the qualitative quota view reports expensive vs cheap nodes, conditional expensive
nodes, and large-context nodes. No invented token or monetary savings.

## Outcome Learning readiness and boundaries

Each node carries `task_family` + routing recommendation so Phase 5B can connect
`task node -> execution -> learn start -> learn finish` via an opaque `decision_id`.
Planning records **no** synthetic outcomes and writes nothing.

Not implemented in V0.1: live multi-model execution, worker spawn, automatic
model/provider switching, automatic planner switching, token accounting,
retention, embeddings, and Graphify rebuild.
