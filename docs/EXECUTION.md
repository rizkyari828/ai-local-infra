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

