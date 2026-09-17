# Local AI Infra v0.1

Reusable, local-first tooling for configuring, measuring, evaluating, and improving Ollama/Qwen workloads. It is the **HOW local AI runs** layer. Each product repository remains the **WHAT the agent needs to know** layer and keeps its own `AGENTS.md`, `QWEN.md`, checkpoint, lessons, architecture, and domain context.

This v0.1 is deliberately file-based. It has profiles, a model registry, hardware facts, safe apply/rollback, drift detection, resource checks, JSONL telemetry, small eval definitions, a minimal Agent Runtime with a bounded Tool Runtime and a conservative Qwen -> DeepSeek escalation policy, and learning dataset schemas. It is not a runtime dependency of Content Studio and is not an API, UI, daemon, database, RAG system, autonomous agent platform, or training pipeline.

## Quick start

```bash
./scripts/ai-profile status
./scripts/ai-profile use coding-routine --dry-run
./scripts/ai-profile use coding-routine
./scripts/ai-profile verify
./scripts/ai-profile rollback
./scripts/verify-environment
```

`use` validates the profile, compares desired and actual state, checks loaded heavyweight models, backs up changed active files, writes Qwen atomically, installs the Ollama override with `sudo` only if needed, reloads/restarts Ollama, health-checks it, and verifies drift. If post-apply validation fails, it attempts to restore the just-created backup. Reapplying a matching profile performs no writes or restart.

Backups and the active-profile marker live under `~/.local/state/local-ai-infra/` by default. They are intentionally outside Git. `studio` manages Ollama runtime settings only; it never changes the Qwen Code developer model.

## Benchmarks, telemetry, and evals

Inference is always explicit:

```bash
./scripts/benchmark-model --model qwen3.6:27b-coding --profile coding-large --confirm-run
./scripts/run-eval coding-small --model qwen3.6:27b-coding --profile coding-routine --confirm-run
```

Both append sanitized run records to ignored JSONL files. Full prompts and source code are not logged. Versioned schemas and golden eval definitions live in Git; machine-specific run history does not. The initial direct benchmark is recorded only as a qualified baseline, not a universal conclusion.

Learning follows `RUN -> TELEMETRY -> EVAL/TEST -> REVIEW -> LEARNING CANDIDATE -> HUMAN APPROVAL -> PROMOTE`. Only reviewed, accepted, consented data may become positive training candidates. See [Learning loop](docs/LEARNING_LOOP.md) and [Operations](docs/OPERATIONS.md).

The teacher/student escalation foundation adds logical model roles, escalation telemetry, a failure taxonomy, a learning-candidate schema, eval promotion, and lightweight metrics. It implements no router, fallback, or training:

```bash
./scripts/record-escalation --run-id <uuid> --task-class <class> \
  --student-model qwen3.6:27b-coding --student-outcome failed \
  --failure-category missed_existing_pattern \
  --teacher-provider deepseek --teacher-model deepseek-flash --teacher-reason failure
./scripts/record-learning-candidate --source-run <uuid> --task-class <class> \
  --observed-failure "..." --failure-category missed_existing_pattern \
  --destination lesson --status reviewed
./scripts/learning-metrics
```

See [Teacher/student learning loop](docs/TEACHER_STUDENT_LOOP.md) and [Eval promotion](docs/EVAL_PROMOTION.md).

## Runtime observability

Structured stage/span telemetry, a central limits policy, and a per-trace report make
runtime bottlenecks measurable instead of guessed:

```bash
./scripts/record-span --trace-id <uuid> --stage inference --duration-ms 18100 \
  --model qwen3.6:27b-coding --provider ollama --role student --attempt 1
./scripts/trace-report
./scripts/trace-report --json
```

See [Runtime observability](docs/RUNTIME_OBSERVABILITY.md) and
[`telemetry/schemas/span-event.schema.json`](telemetry/schemas/span-event.schema.json).

## Agent runtime v0.1

The minimal runtime executes one local-model task through a provider while
automatically emitting validated spans, enforcing configured limits, honoring
timeout/cancellation, and classifying failures. Ollama remains an independent
service; the runtime never embeds weights or downloads models.

```bash
./scripts/agent-run --health
./scripts/agent-run --provider fake --prompt "ping" --confirm-run --json
./scripts/agent-run --config config/runtime.yaml --prompt-file task.txt --confirm-run
```

Providers are configured in [`config/runtime.yaml`](config/runtime.yaml) and
resolved through a small registry, so Codex and future providers can be added
without changing the execution flow. Tests use a deterministic fake provider and
need no live model. See [Agent runtime](docs/AGENT_RUNTIME.md).

## Tool runtime v0.1

Tools give the agent bounded, observable hands for coding workflows without
autonomy. Every call is confined to an explicit workspace root, bounded by
configured limits, cancellable, and records a `tool` span automatically.

```bash
./scripts/agent-run --tool file.read --workspace /path/to/project \
  --params '{"path":"src/app.ts","start_line":1,"end_line":40}' --confirm-run --json
./scripts/agent-run --tool shell.exec --workspace . \
  --params '{"command":"python3 -m unittest discover -s tests"}' --confirm-run --json
```

Tools: `file.read`, `file.search`, `file.write`, `shell.exec`, `test.run`. They
share one `Trace`/session with inference, so `trace-report` shows per-tool timing,
tool calls, file reads, repeated reads, and shell failures. See
[Tool runtime](docs/TOOL_RUNTIME.md).

## Escalation v0.1 (Qwen -> DeepSeek)

The first hosted fallback provider plus a conservative, opt-in policy that runs
at most one Qwen -> DeepSeek hop for model-capability failures. Escalation is
disabled, manual, and hosted-blocked by default; environment/provider/tool
failures never escalate, and nothing is auto-promoted.

```bash
./scripts/agent-run --escalate --escalation-mode manual --confirm-run
./scripts/agent-run --escalate --escalation-mode automatic --hosted --confirm-run
```

DeepSeek reads `DEEPSEEK_API_KEY` from the environment only. The escalation keeps
the original `trace_id`, links runs through the existing escalation telemetry,
inherits the remaining runtime budget, and blocks credential-like prompts. See
[Escalation](docs/ESCALATION.md).

## Agent guidance

Stable operating rules for this solution live in [`AGENTS.md`](AGENTS.md). Task
prompts can stay minimal by following
[`docs/AGENT_TASK_TEMPLATE.md`](docs/AGENT_TASK_TEMPLATE.md).

For the local Qwen Code developer runtime — measured performance, verified
settings, diagnosed bottlenecks, and the recommended workflow — see
[`docs/QWEN_WORKFLOW.md`](docs/QWEN_WORKFLOW.md).

## Why files only?

One WSL2 workstation and one substantial GPU workload at a time do not justify PostgreSQL, Redis, a vector database, HTTP service, dashboard, or scheduler. JSON/JSONL keeps v0.1 inspectable, portable, and easy to replace when evidence supports a larger system.
