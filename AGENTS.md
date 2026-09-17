# Agent guide — Local AI Infra

Concise, authoritative operating rules for this solution. Detailed mechanics live
in the linked docs; do not repeat these rules in every task prompt.

## Scope and boundary

- This solution owns **how** local AI runs: profiles, model/hardware metadata, safe
  apply/rollback, verification, benchmarks, telemetry formats, evals, and reviewed
  learning-data conventions.
- Product repositories own **what** an agent must know. Do not copy project memory,
  prompts, source, credentials, or private context here.
- Do not modify `../ai-studio/` unless a task explicitly scopes it.
- Keep changes inside `local-ai-infra/`; keep repository-wide files at the root.

## Model roles

- `student`: local Qwen coding model, the default worker that attempts every task.
- `teacher-cheap`: hosted DeepSeek coding model, the first paid escalation.
- `teacher-premium`: Codex or another strong hosted model, the second paid escalation.
- Roles describe intent only. See `models/roles.yaml` and `docs/MODEL_ROUTING.md`.

## Learning rules

- `ERROR != LESSON`. A failure is an observation, not knowledge.
- Lifecycle: `observation -> reviewed -> validated -> promoted`, or `rejected`.
  Enforced in `scripts/common/learning.py`.
- Promotion always requires the existing evidence and human-review rules. Unknown or
  unreviewed data never counts as success.
- No automatic lesson, eval, or training-data promotion.
- Escalation is bounded and opt-in: one reviewed Qwen -> DeepSeek hop, disabled,
  manual, and hosted-blocked by default, kept inside the task trace and budget. No
  multi-hop routing, provider scoring, or autonomous self-training.
- No secrets, full prompts, or source captured by default; proprietary data requires
  explicit project consent.
- See `docs/TEACHER_STUDENT_LOOP.md`, `docs/LEARNING_LOOP.md`, and
  `docs/EVAL_PROMOTION.md`.

## Working rules

- Inspect `git status` first. Prefer targeted reads and searches over repo-wide scans.
- Run the smallest relevant validation from this directory.
- Preserve deferred architecture unless a task explicitly scopes a change. Do not add
  a database, cache, vector store, API, UI, daemon, broker, RAG, orchestration
  platform, container stack, or training execution.
- Prefer Python standard library and shell. Profiles are JSON-compatible YAML.
- Active application files stay at their application-owned paths; no symlinks.
- Keep commits bounded to the intended scope. No force push, amend, or history rewrite.

## Constraints

- Keep the implementation understandable by one developer in one sitting.
- Content Studio must not depend on this repository at runtime.

## Safety

- Never print, commit, or replace secrets. Merge managed Qwen fields into the existing
  settings file and preserve all unowned values.
- Use dry-run first. Back up every changed active file before apply; restore after
  failed post-apply checks.
- System-level Ollama changes may use `sudo`; user-level Qwen changes must not.
- A status operation is read-only and must never repair drift.

## Verification

Run `python3 -m unittest discover -s tests -v`, `./scripts/verify-environment`,
representative dry-runs, and `git diff --check`. Do not run expensive model inference
unless explicitly requested. See `docs/OPERATIONS.md`.
