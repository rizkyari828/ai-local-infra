# Teacher/student learning loop

This document describes the smallest useful foundation for a local Qwen student that
escalates to hosted paid teachers. It is metadata, telemetry, taxonomy, candidate
formats, docs, and lightweight metrics. A single bounded, opt-in Qwen -> DeepSeek
escalation is implemented in code (see [Escalation](ESCALATION.md)); there is still
no multi-hop router, proxy, autonomous fallback, or training pipeline.

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
proprietary content without explicit project consent. A minimal accidental-capture
guard rejects obvious credential-like values (`sk-...`, `AKIA...`, private-key
headers, and password/token/API-key assignments) in free-text fields before
persistence; it is not a comprehensive DLP system.

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
Requirements are enforced in code, not only documented:

- `observation`: records a failure; root cause, correction, and evidence are optional.
- `reviewed`: requires a reviewed root cause.
- `validated`: requires a reviewed root cause, a validated correction, and validation
  evidence.
- `promoted`: requires everything `validated` requires, plus human acceptance and a
  reviewer identity.
- `rejected`: recordable without promotion evidence.

A `training-candidate` destination always requires explicit `project_consent`. Invalid
transitions are refused with a non-zero exit code and an actionable message. Recording
a candidate changes nothing about lessons, datasets, or prompts; human approval remains
mandatory.

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

Computed by `./scripts/learning-metrics` from local JSONL (no database). Unknown or
unreviewed data is never counted as success.

| Metric | Numerator | Denominator | Unknown / unreviewed handling |
|---|---|---|---|
| `paid_escalation_rate` | rows with a teacher provider or model | all rows | no teacher means local and is counted in the denominator |
| `local_success_rate` | rows with `student_outcome = succeeded` | all rows | failed/uncertain/blocked still count against the rate |
| `teacher_acceptance_rate` | escalations with `human_review_outcome = accepted` | rows with a teacher provider or model | `unreviewed`, `needs-review`, and `rejected` do not count; a teacher result rejected by human review is not accepted |
| `first_pass_local_success_rate` | rows with `student_outcome = succeeded`, no teacher, `correction_required = false`, and `human_review_outcome = accepted` | all rows | unknown (`null`) `correction_required` and unreviewed rows do not count |
| `mean_human_corrections_per_task` | sum of `human_correction_count` where known | rows where `human_correction_count` is not null | unknown counts are excluded from the denominator; `null` when no count is known |

Rates are `null` when their denominator is empty; they are never invented.
`tasks_with_known_correction_count` is reported so the correction mean is transparent.

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

## Known v0.2 limitations (deferred)

- One escalation row summarizes one coding task. The `teacher_*` fields describe only
  the final teacher escalation for that task; a Qwen -> DeepSeek attempt -> Codex
  attempt chain is not stored as separate attempt rows, so intermediate-tier
  acceptance and cost are not yet analyzable.
- No `task_id`/trace-level aggregation; metrics operate on rows (one row per task).
- No attribution model distinguishing environment or infrastructure failures from
  student-capability failures beyond the escalation policy, which refuses to escalate
  them. `environment_issue` is recorded but still counts in the local success
  denominator.
- No multi-hop routing or autonomous promotion. One reviewed Qwen -> DeepSeek hop is
  implemented; Codex, scoring, and cost optimization are explicit non-goals.

## Non-goals

See the authoritative operating constraints in [`../AGENTS.md`](../AGENTS.md) and
the deferred-architecture list in [`ARCHITECTURE.md`](ARCHITECTURE.md). This
milestone adds only a bounded, opt-in single-hop escalation; it adds no multi-hop
router, autonomous self-training, or training execution, and leaves Content Studio
untouched.
