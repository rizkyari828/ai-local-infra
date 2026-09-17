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
- `parent_span_id` nests spans (for example a retry span under an inference span).
- `parent_run_id` remains available for future multi-tier attempt chains.

## Spans

Schema: [`../telemetry/schemas/span-event.schema.json`](../telemetry/schemas/span-event.schema.json).
A span is a *completed* stage measurement with a duration and completion timestamp.

- `stage`: `inference`, `tool`, `validation`, `review`, `escalation`, `retry`, `queue`, `other`.
- Model attribution: `model`, `provider`, `role`, `attempt`, `retry_count`.
- Usage: `input_tokens`, `output_tokens`, `cached_input_tokens`, `reasoning_tokens`,
  `context_size`, `context_utilization`. Unavailable values stay `null`; they are never
  invented.
- Tools: `tool_kind` (`read`, `write`, `search`, `shell`, `test`, `git`, `other`),
  `tool_name`, `tool_outcome`, `target_hash`, `repeated`.
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

## Limits policy

One place for conservative defaults: [`../config/limits.yaml`](../config/limits.yaml).

- `timeouts.model_timeout_seconds` bounds inference CLI calls (`benchmark-model`, `run-eval`).
- `anomaly_thresholds` (`max_repeated_reads`, `max_retries`, `max_attempts`,
  `context_utilization_warn`) drive `trace-report` warnings.

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
