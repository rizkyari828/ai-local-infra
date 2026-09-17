# Learning loop

```text
RUN -> TELEMETRY -> EVAL/TEST -> REVIEW -> LEARNING CANDIDATE -> HUMAN APPROVAL -> PROMOTE
```

An error is an observation, not a lesson. Repeated observations may be grouped with the deterministic fingerprint of `client + model + operation + error category + error code`. A reviewer must establish the recurring pattern and root cause before creating a validated improvement candidate.

Promotion may update a model profile, prompt pattern, versioned eval, reusable generic lesson, or later an accepted training dataset. It must preserve evidence (run/eval references, versions, tests, corrections, consent, and reviewer decision). Agents cannot promote an unreviewed observation into a stable rule.

Positive training candidates require accepted output, human or human-plus-Codex review, explicit project consent, and a final approved outcome. Rejected/error samples stay separate for debugging/evals. Project source is not copied globally by default; project-only isolation is the safe default. v0.1 performs no training or autonomous self-improvement.

The concrete teacher/student escalation foundation — logical model roles, escalation telemetry, failure taxonomy, learning-candidate schema, eval promotion, and metrics — is described in [Teacher/student learning loop](TEACHER_STUDENT_LOOP.md), [Eval promotion](EVAL_PROMOTION.md), and `datasets/schemas/learning-candidate.schema.json`. Candidates are recorded with `scripts/record-learning-candidate`; observation is still not promotion, and human approval remains required. Lifecycle preconditions (`reviewed` root cause, `validated` correction plus evidence, `promoted` human acceptance plus reviewer) and the rule that unknown/unreviewed data never counts as success are enforced by `scripts/common/learning.py`, not only documented.
