# Operations

## Python runtime

`scripts/bootstrap` is a portable `sh` launcher over the standard-library-only
Python CLI. It requires **Python >= 3.11** because `scripts/common/bootstrap.py`
uses stdlib `tomllib`; Python 3.9/3.10 are rejected before import. The launcher
finds an installed interpreter (`python3`, then `python3.13`/`python3.12`/
`python3.11`), forwards all arguments and the exit code unchanged, never mutates
`PATH`, and never installs or downloads a Python. Set `LAI_PYTHON=/path/to/python3`
to force a specific interpreter. Running the module directly under an unsupported
Python fails fast with a clear "requires Python >= 3.11" message.

The user-facing CLIs `./scripts/orchestrate`, `./scripts/learn`, `./scripts/models`,
and `./scripts/route` use the same shared selector (`scripts/common/launch`) instead
of a bare `#!/usr/bin/env python3` shebang. They pick a supported interpreter even
when `env python3` is older (for example macOS Python 3.9) as long as any
`python3.13`/`python3.12`/`python3.11` or `LAI_PYTHON` is installed, so the commands
work unchanged across machines. When no interpreter >= 3.11 exists the launcher
exits 127 with a clear message (minimum version plus `LAI_PYTHON` guidance) rather
than an import error.

## Environment verification

`./scripts/verify-environment` validates profiles, schemas, and limits (structural
failures) and reports tool versions. On a machine that configures local inference,
it also probes the local Ollama endpoint `http://127.0.0.1:11434/api/tags`; there an
unreachable Ollama is an unrelated runtime-service failure (exit 1), **not** a
Multi-Machine Sync blocker — `bootstrap status`/`apply`/`doctor` do not require
Ollama. On a machine that declares local inference disabled by policy
(`local_inference: NOT_CONFIGURED_BY_MACHINE_POLICY` or `provider_availability.ollama:
false`), the Ollama health probe is skipped (`Ollama: NOT_CONFIGURED_BY_MACHINE_POLICY`,
`Ollama health: SKIPPED_BY_POLICY`) and its absence is never an environment failure.
Machine identity is `LAI_MACHINE`, or the descriptor whose declared `repo_path` is
this repository; health checks are otherwise never weakened.

## Inspect and apply

Start with `./scripts/ai-profile status` or compare a profile explicitly with `--profile coding-routine`. `status` reports `MATCH`, `DRIFT`, or `UNKNOWN` for Qwen and Ollama and never repairs anything.

`./scripts/ai-profile use coding-routine --dry-run` prints active/desired profiles, safe current/desired projections, changed files, loaded-model conflicts, and restart need. It never prints the Qwen `env` object or provider credentials.

Real apply validates first. Qwen changes remain user-level. If the Ollama override differs, `sudo` is used only for install, daemon reload, restart, and a possible system-file restore. A different loaded heavyweight model stops apply; `--unload` explicitly authorizes `ollama stop` for conflicts. The command does not preload the requested model.

Backups are timestamped beneath `~/.local/state/local-ai-infra/backups/`. `rollback --dry-run` previews the newest backup; `rollback` restores it and restarts Ollama only if the system override was part of that backup. Keep this directory private because Qwen backups may contain credentials.

## Model routing (recommendation only)

`./scripts/route --task "<task>"` prints a deterministic, offline recommendation
of the cheapest sufficient tier/effort (mechanical / normal / correctness-sensitive
/ critical) from `config/model-routing.yaml`; `--json` emits the routing evidence
fields. It never calls a model, never changes a user-selected model, and stores no
quotas. See [Model routing](MODEL_ROUTING.md).

## Model capability discovery (read-only)

`./scripts/models detect` / `./scripts/models list` report which providers/models
are known, available, or verified and which effort variants they expose, from
local config and an optional local snapshot. Catalog-only: no model/provider call,
no tokens, no downloads, no config mutation. See
[Model capability discovery](MODEL_CAPABILITIES.md).

## Derived learning (local evidence + shared knowledge)

`./scripts/learn` records bounded local task outcomes into a machine-local SQLite
ledger (`$LAI_STATE_DIR/learning/evidence.db`, never committed) and imports the
Git-shared historical seed idempotently. Reviewed, distilled knowledge lives in
`knowledge/*` and is shared across OpenCode and Codex. No prompts, secrets, or
sessions are stored, and nothing auto-promotes. See
[Derived learning](DERIVED_LEARNING.md).

## Task graph orchestration (plan-only)

`./scripts/orchestrate plan --objective "<one objective>"` decomposes a high-level
objective into a small DAG of task nodes with per-node symbolic routing and context
budgets. It is deterministic-first: known deterministic objectives plan as TOOL_ONLY
(0 additional AI calls), simple AI objectives route directly (no AI planner), and
only ambiguous/security objectives report `PLANNER_REQUIRED`. V0.1 plans only: no
model call, no worker, no persistence, and the raw objective is never stored. See
[Orchestration](ORCHESTRATION.md) and [Deterministic-first execution](DETERMINISTIC_FIRST.md).

