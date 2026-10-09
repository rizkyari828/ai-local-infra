# Multi-model task execution (V0.1)

`./scripts/orchestrate run` executes a validated task graph node-by-node through a
provider-agnostic backend adapter, so a high-level objective uses the cheapest
sufficient model per node instead of running everything on one expensive model.

**Plan first, then confirm.** `run` refuses to launch anything without `--confirm`;
`--dry-run` shows resolved backends/models and the call bounds and launches nothing.
Planning and dry-run are local and persist nothing.

```bash
./scripts/orchestrate plan --objective "..."                 # inspect the plan
./scripts/orchestrate run  --objective "..." --dry-run       # resolve backends, no calls
./scripts/orchestrate run  --objective "..." --confirm       # execute (real backends)
./scripts/orchestrate run  --objective "..." --confirm --backend fake   # offline simulation
```

## Initiating surface vs execution backend

A task may start from any surface (OpenCode, Codex CLI/VS Code/Desktop), but that
does **not** force workers onto the same surface. Execution uses only backends that
can be controlled programmatically through a supported local CLI. There is **no**
UI automation (no clicking model dropdowns in Desktop/VS Code). VS Code/Desktop
remain initiating/manual surfaces unless a real supported execution interface exists.

## Backend adapter contract

A backend implements `capabilities()`, `resolve(...)`, `execute(...)`, `cancel(...)`,
and failure classification. It receives a bounded request: `execution_id`, `task_id`,
`task_family`, `task_class`, resolved `provider`/`model`/`native_variant`, the node
`objective_summary`, context manifest, dependency summaries, acceptance criteria,
and working directory — never the raw master conversation.

Discovered local support:

| Backend | Non-interactive | Model select | Effort select | Mode |
|---|---|---|---|---|
| OpenCode (`opencode run`) | yes | `-m provider/model` | `--variant` | SUPPORTED |
| Codex CLI (`codex exec`) | yes | `-m/--model` | `-c model_reasoning_effort=` | SUPPORTED |
| Codex VS Code / Desktop | no programmatic interface | — | — | initiating/manual |
| `fake` (offline) | n/a | simulated | simulated | SIMULATED |

If a required symbolic tier has no available or automatable backend, the node stops
with `NO_COMPATIBLE_EXECUTION_BACKEND` — never a silent downgrade.

## Sequential execution and git safety

V0.1 executes nodes in **topological order, sequentially** (no parallel workers —
the same Git working tree, simpler attribution, simpler dependency summaries). A
clean worktree is required by default (bounded `--allow-dirty` override); the
initial HEAD is recorded, files changed are tracked per node where practical, and
execution **never** commits, pushes, resets, rebases, or rewrites history.

## Context isolation and dependency contract

Each worker receives only its context manifest: objective summary, acceptance
criteria, compact dependency summaries, relevant knowledge references, and bounded
relevant files/config/tests. Full chat, the whole repository, all prior worker
outputs, and raw sessions are excluded. Each successful node returns a compact
result (`summary`, `decisions`, `files_changed`, `acceptance_results`,
`new_risk_signals`, `recommended_followups`); dependent nodes consume that summary,
never the raw worker response.

## Acceptance validation and failure classification

A worker saying "done" is not enough: acceptance criteria are validated where
deterministic checks exist, and only a bounded completion signal marks `SUCCESS`.
Failures reuse the existing taxonomy. `INFRA_*` categories (`PROVIDER_503`,
`RATE_LIMIT`, `TIMEOUT`, `STALE_SESSION`, `AUTH_FAILURE`, `SAFETY_GUARD`) never
escalate intelligence — they are bounded-retried, then reported. Only
`REASONING_INSUFFICIENT` escalates the affected node (BALANCED -> STRONG, etc.);
siblings are untouched and a `MAX` node stops/reports.

## Conditional nodes, integration, outcome learning

Conditional escalation nodes activate only on their signal (for example a
dependency reporting an architecture ambiguity); otherwise they are `SKIPPED` and
downstream nodes treat `SKIPPED` as satisfied. The final integration node runs last
with compact upstream outcomes and produces the consolidated result. Each executed
meaningful node records one bounded outcome row (`learn start/finish`,
`source=observed_runtime`); planning-only and dry-run produce none, and infra
failures are recorded as non-intelligence evidence.

