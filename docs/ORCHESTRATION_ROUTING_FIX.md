# Exact orchestration routing V0.1

The controller resolves each explicitly requested role, provider, model, and native
effort before launching workers. It uses the existing `resolve_plan`, backend registry,
execution loop, failover helper, operational store, and Master B coordinator. No model
is called to route another model. This adds no framework, database, or provider ranking.

## Root cause audit

| Boundary | Existing behavior and correction |
|---|---|
| Requirements | `orchestrator` produces symbolic tier/effort recommendations. `routing.requirement` treats effort as a floor and can raise medium to high. Exact execution requirements are now separate from recommendations. |
| Provider/backend selection | `execution.resolve_plan` previously selected only tier candidates. It now resolves explicit requests across the controller's whole registered backend inventory. |
| Model identifiers | `discovery` owns exact aliases and catalogs. `Astra` is an alias for `gpt-6-astra`, supported by this machine's Codex account catalog; it is not passed as a CLI model ID. |
| Effort | Tier candidates could carry high even when another effort was desired. Exact requests require an advertised native variant; higher is not a substitute. Adapter resolution is checked for overrides. |
| Dispatch | `CliBackend` owns process parameters. Codex receives `-m` and `-c model_reasoning_effort=medium`, with the OpenAI provider pinned. Installed OpenCode accepts `--model provider/model#variant`; cwd supplies workspace, with no unsupported `--dir`/`--variant` run flags. |
| Fallback | Existing infrastructure failover and reasoning escalation were automatic. Explicit requests default to forbidden fallback; only named, capability-checked alternatives can replace a destination. Mutation replay restrictions still apply. |
| Actual metadata | CLI completion prose and parameters do not establish actual model/effort. CLI adapters leave those fields unknown. A backend adapter may supply them only from trustworthy execution evidence. |
| Accounting | The existing Run/Task/Attempt/Event store records requested/resolved values before invocation, then observed actual values and terminal state. Unavailable destinations create no provider Attempt. |
| Cross-provider integration | The programmatic dispatcher already exists in `execute_plan(backends=...)`. It now binds each task independently; an OpenCode worker never discovers or connects to the Codex reviewer. |

## Requirement interface

Attach `execution_requirement` to an AI node in a validated plan, or supply an
`execution_requirements` task-ID mapping to `execute_plan` / `lifecycle.run_direct`.
The CLI accepts the same JSON mapping through `--execution-requirements FILE` on
`run` and `direct`. Inspect the deterministic plan first to obtain task IDs.
Unknown task IDs, malformed requirements, and requirements on TOOL_ONLY nodes refuse
execution. `--backend fake` remains offline and cannot dispatch a real CLI.

```json
{
  "A1": {
    "role": "implementer",
    "requested_provider": "deepseek",
    "requested_model": "deepseek-flash",
    "requested_effort": "high",
    "required_capabilities": ["implementation"],
    "fallback_policy": {"mode": "forbidden"},
    "allowed_destinations": [{"backend": "opencode", "provider": "deepseek"}]
  },
  "A3": {
    "role": "reviewer",
    "requested_provider": "codex",
    "requested_model": "Astra",
    "requested_effort": "medium",
    "required_capabilities": ["review", "read_only"],
    "fallback_policy": {"mode": "forbidden"},
    "allowed_destinations": [{"backend": "codex", "provider": "codex"}],
    "require_actual_effort": true
  }
}
```

Task IDs above are examples, not instructions to manufacture nodes. Existing DAG
dependencies express implementation -> deterministic verification -> review. Mutation
needs the existing explicit workspace resource scope. Exact requirements do not change
the frozen planner's task semantics, machine policy, or local-only restrictions.

Native effort labels are matched exactly, case-normalized, within the selected model's
advertised inventory. Provider scales are not assumed equivalent. Unsupported medium
returns `BLOCKED_EFFORT_UNAVAILABLE`; an unknown model returns
`BLOCKED_MODEL_UNAVAILABLE`; an unavailable backend/provider returns
`BLOCKED_PROVIDER_UNAVAILABLE`. Existing hard-policy reasons remain authoritative.

An explicit alternative uses:

```json
{"mode":"allowed", "alternatives":[
  {"provider":"deepseek", "model":"deepseek-flash", "effort":"high"}
]}
```

Alternatives remain subject to required capabilities, allowed destinations, machine
policy, and local-only. They are tried in declared order. A result records the original
request, the destination actually resolved for that attempt, and a fallback index.
General symbolic recommendation/failover behavior remains unchanged for callers that
do not declare an exact requirement.

## Evidence and compatibility

The request, resolved model/native effort, and actual model/effort are separate fields.
Unknown actual values are stored as null (UNKNOWN), never copied from command arguments.
A reported mismatch is `EXECUTION_CONTRACT_VIOLATION`, not compliant execution.
`require_actual_effort` / `require_actual_model` demand observed proof: missing evidence
makes the result UNVERIFIED and ineligible for accepted reuse. Even without these flags,
effort verification is never reported VERIFIED without observed matching effort.

Master B work identity includes the complete execution requirement and validated allowed
bindings. Different exact effort or actual-verification requirements cannot join/reuse
one another. Explicitly allowed fallback results are compatible only with an authorized
binding; unknown actual properties cannot satisfy a strict proof requirement. Existing
single-flight, generation fencing, workspace exclusion, and uncertain-mutation recovery
are preserved. No frozen Shared Knowledge Contract identity rules were changed.

Review handoff includes bounded accepted result references, status/acceptance, changed
paths, and deterministic tool evidence. It excludes implementer free-form summaries,
scratchpads, arbitrary acceptance extras, and transcript history. Review objective and
acceptance criteria remain the review node's own; relevant source/diff must be supplied
through existing bounded, validated context references.

## Verification and live boundary

`tests/test_orchestration_routing.py` exercises configured fake adapters through the
production resolver, executor, claim store, CLI, and Direct Mode boundaries. Provider
calls in these tests are zero. The existing engine/KC/Master A/Master B regression suites
remain the acceptance baseline; the two inherited unrelated full-suite issues are not
fixed here.

The local Codex catalog advertises `gpt-6-astra` with `medium`, and installed `codex exec`
supports noninteractive model/config selection. OpenCode destinations/native variants
come from existing configured execution candidates or explicit available capability
records. These establish configured routes, not successful inference or account access.

A possible authorized smoke test is one read-only Codex `gpt-6-astra` request at medium,
with a tiny objective and bounded response. It consumes account quota (or provider cost
under API authentication); price is not inferred here. Process success and response
prove invocation only. Without trustworthy backend evidence, actual effort remains
UNKNOWN and strict effort verification remains UNVERIFIED. A full two-worker paid smoke
would require separate authorization and independent acceptance of implementer evidence.

No live smoke was run. Master B independent acceptance and Home WSL Master 0 portability
remain pending. No branch is merged, and Shared Knowledge Runtime is not started.

Interface references: [Codex configuration](https://developers.openai.com/codex/config-reference),
[Codex CLI](https://developers.openai.com/codex/cli/reference), and
[OpenCode CLI](https://opencode.ai/docs/cli/). Installed CLI help and local account catalog
were checked separately; documentation alone does not establish account availability.
