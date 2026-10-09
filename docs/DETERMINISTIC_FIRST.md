# Deterministic-first / zero-additional-AI execution (V0.1)

AI must not be the default executor merely because AI is available. The engine now
chooses the cheapest **sufficient** executor: deterministic tools first, a direct AI
worker when an obvious bounded AI task exists, and an AI planner only when justified.

```text
OBJECTIVE
   |
   v
DETERMINISTIC PREFLIGHT
   +--> deterministic task       -> TOOL_ONLY        (0 additional AI calls)
   +--> obvious bounded AI task  -> direct AI worker (no AI planner)
   +--> complex / ambiguous      -> AI PLANNER when justified -> routed workers
```

## TOOL_ONLY

`TOOL_ONLY` is a symbolic executor **below** FAST. It is **not** an AI tier and never
appears in provider/model resolution. It represents deterministic execution using
bounded, whitelisted local operations:

`repository_state`, `git_head_matches`, `file_exists`, `config_parse`,
`config_value_matches` (exact equality only), `run_tests` (allowlisted command),
`finalize`.

A task is TOOL_ONLY only when its result can be determined **without semantic or
reasoning judgment**. Architecture decisions, ambiguous root-cause analysis, security
design, complex code changes, unclear requirements, semantic review, and unresolved
test diagnosis remain AI-routed. When uncertain, the safer AI classification wins —
cost optimization never weakens correctness.

`--read-only` runs never use mutating tool operations. The tool executor rejects
unknown operations, free-form shell strings, path escapes, secret-bearing arguments,
and non-allowlisted test commands. It preserves all existing Git safety (no reset,
rebase, destructive checkout, commit, or push).

## Zero additional AI vs true zero AI

- Invoked from OpenCode/Codex chat, the initiating controller LLM call has already
  happened. `TOOL_ONLY` therefore means **0 additional AI worker calls**.
- Invoked directly via CLI (or a future UI), `TOOL_ONLY` can achieve **true zero AI
  calls** for the whole objective. This distinction is stated explicitly to avoid
  misleading efficiency claims.

## Planner modes and bypass

`planner_decision` returns `NONE` (no planning needed), `DETERMINISTIC` (rule-based
decomposition), or `AI` (`PLANNER_REQUIRED`, with required symbolic tier/effort).
Simple deterministic objectives bypass planning entirely; a single obvious AI task
routes directly (no cheap planner call to repeat the classification); ambiguous or
security-sensitive objectives report `PLANNER_REQUIRED`. V0.1 makes **no live AI
planner calls** — it prepares the decision semantics.

## Budget-aware planning

`--max-ai-calls N` now influences planning, not only execution guardrails. Plans
account for `tool_only_nodes`, `direct_ai_nodes`, `planner_ai_calls`,
`conditional_ai_nodes`, `possible_retry_calls`, and expose
`minimum_required_ai_calls`, `planned_ai_calls`, and `hard_max_ai_calls`. If the
genuinely required AI calls exceed the hard maximum, the plan is rejected with
`INSUFFICIENT_AI_CALL_BUDGET` **before execution**.

## No-op detection and deterministic finalization

For tasks with a safe deterministic comparator (`config_value_matches`), desired
state is compared to actual state. If identical, the node returns
`NO_CHANGE_REQUIRED` and no AI worker runs. No-op detection never uses semantic
equivalence that would require reasoning. When all acceptance is deterministic the
final integration node is TOOL_ONLY; architectural synthesis keeps an AI finalizer.

## Tool → AI escalation

A deterministic check that passes → `SUCCESS` (or `NO_CHANGE_REQUIRED` when no change
is needed). A check that cannot be decided deterministically → `AI_REQUIRED` — the
tool execution itself was still TOOL_ONLY, and a tool failure is **never** recorded as
model reasoning failure.

## Failure taxonomy and outcome learning

`TOOL_EXECUTION_FAILED`, `DETERMINISTIC_CHECK_FAILED`, `NO_CHANGE_REQUIRED`,
`AI_REQUIRED`, and `INSUFFICIENT_AI_CALL_BUDGET` are non-intelligence outcomes. Tool
outcomes are recorded with `source=tool_only` and `provider`/`model`/`variant` null;
they never enter model-quality statistics, and future efficiency telemetry can count
avoided AI calls.

## User overrides

`--min-tier`/`--max-tier`/`--local-only`/`--no-premium` constrain AI workers and do
not force a TOOL_ONLY task to become AI unless AI execution is explicitly requested.
An explicit model request is preserved where safe, with a note that deterministic
execution was available.

Not implemented in V0.1: live AI planner calls, the single-master-prompt live smoke,
result cache, context retrieval optimization, efficiency telemetry, and retention.
