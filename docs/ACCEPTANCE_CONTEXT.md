# Exact output acceptance and engine context

The Astra smoke's extra footer was present in the saved assistant response and raw
stdout. It was not added by capture formatting. The harness declared no acceptance
criteria, so `ACCEPTANCE_UNVERIFIED` was correct. A local equality check existed but
was not connected to the engine's typed acceptance evidence.

## Opt-in deterministic acceptance

Declare an expected literal and the existing named acceptance criterion on an AI node:

```json
{
  "acceptance_criteria": ["exact_output"],
  "exact_output": "TOKEN"
}
```

This does not infer acceptance rules by parsing prose. The adapter supplies the complete
captured text as `response_text` and a strictly boolean `response_complete: true`.
`CliBackend` captures the entire successful stdout; its bounded display summary is not
validation input. A fake or programmatic adapter must implement the same capture contract.

The engine computes `acceptance_results.exact_output` using exact string equality, then
uses the existing typed acceptance machinery. A worker's own `exact_output: true` cannot
override a mismatching or missing capture. Other required criteria still need their own
valid evidence. Missing/incomplete capture is UNVERIFIED; extra text is ACCEPTANCE_FAILED;
an exact match is accepted only if every other required condition passes.

No whitespace, footer, line, prefix, or matching-token extraction occurs. In particular,
CLI stdout's terminal newline is part of the captured response: if required, declare it
explicitly as `"exact_output": "TOKEN\n"`. This is not a hidden newline allowance.
Malformed expected values refuse execution; a declared criterion without an expected
literal blocks before launch. Ordinary execution without this criterion is unchanged.

Expected literal text participates in plan, delivered-input, and task/reuse identity.
A lossless response digest also participates in accepted result identity so whitespace
differences cannot collapse downstream dependency fingerprints. Complete response text
is used in memory for validation, not added to persisted execution results. Failed and
unverified outcomes remain ineligible for Master B accepted reuse.

## Compatible engine instructions

For exact-output tasks the engine omits its generic structured-result instruction and
renders the expected literal as a JSON string, preserving whitespace unambiguously.
Objective, permissions, safety restrictions, dependencies, and acceptance remain present.
Other tasks retain the existing structured-result instruction.

This is not permission to bypass system/developer instructions. Global AGENTS, Ponytail,
CLI configuration, hooks, and mandatory guidance are not disabled or edited. The observed
smoke inherited a Ponytail rule requiring a skipped-work/risk footer. If that mandatory
rule remains active, it conflicts with literal-only output: a footer must fail exact
acceptance, not be trimmed away. The engine cannot reliably infer all dynamically injected
CLI guidance from node metadata, so it does not claim a general prelaunch proof of
compatibility. Caller-visible missing/invalid literal specifications are detected before
launch. A live literal-output handoff remains unverified under the observed conflicting
guidance. No additional live smoke is authorized by this change.

## Proven context redundancy

The prompt already renders its authoritative objective and acceptance criteria. Identical
copies of those values in OBJECTIVE_SUMMARY / ACCEPTANCE_CRITERIA context items are now
omitted only from the rendered DATA block. Distinct content is retained. If no other DATA
remains, its empty wrapper is omitted too.

Context packs and manifests are not mutated: source metadata, requiredness, fingerprints,
knowledge content/provenance, dependency evidence, and context identity are retained.
No repository sources, knowledge requirements, machine policy, or mandatory guidance are
removed. Tools are not newly exposed, and provider-internal tool definitions are not
changed. This introduces no Shared Knowledge Runtime or routing redesign.

No token savings are asserted. The saved smoke recorded 21,294 input tokens (13,184
cached), 90 output tokens including 61 reasoning tokens. Its CLI figure of 8,200 equals
uncached input plus output. Per-instruction/tool/hook token costs were not reported;
the large input cannot be attributed to the 935-character engine prompt alone.

Offline coverage is in `tests/test_acceptance_context.py`; existing routing, Master B,
Master A, Shared Knowledge Contract, and Master 0 tests remain the regression boundary.
Requested/resolved/actual model and effort attribution is unchanged; actual Astra effort
is still UNKNOWN without trustworthy provider evidence.