## Multi-model execution (gated)

`./scripts/orchestrate run --objective "<one objective>" --confirm [--backend fake|opencode|codex|auto]`
executes the validated graph sequentially, routing each node independently. It
requires `--confirm` (or `--dry-run`), one node at a time, with minimum-sufficient
context and bounded retry/escalation. `--read-only` enforces a hard read-only
policy, and `--max-ai-calls N` sets a hard ceiling on real worker launches. See
[Multi-model execution](EXECUTION.md).

## Operational data contract

Orchestration lifecycle (Project/Run/Task/Attempt/RunEvent) is persisted to a
machine-local SQLite contract at `$LAI_STATE_DIR/orchestration/orchestration.db`,
separate from learning evidence and shared knowledge. See
[Operational data contract](OPERATIONAL_DATA_CONTRACT.md).

## Context retrieval

AI tasks receive a bounded, provenance-aware context pack (minimum sufficient context)
built deterministically, with symbolic budgets in `config/context-budgets.yaml`. See
[Context retrieval](CONTEXT_RETRIEVAL.md).

## Runtime doctor (read-only)

`LAI_MACHINE=work-mac ./scripts/bootstrap doctor` answers "is this machine ready for AI coding right now?" without mutating anything: no apply, no backups, no graph rebuild, no process start/stop, no agent launches. It reports `PASS`/`WARN`/`FAIL`/`SKIP` for repository, bootstrap, OpenCode, Codex, RTK, Graphify, Headroom, and the Codex Headroom MCP. `--json` emits a stable machine-readable report. Exit codes: `0` no `FAIL`, `1` some `FAIL`, `2` config/exec error. The tested baseline lives in `config/tool-baseline.yaml`; exact version match is `PASS`, any other version is `WARN` (never auto-upgrades/downgrades), and runtime AUTO is not re-proven. See [Multi-machine sync](MULTI_MACHINE_SYNC_V0_1.md) §18.

## Telemetry storage and hygiene

v0.1 writes append-only JSONL. Each event is a flat, typed record with stable IDs, UTC time, event type, dimensions (project/client/model/profile/operation), numeric measures, outcome fields, and explicit runtime/config versions. The teacher/student extension adds `telemetry/escalations/runs.jsonl` with the same flat, null-capable shape, defined by `telemetry/schemas/escalation-run.schema.json`. See [Teacher/student learning loop](TEACHER_STUDENT_LOOP.md). This shape is intentionally friendly to columnar conversion:

```text
JSONL (capture/source of truth for v0.1) -> Parquet (later compact analytics files) -> DuckDB (local ad-hoc analytics)
```

DuckDB is not installed or required. A future user could query JSONL directly or converted Parquet:

```sql
-- Illustrative future command, not a v0.1 dependency.
SELECT model, profile, count(*) AS runs, avg(duration_ms) AS avg_ms
FROM read_json_auto('telemetry/runs/*.jsonl', format = 'newline_delimited')
WHERE status = 'succeeded'
GROUP BY model, profile;

SELECT model, quantile_cont(duration_ms, 0.95) AS p95_ms
FROM read_parquet('telemetry/parquet/*.parquet')
GROUP BY model;
```

Avoid dynamic nested payloads in core events. Add fields through a new schema version, keep old readers possible, and use stable null-capable columns rather than changing meanings.

Retention guidance:

- Raw tool/runtime events: short-to-medium retention, then aggregate or delete deliberately.
- Recurring errors: group/deduplicate by fingerprint; retain reviewed pattern summaries longer than duplicates.
- Benchmark/eval summaries: long-lived with model/profile/hardware/version evidence.
- Accepted reviewed training candidates and decisions: long-lived and isolated by consent.
- Full prompts/source: not captured by default.
- Secrets, keys, tokens, `.env`, SSH material, personal files, and production-sensitive data: never intentionally captured.

## Future storage split (documented, not implemented)

- PostgreSQL may later hold operational/knowledge metadata, evaluations, accepted training examples, review/promotion decisions, and validated knowledge metadata.
- ClickHouse is only justified for future high-volume telemetry after real multi-agent or multi-machine event volume demonstrates the need.
- PostgreSQL + pgvector is only justified if historical knowledge grows enough to require semantic retrieval.
- Redis/Valkey remains deferred until caching, coordination, or rate-limiting has a measured need.

JSONL remains authoritative for this milestone. There is no migration code or runtime dependency for any future store.

## Failure handling

If apply validation fails after writes, the command attempts immediate restore. Inspect both the error and actual state with `status`; do not assume rollback succeeded after a power loss or failed `sudo`. `verify` checks repository syntax, active-state drift, and Ollama health. It does not run inference.
