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

The request also carries the **complete task contract** (task-specific objective,
operation, targets, permissions, dependencies, acceptance, capability requirement)
and the **validated context pack**; the final worker prompt is built from them, so
the context whose identity/provenance is recorded is the context actually delivered.
`working directory` is the single explicit **workspace root** for the run (used for
Git checks, context retrieval, deterministic tools, the worker `cwd`, and mutation
checks). See `docs/EXECUTION_CONTRACT.md`.

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

Process completion and task acceptance are **separate**. A backend exiting 0 with
output is a *completed process*, not an accepted task. `_acceptance` classifies each
node independently: `PASSED` only when every required criterion has explicit passing
evidence, `FAILED` when a required criterion fails, and `UNVERIFIED` when the process
completed but no independent acceptance exists. An `UNVERIFIED` node is neither
success nor failure: it cannot create positive quality evidence, cannot be cached, and
blocks downstream nodes/finalization. A model's prose ("I reviewed it.", "I cannot
complete the requested work.") is never acceptance. Failures reuse the existing
taxonomy. `INFRA_*` categories (`PROVIDER_503`,
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
excluded from Git; no raw prompts/chats/transcripts/credentials, and persisted result
tails are redacted). Writes are atomic. `--resume` reuses successful nodes only after
validating repository identity, HEAD, AND the plan/task-contract identity (a changed
objective or task contract refuses resume); a mutating node whose terminal status was
not durably recorded refuses resume as an uncertain mutation. An incompatible
repository or plan refuses resume rather than reusing stale results. Cancellation marks the
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
  escalation, conditional nodes, and final integration. The counter increments at the
  actual worker launch boundary: a node rejected before launch (missing/invalid
  context, policy rejection, cache hit, deterministic completion) consumes **zero**.
  Before each launch the
  remaining budget is checked; if exhausted the node returns
  `CALL_BUDGET_EXHAUSTED`, downstream nodes are `NOT_RUN`, and state is preserved.
  Planning/dry-run consume nothing. The budget bounds worker CLI launches made by this
  engine; it cannot bound model turns internal to an external coding-agent CLI, which
  the engine cannot observe. Bounds distinguish `planned_calls`,
  `possible_conditional_calls`, `possible_retry_calls` from `hard_max_ai_calls`,
  `actual_calls_used`, `remaining_calls`. `--resume` never resets the counter: the
  persisted `calls_used` and ceiling are reused.
- **Permission handling**: a backend that cannot proceed non-interactively because it
  requests approval is classified `PERMISSION_REQUIRED` (never
  `REASONING_INSUFFICIENT`) and is **not** intelligence-escalated. No automatic
  permission approval is ever performed; the CLI timeout remains the final safety
  net.
- **Read-only enforcement**: a `READ_ONLY` node keeps its restriction even inside a
  `MIXED` graph. Before each such node the orchestrator captures the worktree paths
  AND a content/state fingerprint (status + `git diff`), and rejects any change —
  including further edits to a file that was already dirty — as
  `READ_ONLY_VIOLATION`, stopping downstream nodes and reporting the changed paths
  without any automatic reset/revert. Residual limit: this is post-hoc detection, not
  a sandbox; ignored files and untracked content outside `git status` are not covered.
  Where a backend supports it, a safe sandbox is
  propagated (Codex: `-s read-only`); OpenCode has no read-only flag (only the
  dangerous `--auto`), so OpenCode relies on orchestration + worker instructions +
  the post-node Git check.
- `PERMISSION_REQUIRED`, `READ_ONLY_VIOLATION`, and `CALL_BUDGET_EXHAUSTED` are
  non-intelligence categories: they never weaken a model's standing, and nodes never
  launched due to budget/policy are not recorded as model failures.

## Capability-preserving backend failover (V0.1)

Symbolic capability (tier/effort) is separate from runtime availability. Registry
presence in `models/roles.yaml`/`config/model-capabilities.yaml` never implies a
model is runnable. Resolution selects only candidates that are **actually runnable**:
backend binary present, provider served by that backend, model installed/available,
effort/variant supported, backend not in cooldown — and that **preserve the symbolic
floor** (candidate tier/effort `>=` required).

Runtime availability states (local, not Git): `BACKEND_HEALTHY`,
`BACKEND_UNAVAILABLE`, `PROVIDER_UNAVAILABLE`, `MODEL_UNAVAILABLE`,
`ACCOUNT_MODEL_UNSUPPORTED`, `TEMPORARILY_RATE_LIMITED`, `UNKNOWN`. Health checks are
cheap (binary presence, local endpoints/inventory such as Ollama `/api/tags` and
Headroom `/health`); they never send a completion request and never consume the
AI-call budget.

The portable candidate map lives in `config/model-capabilities.yaml`
(`backends` = which providers each backend serves; `failover` = per-tier ordered
candidates). Transient health/cooldowns stay local.

- **Failover vs escalation**: infrastructure/availability failures
  (`RATE_LIMIT`, `PROVIDER_503`, `TIMEOUT`, `ACCOUNT_MODEL_UNSUPPORTED`,
  `MODEL_UNAVAILABLE`, `BACKEND_UNAVAILABLE`, `PROVIDER_UNAVAILABLE`) fail over to
  another capability-preserving candidate. `REASONING_INSUFFICIENT` is **not**
  failover — it is intelligence escalation. The two paths never mix.
- **Capability floor**: a fallback must satisfy the same tier/effort floor. If only a
  weaker candidate exists it is refused with `NO_CAPABILITY_PRESERVING_FALLBACK` —
  never a silent downgrade.
- **Circuit breaker**: a rate-limit/503 trips a bounded per-run cooldown for that
  backend/provider; subsequent nodes in the same run skip it without spending
  another call. No daemon, no permanent blacklist.
- **Call budget**: failover consumes the same hard `--max-ai-calls` budget; health
  checks cost zero; no failover can bypass the ceiling.
- **User constraints** (`--local-only`, `--no-premium`, `--prefer-provider`,
  `--prefer-model`) are honored as preferences/filters, never above safety. If a
  preferred backend is unavailable and no alternative is permitted, execution stops.
- **Same node only**: failover affects only the failing node; successful siblings are
  not rerun.

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

