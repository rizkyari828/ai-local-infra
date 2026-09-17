# Teacher/student learning loop

This document describes the smallest useful foundation for a local Qwen student that
escalates to hosted paid teachers. It is metadata, telemetry, taxonomy, candidate
formats, docs, and lightweight metrics. It is **not** a router, proxy, fallback, or
training pipeline.

## Roles

Logical roles live in [`../models/roles.yaml`](../models/roles.yaml). They describe
intent only. No API keys are stored, and no hosted teacher is required for
local-ai-infra to operate.

| Role | Tier | Provider | Model reference | Purpose |
|---|---|---|---|---|
| `student` | local | Ollama | `qwen3.6:27b-coding` | Attempt every coding task first. |
| `teacher-cheap` | hosted | DeepSeek | `deepseek-coder` | First paid escalation for bounded, classified failures. |
| `teacher-premium` | hosted | Codex | `codex` | Second paid escalation for hard or high-uncertainty tasks. |

Escalation is always a human or explicit-policy decision in this milestone.

## The loop

```text
TASK
 -> QWEN (student)
 -> success: accept
 -> failure / uncertainty: teacher escalation (cheap, then premium)
 -> teacher solution
 -> tests / human review
 -> escalation telemetry (JSONL)
 -> learning candidate (observation -> reviewed -> validated)
 -> validated lesson / eval
 -> improved Qwen workflow
```

A paid teacher must produce more than a patch: it should leave structured evidence
that can improve the student over time.

## Observation is not a lesson

```text
observation
-> recurring pattern or meaningful failure
-> reviewed root cause
-> validated correction
-> lesson / eval / training candidate
-> explicit promotion (human approval)
```

Single errors are never promoted automatically. `ERROR != LESSON`.

## Escalation telemetry

Schema: [`../telemetry/schemas/escalation-run.schema.json`](../telemetry/schemas/escalation-run.schema.json).
Records are flat, null-capable JSONL written to `telemetry/escalations/runs.jsonl`
(ignored by Git). Core fields:

- Identity and grouping: `run_id`, `parent_run_id`, `timestamp_utc`, `task_class`.
- Student: `student_role`, `student_model`, `student_profile`, `student_outcome`.
- Failure: `failure_category` (taxonomy id).
- Teacher: `teacher_provider`, `teacher_model`, `teacher_reason`, `teacher_outcome`.
- Review: `human_review_outcome`, `correction_required`, `human_correction_count`,
  `tests_passed`.
- Candidates: `lesson_candidate`, `eval_candidate`, `training_candidate`.
- Cost and time: `duration_ms`, `cost_input_tokens`, `cost_output_tokens`,
  `cost_cache_hit_tokens`, `estimated_cost_usd`, `pricing_source`.

Never captured: full source, full prompts, secrets, credentials, `.env`, or
proprietary content without explicit project consent.

Record a sanitized observation:

```bash
./scripts/record-escalation \
  --run-id 6f6b2f6e-6b7c-4f6d-9c1e-2f3a4b5c6d7e \
  --task-class bounded-code-transformation \
  --student-model qwen3.6:27b-coding --student-profile coding-routine \
  --student-outcome failed --failure-category missed_existing_pattern \
  --teacher-provider deepseek --teacher-model deepseek-coder --teacher-reason failure \
  --teacher-outcome accepted --human-review-outcome accepted \
  --correction-required yes --human-correction-count 1 --tests yes \
  --lesson-candidate --eval-candidate --duration-ms 8400
```

## Failure taxonomy

A small, practical taxonomy lives in
[`../telemetry/taxonomy/failure-categories.yaml`](../telemetry/taxonomy/failure-categories.yaml)
and is mirrored in `scripts/common/learning.py`. Current ids: `syntax_or_compile`,
`test_failure`, `missed_existing_pattern`, `wrong_api_usage`, `tool_loop`,
`excessive_reread`, `context_loss`, `invalid_structured_output`, `environment_issue`,
`architecture_uncertainty`, `security_uncertainty`, `persistence_uncertainty`,
`human_rejected`, `unknown`.

Extend it through a reviewed edit. Readers treat unknown ids as strings so future
extension does not break the schema.

## Learning candidate

Schema: [`../datasets/schemas/learning-candidate.schema.json`](../datasets/schemas/learning-candidate.schema.json).
A candidate references one or more source runs and records the observed failure,
reviewed root cause, validated correction, validation evidence, teacher used, human
acceptance, proposed destination (`lesson`, `eval`, or `training-candidate`), and
promotion status.

Lifecycle: `observation -> reviewed -> validated -> promoted`, or `rejected`.
Recording a candidate changes nothing about lessons or datasets. A `promoted`
candidate must carry human acceptance, a reviewer, a root cause, a correction, and
validation evidence. A `training-candidate` requires explicit `project_consent`.

```bash
./scripts/record-learning-candidate \
  --source-run 6f6b2f6e-6b7c-4f6d-9c1e-2f3a4b5c6d7e \
  --task-class bounded-code-transformation \
  --observed-failure "Missed the repository's existing helper convention" \
  --failure-category missed_existing_pattern \
  --reviewed-root-cause "Student did not search for an existing helper before writing one" \
  --validated-correction "Require a pattern search before introducing a new helper" \
  --validation-evidence "regression eval qwen-regression-example passed" \
  --teacher-provider deepseek --teacher-model deepseek-coder \
  --destination lesson --status validated --reviewer "human-reviewer"
```

## Eval promotion

Converted failures become sanitized regression evals. See
[`EVAL_PROMOTION.md`](EVAL_PROMOTION.md). A harmless synthetic example lives in
[`../evals/regression/qwen-regression-example.yaml`](../evals/regression/qwen-regression-example.yaml).

## Metrics

Computed by `./scripts/learning-metrics` from local JSONL (no database):

- `paid_escalation_rate` = escalated tasks / total coding tasks.
- `local_success_rate` = Qwen-succeeded tasks / total coding tasks.
- `teacher_acceptance_rate` = accepted teacher solutions / teacher escalations.
- `first_pass_local_success_rate` = Qwen tasks accepted without teacher correction / total.
- `mean_human_corrections_per_task`.

Rates are `null` when their denominator is empty; they are never invented.

```bash
./scripts/learning-metrics
./scripts/learning-metrics --input telemetry/escalations/runs.jsonl --json
```

## Cost model

Cost is optional metadata for later analysis. Fields are `cost_input_tokens`,
`cost_output_tokens`, `cost_cache_hit_tokens`, `estimated_cost_usd`, and
`pricing_source`. Live pricing is never hardcoded as application truth; when exact
cost is unavailable, `estimated_cost_usd` stays null.

## What "Qwen improves" means now

Initially, improvement means:

- better instructions and prompts
- better context selection
- better profiles and stop rules
- better eval coverage
- better reviewed examples

It does **not** mean the Qwen weights change. LoRA/QLoRA or other fine-tuning is
deferred until a sufficiently large, reviewed, consented dataset exists and
evaluations justify it.

## Non-goals

No automatic router, no automatic DeepSeek/Codex fallback, no autonomous
self-training, no LoRA/QLoRA, no vector database, no PostgreSQL/ClickHouse, no
Redis/Valkey, no API server, UI, daemon, multi-agent platform, or RAG. Content
Studio is untouched and is not a runtime dependency of this work.
