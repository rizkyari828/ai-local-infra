# Local AI Infra v0.1

Reusable, local-first tooling for configuring, measuring, evaluating, and improving Ollama/Qwen workloads. It is the **HOW local AI runs** layer. Each product repository remains the **WHAT the agent needs to know** layer and keeps its own `AGENTS.md`, `QWEN.md`, checkpoint, lessons, architecture, and domain context.

This v0.1 is deliberately file-based. It has profiles, a model registry, hardware facts, safe apply/rollback, drift detection, resource checks, JSONL telemetry, small eval definitions, and learning dataset schemas. It is not a runtime dependency of Content Studio and is not an API, UI, daemon, database, RAG system, agent platform, or training pipeline.

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
  --teacher-provider deepseek --teacher-model deepseek-coder --teacher-reason failure
./scripts/record-learning-candidate --source-run <uuid> --task-class <class> \
  --observed-failure "..." --failure-category missed_existing_pattern \
  --destination lesson --status reviewed
./scripts/learning-metrics
```

See [Teacher/student learning loop](docs/TEACHER_STUDENT_LOOP.md) and [Eval promotion](docs/EVAL_PROMOTION.md).

## Agent guidance

Stable operating rules for this solution live in [`AGENTS.md`](AGENTS.md). Task
prompts can stay minimal by following
[`docs/AGENT_TASK_TEMPLATE.md`](docs/AGENT_TASK_TEMPLATE.md).

## Why files only?

One WSL2 workstation and one substantial GPU workload at a time do not justify PostgreSQL, Redis, a vector database, HTTP service, dashboard, or scheduler. JSON/JSONL keeps v0.1 inspectable, portable, and easy to replace when evidence supports a larger system.
