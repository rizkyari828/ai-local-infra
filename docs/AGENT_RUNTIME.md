# Agent runtime

The smallest portable runtime that executes local-model tasks through a provider
while automatically applying validation, limits, cancellation, telemetry, and
error classification. v0.1 is deliberately one task at a time: no planner,
router, hosted provider, queue, or multi-agent framework.

## Execution flow

```text
task
  -> AgentRuntime (trace/run context, validation, limits)
  -> ModelProvider (resolved from config)
  -> OllamaProvider
  -> http://localhost:11434/api/generate
  -> Ollama
  -> Qwen
  -> normalized RuntimeResult + automatic inference span
```

Ollama stays an independent inference service. This repository never embeds
weights, manages GPUs/CUDA/Metal, reimplements inference, or downloads models.

## Provider abstraction

[`../scripts/common/providers.py`](../scripts/common/providers.py) defines
`ModelProvider` with `execute(request)`, `health_check()`, and `describe()`.
A request carries `model`, `prompt`, `timeout_seconds`, optional scalar
generation `options`, `keep_alive`, and a cooperative `cancel` event. A response
carries only what the service reported; absent token/context values stay `None`
and are never fabricated.

Provider resolution is a small registry (`PROVIDER_BUILDERS`) plus
`build_provider(config, name)`, so a future `DeepSeekProvider` or
`CodexProvider` is added by registration, not by runtime branching.

## Configuration and portability

[`../config/runtime.yaml`](../config/runtime.yaml):

```yaml
provider: ollama
base_url: http://localhost:11434
model: qwen3.6:27b-coding
```

The same runtime code runs on macOS, Linux, WSL2, or a future GPU PC; only
`base_url`/`model` change. There are no OS-specific branches and no
machine-specific absolute paths. The file rejects credentials and must contain
no secrets.

## Health check

`OllamaProvider.health_check()` returns a structured status without downloading
anything: `healthy`, `unreachable`, `model_missing`, or `misconfigured`. The
runtime never calls health per inference.

```bash
./scripts/agent-run --health
./scripts/agent-run --health --json
```

## Limits, timeout, and cancellation

Limits come from [`../config/limits.yaml`](../config/limits.yaml); nothing is
hard-coded in the runtime.

- `timeouts.model_timeout_seconds`: per-attempt provider deadline (enforced).
- `timeouts.max_runtime_seconds`: whole-task budget across attempts (enforced).
- `anomaly_thresholds.max_attempts`: upper bound on retryable attempts (enforced).

The provider call runs under a deadline with cooperative cancellation. A timed-out
call fails as `provider_timeout`; a cancelled task fails as `user_cancelled` and
is never retried. Only explicitly retryable provider failures are retried, up to
the configured bound.

## Automatic telemetry

Callers never call `record-span` for normal execution. Every attempt emits one
validated `inference` span using the existing schema and secret guard:

```text
trace_id, run_id, span_id, stage, provider, model, role, attempt, retry_count
start/end (timestamp_utc + duration_ms), outcome error_category/error_code
input_tokens, output_tokens, cached_input_tokens, cache_miss_tokens, reasoning_tokens,
total_tokens, cache_hit_ratio, context_size, context_utilization
estimated_input/cached_input/output/total_cost, pricing_profile, pricing_currency
prompt_prefix_hash (digest of the first 2048 prompt characters)
limits_version
```

Prompts and response content are never persisted. One task keeps one `trace_id`
across all attempts, and `RuntimeResult` carries it so future escalation inherits
the same trace.

Provider usage is normalized once in `providers.normalize_openai_usage()` and
priced from [`../config/pricing.yaml`](../config/pricing.yaml) (empty by default,
so cost stays unknown). See [Provider usage and cache telemetry](CACHE_TELEMETRY.md).

## Error classification

Failures reuse `telemetry.RUNTIME_ERROR_CATEGORIES`. An Ollama connection problem
maps to `provider_failure` (or `provider_timeout`), a missing model to
`configuration_failure`, and never to `model_reasoning_failure`. This keeps
provider/environment problems out of the Teacher -> Student learning signal.
Only `model_reasoning_failure` should ever mean the model reasoned poorly.

## Fake provider and tests

`FakeModelProvider` is deterministic and needs no service:
`success`, `no_token_metadata`, `timeout`, `provider_error`, `unavailable`,
`invalid_response`, `model_missing`, `cancelled`. The normal suite runs without
Ollama. A live smoke test exists but is skipped unless
`LAI_OLLAMA_INTEGRATION=1` and a healthy endpoint/model are already present; it
never downloads a model.

## Future compatibility

Routing is not implemented. `RuntimeResult` already carries success/failure,
failure category, provider, model, duration, attempt count, and `trace_id`, which
is the information a future policy needs:

```text
Qwen local -> DeepSeek -> Codex
```

## Not in scope

No multi-agent teams, autonomous planning, DeepSeek/Codex integration, routing
policy, queues, brokers, Redis, vector stores, distributed tracing, GPU
scheduling, custom inference, training, or automatic model downloads.
