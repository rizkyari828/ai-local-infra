# Learning loop

```text
RUN -> TELEMETRY -> EVAL/TEST -> REVIEW -> LEARNING CANDIDATE -> HUMAN APPROVAL -> PROMOTE
```

An error is an observation, not a lesson. Repeated observations may be grouped with the deterministic fingerprint of `client + model + operation + error category + error code`. A reviewer must establish the recurring pattern and root cause before creating a validated improvement candidate.

Promotion may update a model profile, prompt pattern, versioned eval, reusable generic lesson, or later an accepted training dataset. It must preserve evidence (run/eval references, versions, tests, corrections, consent, and reviewer decision). Agents cannot promote an unreviewed observation into a stable rule.

Positive training candidates require accepted output, human or human-plus-Codex review, explicit project consent, and a final approved outcome. Rejected/error samples stay separate for debugging/evals. Project source is not copied globally by default; project-only isolation is the safe default. v0.1 performs no training or autonomous self-improvement.
