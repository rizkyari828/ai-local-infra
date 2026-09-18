# Runtime observability

Local-first telemetry for locating the real bottleneck in a task: inference, tools,
reads, context growth, retries, validation, provider failure, or escalation. There is
no tracing backend or database; the goal is a stable contract and lightweight
aggregation that the Agent Runtime (see [Agent runtime](AGENT_RUNTIME.md)) or a
developer can emit and read.

## Trace model

One logical task shares a single `trace_id` across every artifact:

```text
span events (telemetry/spans/runs.jsonl)
  + escalation summary (telemetry/escalations/runs.jsonl, trace_id)
  + learning candidate (datasets/candidates, source_run_ids -> run_id -> trace_id)
```

- `run_id` identifies the escalation summary row (one row per task).
- `trace_id` defaults to `run_id` and is the correlation key for spans.
- `parent_span_id` nests spans (for example a retry span under an inference span,
  or a DeepSeek escalation span under the Qwen span).
- `parent_run_id` links an escalation summary row to the run it escalated from
  (for example `run_id` = DeepSeek run, `parent_run_id` = Qwen run). See
  [Escalation](ESCALATION.md).

## Spans

Schema: [`../telemetry/schemas/span-event.schema.json`](../telemetry/schemas/span-event.schema.json).
A span is a *completed* stage measurement with a duration and completion timestamp.

- `stage`: `inference`, `tool`, `validation`, `review`, `escalation`, `retry`, `queue`, `other`.
- Model attribution: `model`, `provider`, `role`, `attempt`, `retry_count`.
- Usage: `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_miss_tokens`,
  `reasoning_tokens`, `total_tokens`, `cache_hit_ratio`, `context_size`,
  `context_utilization`. Unavailable values stay `null`; they are never invented.
- Provider cost (optional): `estimated_input_cost`, `estimated_cached_input_cost`,
  `estimated_output_cost`, `estimated_total_cost`, `pricing_profile`,
  `pricing_currency`, from [`../config/pricing.yaml`](../config/pricing.yaml)
  (empty by default, so cost stays unknown). See
  [Provider usage and cache telemetry](CACHE_TELEMETRY.md).
- Cache-prefix observability: `prompt_prefix_hash` is a digest of the first 2048
  prompt characters, never the prompt.
- Tools: `tool_kind` (`read`, `write`, `search`, `shell`, `test`, `git`, `other`),
  `tool_name`, `tool_outcome`, `target_hash`, `repeated`, plus bounded tool metrics
  `bytes_read`, `bytes_written`, `exit_code`, `result_count`.
- Failures: `error_category`, `error_code`.
- `limits_version` records which policy produced the limits in force.

Record a span:

```bash
./scripts/record-span --trace-id <uuid> --stage inference --duration-ms 18100 \
  --model qwen3.6:27b-coding --provider ollama --role student --attempt 1 \
  --input-tokens 48000 --output-tokens 6200 --context-size 49152 --context-utilization 0.98

./scripts/record-span --trace-id <uuid> --stage tool --duration-ms 120 \
  --tool-kind read --tool-name read_file --tool-outcome succeeded --target src/app.ts
```

`--target` is hashed with `hash_target`; the raw path is never stored.

## Trace report

```bash
./scripts/trace-report
./scripts/trace-report --json
./scripts/trace-report --trace-id <uuid>
```

Per trace it reports duration by stage, tool calls/failures/timeouts, file reads/writes,
repeated reads, attempts, retries, token totals, peak context, the escalation chain, and
warnings when configured thresholds are exceeded.

For cross-trace provider usage, cache effectiveness, latency, and estimated cost, use
the read-only `./scripts/cache-metrics` report (see
[Provider usage and cache telemetry](CACHE_TELEMETRY.md)).

## Task outcome

Schema: [`../telemetry/schemas/task-outcome.schema.json`](../telemetry/schemas/task-outcome.schema.json).
One compact row per completed task, written by `scripts/agent-run` and, for one-shot
`oc run`, by `scripts/record-opencode-outcome`, to
`telemetry/outcomes/runs.jsonl` (append-only, ignored by Git; `--outcome-output`
redirects it). It aggregates the runtime result and, when present, the escalation
outcome for the same `trace_id`; it is metadata only.

- Identity: `task_id` (the `trace_id`, the dedupe key) and `run_id` of the run that
  finished the task.
- Providers: `initial_*` describe the attempt that started the task; `final_*`
  describe the result that ended it (the teacher when an escalation ran, otherwise
  the student). Variants are best-effort and stay `null` when unknown.
- Outcome: `status`, `success`, `tests_passed`, `escalated`, `escalation_count`,
  `attempt_count`, `error_category`/`error_code`, `started_at`/`completed_at`, and
  `total_latency_ms`.
- Cost: `estimated_total_cost` sums each attempt's locally estimated cost
  (`pricing.estimate_usage_cost`, the same normalization spans use) and stays
  `null` unless every attempt cost is known. `provider_reported_cost` is separate
  and usually `null` for runtime-driven tasks.

Success semantics (never inferred from model text):

- `success` is `true` only when the final runtime result's `status` is `succeeded`
  (the teacher result when escalation ran, otherwise the student result);
  `failed`/`cancelled` are `false`, and an unknown status is `null`.
