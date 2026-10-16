# Efficiency Telemetry V0.1

Deterministic, auditable, metadata-only telemetry answering, from structured data:
what actually executed, what was deterministic, what AI work was logically required,
what was reused, and how many real model invocations actually happened.

```text
ENTRY
├─ CHAT_HANDOFF
└─ DIRECT_CLI
       ↓
Deterministic Core
       ↓
TOOL_ONLY / NO-OP / CACHE / AI
       ↓
Operational Lifecycle Hooks
       ↓
Run / Task / Attempt / Event
       ↓
Efficiency Query Layer
```

## Source of truth

Telemetry is **derived**, not a second execution log. It reads:

- `orchestration.db` (Operational Data Contract v3): Run / Task / Attempt / RunEvent
- bounded context metadata: `$LAI_STATE_DIR/orchestration/<run>/context/<task>.json`
- bounded result-cache metadata referenced by task provenance
- (outcome evidence remains the learning store's authority; efficiency does not rewrite it)

New telemetry DB: **none**. `scripts/common/efficiency.py` computes summaries on demand.

## It measures facts, never hypothetical savings

Valid facts: `logical AI tasks = 5`, `cache-reused AI tasks = 3`, `real invocations = 2`,
`TOOL_ONLY tasks = 4`.

Not a fact: "TOOL_ONLY saved exactly 4 AI calls". A TOOL_ONLY task is not proven to have
an AI alternative, so no counterfactual saving is invented. The only "avoided" figure
exposed is **calls avoided by valid cache hit** — those tasks were already classified as
logical AI work, so reusing them provably avoided real invocations.

## Metric definitions

| Term | Meaning |
|---|---|
| logical AI task | a task whose executor semantics require AI |
| real AI invocation | one actual provider/backend model invocation attempt |
| cache-reused AI task | a logical AI task satisfied by safe result reuse, no invocation |
| planner AI call | an actual AI planner invocation (V0.1: none — decision semantics only) |
| retry | another real invocation for the same task via retry semantics |
| fallback | another real invocation for the same task through a replacement backend |

These categories are never conflated. One logical AI task with two attempts (Codex
`RATE_LIMIT` → OpenCode success) reports `logical_ai_tasks = 1`,
`real_invocation_attempts = 2`, `failed = 1`, `successful = 1`, `fallback = 1`. A cached
logical AI task reports `logical_ai_tasks = 1`, `cache_reused_ai = 1`,
`real_invocation_attempts = 0`.

`TOOL_ONLY` and `NO_CHANGE_REQUIRED` are measured independently and are **disjoint** in
the summary: `no_change_tasks` (any executor with `NO_CHANGE_REQUIRED`) is broken out of
the generic `tool_only_tasks` bucket. Neither is counted as an AI task. TOOL_ONLY is not
cache reuse, and cache reuse is not TOOL_ONLY. Tool operations are exposed by type
(`repository_state`, `config_parse`, `finalize`, …) without flooding low-level operations.

## Run metrics

`total_tasks`, `tool_only_tasks`, `no_change_tasks`, `logical_ai_tasks`,
`cache_reused_ai_tasks`, `fresh_ai_tasks`, `conditional_ai_tasks`,
`real_invocation_attempts`, `successful_invocations`, `failed_invocations`,
`planner_calls`, `retry_calls`, `fallback_calls`, `calls_avoided_by_cache`, plus budget
(`hard_max_ai_calls`, `planned_ai_calls`, `minimum_required_ai_calls`, `used_ai_calls`,
`remaining_ai_call_budget`). Values that are not known are reported as `null`, never
fabricated.

## Cache metrics

`hits`, `misses`, `rejected`, `written`, `invalid`, `bypassed_by_user`,
`incremental_nodes_reused`, and `calls_avoided_by_valid_hit`. Derived from bounded
`CACHE_HIT` / `CACHE_MISS` / `CACHE_REJECTED` / `CACHE_ENTRY_WRITTEN` events and their
bounded reason codes (`CACHE_INVALID`, `USER_FORCED_REFRESH`, …).

## Context metrics

From bounded ContextManifest metadata (never pack content): `pack_count`,
`total_chars`, `required_items`, `optional_items`, `optional_items_dropped`,
`deduplicated_chars`, `graphify_fallback_packs`, and `budget_classes`. No token estimate
is produced; no tokenizer dependency is added. Absent manifests report
`available: false` with zeros.

## Model / backend breakdown

Grouped real-invocation facts by `backend`, `surface`, `provider`, `model`,
`required_tier`, `required_effort`, `native_effort`, `normalized_effort`, `status`, and
`failure_category`. No model name is hardcoded into the schema; a future view such as
"OpenCode / DeepSeek / HIGH → 3 calls" is a query over these rows. Failover telemetry
exposes `failover_selected`, `retry_selected`, and failure counts by category; infra
failures are never reinterpreted as intelligence weakness.

## Planner bypass

`planner_mode` (`NONE` / `DETERMINISTIC` / `AI`) and `planner_calls` are exposed so
deterministic planner bypass is analyzable. No hypothetical saved planner call is
claimed without an explicit alternative.

## Entry modes and zero-AI semantics

| Entry mode | Meaning |
|---|---|
| `CHAT_HANDOFF` | an external/chat controller initiated the run; its cost is unknown to the engine |
| `DIRECT_CLI` | invoked directly through the local engine (no chat controller) |
| `AUTOMATION` | an automated wrapper |
| `API_OR_UI_FUTURE` | reserved for a future API/UI |

- `zero_additional_ai`: the local engine launched **0** worker/planner AI calls (a chat
  controller may still have spent calls the engine cannot see).
- `true_zero_ai`: `local_ai_calls == 0` **and** `controller_ai_calls == 0`.

For `CHAT_HANDOFF`, `controller_ai_calls` is `None` (unknown) unless a trusted
integration explicitly supplies it, so `true_zero_ai` is **not** claimed. For
`DIRECT_CLI`/`AUTOMATION`, the controller cost is `0` by definition of the local path.

## Aggregation safety

Metrics with incompatible semantics are never summed into a single "saved" number.
TOOL_ONLY counts and cache-hit counts are reported independently; only
`calls_avoided_by_cache` carries an avoided-call meaning, and only because the task was
already AI work. Machine (`machine_id`) and project (`project_id`/`repository_id`) are
aggregation boundaries, so a future cross-machine view can distinguish `work-mac` from
`home-wsl` while still recognizing the same logical project.

## RTK / Headroom non-overlap warning

Provider token/cache/cost measurements (`docs/CACHE_TELEMETRY.md`, RTK, Headroom) and
this orchestration efficiency layer are **separate dimensions**. They may overlap
(provider prefix caching can reduce tokens inside a call that orchestration also reused,
or a failover can spend tokens orchestration did not count). They are never merged into
one "savings" number unless non-overlap is mathematically proven. This module reports
orchestration-level counts only.

## Query layer

`scripts/common/efficiency.py` (no CLI parsing, no HTTP API):

- `get_run_efficiency(run_id)` — full per-run summary
- `get_recent_efficiency(limit, machine_id, project_id)` — recent runs + totals
- `get_model_usage(run_id, machine_id, project_id)` — grouped invocation facts
- `get_cache_efficiency(run_id, …)` — bounded cache totals
- `get_context_efficiency(run_id)` — bounded context metrics

CLI (uses the query layer, never parses logs):

```bash
./scripts/orchestrate efficiency <run-id>
./scripts/orchestrate efficiency --recent --limit 20
./scripts/orchestrate efficiency --model-usage
./scripts/orchestrate efficiency <run-id> --json
```

## JSON contract

```json
{
  "schema_version": 1,
  "run_id": "...",
  "entry_mode": "DIRECT_CLI",
  "tasks": {"total": 8, "tool_only": 3, "no_change": 1, "logical_ai": 4,
            "cache_reused_ai": 2, "fresh_ai": 2, "conditional_ai": 0},
  "ai": {"real_invocation_attempts": 2, "successful_invocations": 2, "failed_invocations": 0,
         "planner_calls": 0, "retry_calls": 0, "fallback_calls": 0, "calls_avoided_by_cache": 2},
  "zero_ai": {"local_ai_calls": 2, "controller_ai_calls": 0,
              "zero_additional_ai": false, "true_zero_ai": false}
}
```

## Privacy

Telemetry JSON and query results are metadata-only: never prompts, responses, terminal
transcripts, full context packs, diffs, tokens, or secrets. The secret guard still scans
bounded operational fields.

## Command surface (`scripts/efficiency`)

Read-only CLI over the query layer above. It adds no metric, no cost and no token
figure, and it never writes to the operational store, the learning store or the result
cache. Every field printed comes from `scripts/common/efficiency.py`.
(`scripts/orchestrate efficiency` remains as-is; this is the dedicated surface.)

```bash
./scripts/efficiency summary [--limit N] [--machine ID] [--json]
./scripts/efficiency run <run_id> [--json]
./scripts/efficiency models [--machine ID] [--json]
./scripts/efficiency cache [--machine ID] [--json]
./scripts/efficiency context <run_id> [--json]
./scripts/efficiency baseline [--limit N] [--machine ID] [--out PATH] [--json]
```

`--state-dir PATH` overrides the machine state root on every subcommand (default:
`$LAI_STATE_DIR`). `summary` is bounded to the module's own recent-runs window
(default `--limit 20`). `models` is not windowed: the module aggregates every recorded
attempt, and adding a window here would reshape its output.

## Pre-pilot baseline

`./scripts/efficiency baseline [--limit N] [--machine ID]` captures the current readings
into one JSON document so a pilot can be diffed against it later without a mapping layer.

A baseline covers **one window and one machine**, and both are recorded in the snapshot:

- `window.limit` - the run window used (default 20, the module's own default)
- `window.runs_included` - how many runs the window actually covered
- `window.machine_filter` - the machine filter applied, or `null`
- `window.selection` - how those runs were chosen (`most recent N runs by created_at`)
- `window.exceptions` - any block that could not share that window, with the reason

`--limit N` sets the window, and the same N is used for the `summary` and `cache` blocks,
so those two always describe the same runs. `--machine ID` narrows every block to one
machine. `--limit` picks the most recent N runs first and `--machine ID` then narrows that
set, so `window.runs_included` can be smaller than `window.limit`. A block that cannot be
aligned without reshaping a module's public output is listed in `window.exceptions` rather
than silently mixed in; today the `models` block is declared there, because the module
aggregates every recorded attempt and exposes no run window.

The snapshot also says which machine it describes:

- `machine_id` / `project_id` - the identity the baseline resolved, or `null`
- `machine_resolution` / `project_resolution` - `mode` (`explicit`, `dominant` or
  `unresolved`), the `covered_runs` breakdown, and a note

A `null` is never bare: it always arrives with the breakdown that explains it (for
example a window in which every run reports `unknown`). No identity is invented - every
value comes from a run row.

Remaining blocks: `schema_version`, `captured_at` (UTC, ISO 8601), `summary`, `models`,
`cache` (the module's own output, unreshaped), `source` (state directory and runs
included) and `not_included`.

It contains **no** tokens, cost, RTK savings, Headroom savings, prompts, responses or
transcripts - only counts, identifiers and timestamps. The default output path is
`$LAI_STATE_DIR/efficiency/baseline-<UTC>.json`; `--out PATH` writes elsewhere. The
snapshot is operator state, not repository content: it is never committed and never
added to a fixture set.

### Comparing two baselines

A comparison is valid only when the two snapshots are comparable: `window.limit`,
`window.machine_filter` and `window.runs_included` must line up, and `window.exceptions`
must be the same on both sides. Two baselines taken at the same limit over unchanged
state differ only in `captured_at`.

The **on-disk run-directory count is not a run count.** `orchestration/` holds many
directories, and most are not rows in the operational DB. `orchestration.db` is the
authority, and `window.runs_included` counts its rows - never the directories on disk.

These additions defer nothing new and move nothing out of the section below: automatic
cross-machine aggregation/UI, HTTP API/WebSocket/SSE, a persisted telemetry warehouse,
cost/token estimation, retention scheduling, and unifying RTK/Headroom savings with
orchestration savings all remain deferred.

## Deferred

Automatic cross-machine aggregation/UI, HTTP API/WebSocket/SSE, a persisted telemetry
warehouse, cost/token estimation (needs a real tokenizer/pricing decision), retention
scheduling, and unifying RTK/Headroom savings with orchestration savings.
