# Bounded escalation (Qwen -> DeepSeek)

The first hosted fallback provider and a small policy layer that decides when a
second provider is allowed. v0.1 supports exactly one hop from the local student
to the hosted fallback. It is not a router, planner, scorer, cost optimizer, or
retry framework.

```text
Qwen Local
  -> classified eligible failure (policy)
  -> DeepSeek V4.1 Flash (reasoning high)
  -> normalized result + existing telemetry
```

## DeepSeekProvider

`scripts/common/providers.py` adds `DeepSeekProvider` to the existing
`ModelProvider` registry (OpenAI-compatible `POST /chat/completions`).

- Credentials come only from an environment variable (`DEEPSEEK_API_KEY` by
  default). They are never stored in YAML, returned by `describe()`, logged, or
  written to telemetry.
- Configurable fields: `base_url`, `model`, `reasoning_profile` (default
  `high`), optional `api_key_env`, optional scalar generation options.
- The configured fallback is **DeepSeek V4.1 Flash**: `model = deepseek-flash`,
  `reasoning_profile = high`. The reasoning level is carried by the model choice
  in the existing request format (the provider sends `model` plus its normal
  scalar options); no new model abstraction or undocumented API field is added.
- Response normalization into `ProviderResponse`: content, provider/model,
  duration, input/output tokens, cached input tokens, reasoning tokens, and
  finish reason. Unavailable values stay `null`; they are never invented.
- Errors map onto the existing runtime categories (`configuration_failure` for a
  missing key or rejected credentials, `provider_timeout`, `provider_failure` for
  rate limits/5xx/connection problems, `validation_failure` for a 400).

Configuration (`config/runtime.yaml`):

```yaml
deepseek:
  provider: deepseek
  base_url: https://api.deepseek.com
  model: deepseek-flash
  reasoning_profile: high
  api_key_env: DEEPSEEK_API_KEY
```

## Policy

`scripts/common/escalation.py` defines `EscalationPolicy` (pure decision) and
`EscalationController` (decide and run at most one hop).

Defaults are conservative (`config/runtime.yaml` `escalation`):

```yaml
escalation:
  enabled: false
  mode: manual          # manual | automatic
  hosted_allowed: false
  fallback_provider: deepseek
  eligible_categories: [model_reasoning_failure, validation_failure]
  max_depth: 1
```

Eligibility by default:

| Category | Escalate? | Reason |
|---|---|---|
| `model_reasoning_failure` | yes | a stronger model may solve it |
| `validation_failure` | yes | output/schema quality may improve |
| `resource_limit` | no | current signals are task/tool budgets, not model-local |
| `environment_failure`, `configuration_failure` | no | infrastructure, not model capability |
| `tool_failure`, `tool_timeout` | no | tooling problem |
| `provider_failure`, `provider_timeout` | no | provider/network problem |
| `user_cancelled` | no | explicitly cancelled |

`eligible_categories` is configurable, but the default excludes infrastructure
and resource limits so provider/tool/environment failures never become hosted
calls or learning signal about the student. `max_depth` must be `1` in this
milestone (multi-hop is rejected by config validation).

## Decision outcomes

`maybe_escalate` returns a structured outcome, never a silent call:

```text
skip   student_succeeded | escalation_disabled | category_not_eligible
       depth_exceeded | cancelled | budget_exhausted
blocked  hosted_blocked | hosted_secret_detected
recommend  manual_mode
escalate   escalated
```

- **disabled** or **not eligible** or **hosted blocked** → no hosted call; the
  caller receives the reason.
- **manual** → `recommend` (a recommendation object); it does not call DeepSeek.
- **automatic** + eligible + allowed → one DeepSeek call.

## Trace continuity and run linkage

One task keeps one `trace_id`. The teacher run gets a new `run_id` and nests
under the student span via `parent_span_id`. The existing escalation telemetry
row links the runs through `parent_run_id`:

```text
trace abc
├─ Qwen inference span (run R0)
├─ existing tool spans (run R0)
├─ escalation row (run_id = teacher run R1, parent_run_id = R0, trace_id = abc)
└─ DeepSeek inference span (run R1, parent_span_id = Qwen span)
```

No second telemetry model is introduced.

## Runtime budget and cancellation

The controller derives a child session that inherits the same `started`,
`budget`, and cancellation event. The DeepSeek run therefore sees only the
remaining `max_runtime_seconds`: a task that already spent most of its budget
gives the hosted model a smaller deadline, and a negative/zero remainder is
reported as `budget_exhausted` without calling out. A set cancellation event
prevents escalation and stops a running call.

## Hosted-provider safety

Before any hosted call the controller reuses the existing secret guard
(`learning.contains_secret_like`) on the task prompt. Credential-like text is
blocked (`hosted_secret_detected`) and nothing is sent. Only the task prompt is
forwarded; environment variables, credentials, telemetry payloads, and unrelated
repository content are never attached. No new DLP system is added.

## Learning attribution safety

Escalation is recorded through the existing escalation schema with
`human_review_outcome = unreviewed`, `teacher_outcome = unknown`, and all
candidate flags `false`. A reasoning failure followed by a hosted success is an
*observation* that can later feed the reviewed pipeline; it never auto-promotes a
lesson, eval, or training candidate. Non-eligible (environment/provider/tool)
failures are not escalated at all, so they cannot be mis-attributed to the
student. The runtime-to-taxonomy mapping lives in `escalation.py`.

## CLI

```bash
# recommendation only (no hosted call)
./scripts/agent-run --escalate --escalation-mode manual --confirm-run

# automatic, hosted blocked unless explicitly allowed; may incur cost
./scripts/agent-run --escalate --escalation-mode automatic --hosted --confirm-run
```

`--provider` still selects the student provider; `--escalation-output` redirects
the escalation JSONL.

## Tests

`tests/test_escalation.py` uses fake providers and injected HTTP openers, so the
normal suite makes zero paid API requests. It covers provider config and
normalization, missing key, token metadata present/missing, timeout/failure,
cancellation, hosted secret protection, disabled/blocked/manual/automatic
policy, non-eligible failures, depth and cancellation protection, remaining
budget, both teacher outcomes, escalation telemetry, and learning attribution.
An opt-in live smoke test runs only with `LAI_DEEPSEEK_INTEGRATION=1`.

## Deferred

CodexProvider, multi-hop routing, autonomous planner/ReAct loops, provider
scoring, cost optimization, and multi-agent orchestration.
