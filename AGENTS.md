# Agent guide — Local AI Infra

## Boundary

This repository owns **how** local AI is configured, measured, evaluated, and improved. Project repositories own **what** an agent must know about their domain. Never copy project memory, source, prompts, credentials, or private context here by default.

## v0.1 constraints

- Keep the implementation understandable by one developer in one sitting.
- Prefer Python standard library and shell. Profiles are JSON-compatible YAML so no YAML dependency is required.
- Do not add a database, cache, vector store, API, UI, daemon, broker, RAG, orchestration platform, container stack, or training execution.
- Active files remain at their application-owned paths. Do not symlink them into this repository.
- Content Studio must not depend on this repository at runtime.

## Safety

- Never print, commit, or replace secrets. Merge managed Qwen fields into the existing settings file and preserve all unowned values.
- Use dry-run first. Back up every changed active file before apply. Restore after failed post-apply checks.
- System-level Ollama changes may use `sudo`; user-level Qwen changes must not.
- A status operation is read-only and must never repair drift.
- Treat a single error as an observation, not a lesson. Promotion requires review and evidence.

## Verification

Run `python3 -m unittest discover -s tests -v`, `./scripts/verify-environment`, representative dry-runs, and `git diff --check`. Do not run expensive model inference unless explicitly requested.