## Resume, cancellation, call bounds

Run state is bounded and machine-local (`$LAI_STATE_DIR/orchestration/<run-id>/`,
excluded from Git; no raw prompts/chats/transcripts/credentials). `--resume`
reuses successful nodes, validating repository identity and HEAD; an incompatible
repository refuses resume rather than reusing stale results. Cancellation marks the
current node `CANCELLED` and the rest `NOT_RUN` (no fabricated outcomes). Before a
confirmed run, the call bounds are shown: planned AI nodes, conditional nodes, and
the bounded retry/escalation maximum; workers are instructed not to spawn nested
orchestrators/sub-agents, and the top-level orchestrator owns decomposition.

Not implemented in V0.1: parallel workers, automatic commit/push, learned-default
promotion, retention scheduler, central DB, embeddings, monetary-cost assumptions,
and UI automation.

## Execution guardrails

- **Execution intent** is `READ_ONLY`, `MUTATING`, or `MIXED`, derived from clear
  action semantics (word-boundary mutation verbs, negation-aware) — never from loose
  substrings, so "inspect the configured models" / "review the current config" stay
  `READ_ONLY`. `--read-only` forces the `READ_ONLY` policy; if the decomposition
  still yields a mutating node the plan is **invalid** with a
  `READ_ONLY_MUTATION_CONFLICT` diagnostic and no worker is launched (semantics are
  never silently rewritten).
- **Hard AI-call budget**: `--max-ai-calls N` is a hard ceiling shared across every
  real worker launch — initial nodes, same-tier retries, infra retries, reasoning
  escalation, conditional nodes, and final integration. Before each launch the
  remaining budget is checked; if exhausted the node returns
  `CALL_BUDGET_EXHAUSTED`, downstream nodes are `NOT_RUN`, and state is preserved.
  Planning/dry-run consume nothing. Bounds distinguish `planned_calls`,
  `possible_conditional_calls`, `possible_retry_calls` from `hard_max_ai_calls`,
  `actual_calls_used`, `remaining_calls`. `--resume` never resets the counter: the
  persisted `calls_used` and ceiling are reused.
- **Permission handling**: a backend that cannot proceed non-interactively because it
  requests approval is classified `PERMISSION_REQUIRED` (never
  `REASONING_INSUFFICIENT`) and is **not** intelligence-escalated. No automatic
  permission approval is ever performed; the CLI timeout remains the final safety
  net.
- **Read-only enforcement**: for `READ_ONLY` runs the orchestrator captures the
  worktree before each node and rejects any tracked/untracked change as
  `READ_ONLY_VIOLATION`, stopping downstream nodes and reporting the changed paths
  without any automatic reset/revert. Where a backend supports it, a safe sandbox is
  propagated (Codex: `-s read-only`); OpenCode has no read-only flag (only the
  dangerous `--auto`), so OpenCode relies on orchestration + worker instructions +
  the post-node Git check.
- `PERMISSION_REQUIRED`, `READ_ONLY_VIOLATION`, and `CALL_BUDGET_EXHAUSTED` are
  non-intelligence categories: they never weaken a model's standing, and nodes never
  launched due to budget/policy are not recorded as model failures.

## Live execution caveats (verified on Work Mac)

- OpenCode/Codex backends are **non-interactive**; a worker that tries to use tools
  requiring approval can block on a permission prompt. Bounded by a CLI timeout
  (default 300s, child terminated on timeout, classified `TIMEOUT`), but live nodes
  should stay read-only or run under a pre-approved permission policy. Prefer
  `plan` + `run --dry-run` first.
- Runtime verification on Work Mac: OpenCode + `deepseek/deepseek-flash` (variant
  `high`) executes end-to-end and records `observed_runtime` evidence. The default
  `FAST`/`BALANCED` resolution maps to a local Ollama model that is not running, and
  the `MAX` role maps to a placeholder backend model, so those are not live-verified.

