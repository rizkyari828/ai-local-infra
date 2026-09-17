# Eval promotion

Turn a real failed Qwen case into a sanitized, reusable regression eval. Promotion
is a reviewed manual process. Nothing is copied from a project automatically.

```text
real failure
-> classify (failure taxonomy)
-> sanitize / generalize
-> create eval case
-> validate expected outcome
-> add to eval suite
```

## Steps

1. **Classify.** Record or reuse an escalation record and set `failure_category` to a
   taxonomy id from [`../telemetry/taxonomy/failure-categories.yaml`](../telemetry/taxonomy/failure-categories.yaml).
2. **Sanitize and generalize.** Remove project names, paths, identifiers, source,
   prompts, and any proprietary detail. Rephrase the failure as a minimal,
   self-contained task that still exercises the same weakness. Do not copy source.
3. **Create the eval.** Add `evals/<group>/<eval-id>.yaml` with a `prompt`, an
   `assertions` block, a `task_type`, and a `sanitized: true` marker. Keep it
   deterministic and cheap enough to run locally.
4. **Validate the expected outcome.** Run it against the student profile. A regression
   eval should fail (or be uncertain) on the pre-correction student behavior and pass
   after the reviewed correction. If it cannot be validated, do not add it.
5. **Add to the suite.** Commit the eval. `./scripts/run-eval` discovers suites from
   `evals/*/*.yaml`, so a new file becomes runnable without code changes:

   ```bash
   ./scripts/run-eval qwen-regression-example \
     --model qwen3.6:27b-coding --profile coding-routine --confirm-run
   ```

6. **Link evidence.** Reference the eval id in the learning candidate's
   `validation_evidence`. Only then may the candidate reach `promoted`.

## Rules

- One generalized case per distinct root cause; group duplicates by fingerprint.
- No proprietary source, prompts, secrets, or customer data in an eval.
- Synthetic or fully generalized content only, unless an authorized reviewer marks a
  sanitized excerpt shareable.
- Eval definitions are versioned; raw run results stay ignored.

## Synthetic example

[`../evals/regression/qwen-regression-example.yaml`](../evals/regression/qwen-regression-example.yaml)
demonstrates the mechanism. It is harmless, synthetic, and derived from a taxonomy
category (`invalid_structured_output`) rather than any Content Studio implementation.