- `tests_passed` is only what the caller already passed in; the runtime does not
  parse output or run tests to populate it, so it is normally `null`.
- `escalated`/`escalation_count` reuse the existing escalation outcome; failed,
  blocked, and non-escalated tasks keep the student as the final provider.

Idempotency: files are append-only and duplicate `task_id` rows collapse to the
latest at read time, matching the span and OpenCode aggregators, so re-recording a
task never double-counts.

Report:

```bash
./scripts/task-outcomes
./scripts/task-outcomes --json
./scripts/task-outcomes --input telemetry/outcomes/runs.jsonl
```

It reports task count, success rate, escalation rate, average attempts, average
latency, average cost per task, and average cost per successful task, broken down
by final provider/model/variant and by task type. Unknown values are excluded from
rates and averages that need them; rates are `null` when their denominator is empty,
so an unknown `success` is never reported as a failure.

## OpenCode one-shot outcomes

The user-level `oc` wrapper auto-starts the gateway, imports OpenCode's aggregate
stats, and — for a one-shot `oc run ...` only — calls
`scripts/record-opencode-outcome` to append a Task Outcome V1 row. Interactive `oc`
is **not** finalized: a whole interactive session is not one task. The bridge reads
only structured session metadata from the OpenCode CLI; it never reads or stores
prompts, titles, messages, responses, source, or credentials.

OpenCode v2.0.x exposes execution status, not semantic task success, so the bridge
never invents success:

- Identity: the OpenCode session id, mapped to a deterministic UUIDv5
  (`opencode:session:<id>`) to satisfy the existing UUID `task_id` contract and make
  repeated finalization idempotent (a `task_id` already present is skipped). The raw
  session id is not stored.
- Execution state: `status` maps OpenCode's idle outcome
  (`succeeded`/`failed`/`interrupted`) to `succeeded`/`failed`/`cancelled`; a missing
  or unfamiliar outcome stays `unknown`.
- `success` is **always `null`** (execution completion is not task success), and
  `tests_passed` stays `null` because no per-run structured test signal exists.
  `error_category`/`error_code` stay `null` too.
- Providers: `initial_*`/`final_*` come from the session's model
  (`providerID`/`modelID`/`variant`); `provider_reported_cost` is OpenCode's own
  session cost. `estimated_total_cost` uses the shared pricing file only when a
  price is configured, and stays `null` otherwise (no double counting).
- `total_latency_ms` uses the session's `idle`/`updated` minus `created`; attempts,
  run id, task type, and escalation stay `null`/`false`.

This is the smallest reliable bridge: no OpenCode plugin, no second gateway, no
database or daemon, no model-output sentinel, and no routing change. Correlating an
outcome row to gateway inference spans is not done in V1 because OpenCode sends no
UUID session correlation header, so those links stay `null`.

## Limits policy

One place for conservative defaults: [`../config/limits.yaml`](../config/limits.yaml).

- `timeouts.model_timeout_seconds` bounds one inference attempt; `max_runtime_seconds`
  bounds the whole task across attempts.
- `tools` bounds tool execution: `tool_timeout_seconds`, `max_tool_calls`,
  `max_file_read_bytes`, `max_file_write_bytes`, `max_shell_output_bytes`,
  `max_search_results`. See [Tool runtime](TOOL_RUNTIME.md).
- `anomaly_thresholds` (`max_repeated_reads`, `max_retries`, `max_attempts`,
  `context_utilization_warn`) drive `trace-report` warnings, and `tools.max_tool_calls`
  warns when a trace exceeds its tool-call budget.

Load with `scripts.common.telemetry.load_limits()`. Missing file falls back to the same
defaults. Do not scatter hard-coded limits across scripts.

## Error classification

`RUNTIME_ERROR_CATEGORIES` (in `scripts/common/telemetry.py`) classifies runtime
failures for spans and future runtime code: `model_reasoning_failure`,
`provider_failure`, `provider_timeout`, `tool_failure`, `tool_timeout`,
`validation_failure`, `environment_failure`, `configuration_failure`, `resource_limit`,
`context_limit`, `user_cancelled`, `unknown`.

Only `model_reasoning_failure` should be treated as the model reasoning poorly. Provider,
tool, environment, configuration, and limit failures must not become negative learning
signal about the student. The escalation summary keeps the student failure taxonomy
(`failure_category`) separate from these runtime categories.

## What is measurable

- **Fully measurable**: total duration, stage durations (when a runtime emits spans),
  tool calls/failures/timeouts, file reads/writes, repeated reads via `target_hash`,
  attempts, retries, escalation chain and outcome, run-level duration/tokens/context.
- **Partially measurable**: token usage and context size/utilization only when the
  provider/runtime exposes them; reasoning tokens are often unavailable.
- **Not measurable here**: queue/wait times and provider retry storms. There is no
  queue, and the Agent Runtime bounds retries and total runtime by configuration
  instead of retrying indefinitely. Circuit breakers and backpressure remain
  intentionally deferred.

## Privacy

Never record prompts, source, secrets, credentials, or raw paths. The span and run
validators reuse the secret guard from `scripts/common/learning.py`, and `target_hash`
stores only a digest. Full prompts and source remain off by default; proprietary data
requires explicit consent.
