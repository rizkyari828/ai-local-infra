# Verified result handoff V0.1

This is an opt-in extension of the existing typed acceptance contract and sequential
controller. It adds no router, operational database, artifact service, or knowledge
runtime. Ordinary nodes and the complete-response `exact_output` comparison retain
their existing behavior. No live inference is part of this implementation.

## Integration points

The production path is `execute_plan(backends=...)` -> `_run_node` -> `CliBackend`
or another registered adapter. Before this change CLI completion supplied no typed
fact verification, and reviewer dependencies deliberately omitted free-form worker
summaries. A result reference alone could not deliver the captured answer.

The bounded changes connect capture validation to `_acceptance_evidence` before
Attempt settlement, retain verified evidence in `_finalize`, and publish through
the existing Master B claim/result transaction. `_dependency_facts` checks the accepted
source result and fingerprint before passing its evidence to the next task. Existing
context retrieval delivers it as a required DEPENDENCY_FACT under the untrusted DATA
header. Missing, redacted, truncated, or omitted required evidence blocks launch.
The source must also carry a `durable_result_id` assigned after completed publication
or accepted-result adoption. Passing fact checks without publication, including an
uncertain mutation outcome, cannot authorize the reviewer.

## Explicit node contracts

Attach these to AI nodes in a validated plan. The reviewer must depend on the named
implementation node and have the `reviewer` execution role. These are programmatic
plan fields; no automatic interpretation of objective prose is performed.

```json
{
  "task_id": "I",
  "acceptance_criteria": ["structured_result"],
  "structured_result": {
    "kind": "reverse_text_v1",
    "source": "ABCD",
    "allow_explanation": true
  }
}
```

```text
BEGIN_VERIFIABLE_RESULT
SOURCE: ABCD
REVERSED: DCBA
LENGTH: 4
END_VERIFIABLE_RESULT
```

V0.1 supports only this deterministic transformation: reverse the declared source
string and count its characters. Source is 1–128 ASCII letters, digits, spaces,
periods, underscores, or hyphens. Unknown contract keys/kinds are rejected before
execution; callers cannot supply expressions or override computed expectations.

```json
{
  "task_id": "R",
  "depends_on": ["I"],
  "acceptance_criteria": ["review_result"],
  "review_result": {"source_task_id": "I", "allow_explanation": true}
}
```

```text
BEGIN_REVIEW_RESULT
REVIEW_VERDICT: PASS
END_REVIEW_RESULT
```

`FAIL` is also a valid declared verdict, but fails acceptance. The engine records the
reviewer's parsed verdict separately in `result_verification.fields`, binds accepted
review evidence to the exact source result/evidence fingerprints, and does not treat
reviewer PASS as proof of source facts. Source facts must already pass deterministic
verification. `execution_status` and `completion_signal` preserve process completion
separately from acceptance and the parsed verdict. Requested/resolved/actual provider attribution is unchanged; absent
trustworthy actual model/effort evidence stays UNKNOWN.

## Parsing and acceptance

The adapter supplies complete captured response text and strictly boolean
`response_complete: true`. Exactly one block is required. Fields use `KEY: value`
on separate lines. Missing, extra, duplicated, malformed, or conflicting fields and
multiple/partial blocks fail. No expected value comes from the worker's assertion.
All other declared acceptance criteria still require their own typed evidence.

Missing block/capture or incomplete capture is UNVERIFIED. Explicit bad evidence is
FAILED. An exact successful block is accepted only when every required criterion
passes. Text outside the block is permitted only by `allow_explanation: true`;
additional fact declarations outside the block are rejected. Outside text is retained
in the evidence, not trimmed to manufacture an exact-output match. Mandatory inherited
guidance is not bypassed or edited.

For these new contracts OpenCode's JSONL adapter captures finalized text parts from
one completed assistant message, ending with `step_finish` / `reason: stop`. Reasoning,
tool events, and the event transcript are excluded. Multiple message identities,
duplicates, unknown/error records, and incomplete streams are unverified. Codex uses
its complete stdout capture. Existing ordinary/exact-output adapter behavior is unchanged.
This transport handling was checked against the installed OpenCode emitter and tested
offline; real provider serving behavior is not established by these tests.

## Bounded durable evidence

The complete visible response is limited to 2,048 UTF-8 bytes; the evidence envelope
is limited to 6,000 serialized characters. OpenCode's transport input is separately
limited to 131,072 bytes. Bounds are refusals, never silent truncation of required
evidence. Existing context and final-prompt budgets can impose a smaller ceiling.
Secret detection/redaction changes refuse evidence rather than claiming it verified.

Only successfully accepted opt-in results persist a `verified_result` envelope with
source task ID/semantic fingerprint, capture, declared contract, parsed/computed facts,
verification, acceptance, and a SHA-256 evidence fingerprint. The surrounding dependency
supplies the accepted result fingerprint/reference. Review results additionally record
the exact source task/result/evidence references they reviewed. No hidden reasoning,
transcript history, arbitrary worker extras, or unrelated tools are included.

The envelope is part of accepted result identity. Contract changes affect task, plan,
delivered-input, and work compatibility; changed capture changes descendant identity,
even if the structured facts are unchanged. Source task IDs are identity-bearing for
these new provenance contracts only. Ordinary task-ID equivalence is unchanged.

The existing operational store owns claims, fencing, durable Attempts and accepted
publication. Cache adoption revalidates evidence against the current contract; resume
refuses missing/corrupt evidence. Recovery can finish publication of an already durable
verified result without another implementer launch. Failed/unverified implementations
never authorize a reviewer Attempt. Mutation postconditions, workspace exclusion, and
uncertain-outcome recovery remain the existing Master B rules.

## Offline verification

`tests/test_verified_handoff.py` covers the full controller workflow with fake OpenCode
and Codex backends, the real CLI adapter path with stub subprocesses, parser attacks,
required context loss, secrecy/size limits, cache invalidation, resume/publication
recovery, and concurrent claims. Existing routing, acceptance-context, Master A,
Master B, contract and stabilization regressions remain required.

Live handoff is not verified. A later bounded smoke needs explicit authorization;
these changes do not authorize inference, fallback, or extra workers.

## Recovery acceptance revalidation

Every controller adoption of an eligible persisted result checks all declared criteria
with the existing typed acceptance rules, before independently checking its capture
proof. Stored false, missing, malformed, or conflicting evidence is not overwritten by
an otherwise valid response envelope. Exact-output reuse also checks the stored lossless
response digest. No worker is launched to repair invalid persisted acceptance.

The same check runs at cache lookup, claim acquisition, the final adoption race check,
and resume. Resume invalidation blocks the affected task and its transitive dependents;
unaffected successful historical tasks may remain. Existing CACHE_REJECTED, TASK_FAILED,
and RUN_FAILED events record refusal. The returned state, counters and durable terminal
statuses agree; historical Attempt rows are preserved and no refusal Attempt is created.
Valid resume, partial-publication recovery, identity, fencing and mutation exclusion
retain their existing behavior. Controller-path regressions are in
`tests/test_handoff_recovery.py`.
