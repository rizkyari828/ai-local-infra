# Architecture

## Boundary

`local-ai-infra` owns reusable execution mechanics: model/profile metadata, safe local configuration, verification, benchmarks, telemetry formats, eval definitions, and reviewed learning-candidate formats. A project repository owns domain prompts, source, architecture, decisions, memory, and delivery state. The dependency direction is currently neither: this repository operates local tools, while Content Studio continues to run independently.

## v0.1 components

- JSON-compatible YAML profiles and registry files are versioned desired state.
- `ai-profile` reads actual Qwen and systemd state, reports drift, applies only managed fields, and stores operational state outside Git.
- Qwen writes are atomic JSON replacements after a merge. Unmanaged values, including secrets, stay in memory and the active file; they are never printed or copied into Git.
- Ollama uses one generated systemd drop-in. Only its installation/restart crosses the `sudo` boundary.
- JSONL runtime records are append-only local files. Schemas, evals, curated baselines, and docs are versioned; raw runs are ignored.
- Inference tools require an explicit `--confirm-run`; validation never launches a 27B workload.
- A minimal Agent Runtime executes one local-model task through a provider (Ollama),
  emitting validated spans automatically, enforcing configured limits, honoring
  timeout/cancellation, and classifying failures. Ollama remains an independent
  inference service. See [Agent runtime](AGENT_RUNTIME.md).
- A bounded Tool Runtime (`file.read`, `file.search`, `file.write`, `shell.exec`,
  `test.run`) executes within one session trace, confined to an explicit workspace
  root, with automatic tool spans and a tool-call budget. See
  [Tool runtime](TOOL_RUNTIME.md).
- A `DeepSeekProvider` and a conservative, opt-in `EscalationController` allow at
  most one Qwen -> DeepSeek hop for classified model-capability failures, keeping
  the original trace and the remaining runtime budget. See
  [Escalation](ESCALATION.md).
- Inference spans carry normalized provider usage (cache hit/miss, total tokens),
  a prefix fingerprint, and optional estimated cost from a data-only pricing file.
  A read-only `cache-metrics` report aggregates cache effectiveness, latency, and
  cost. This is observability only: no routing, scoring, or cost optimization. See
  [Provider usage and cache telemetry](CACHE_TELEMETRY.md). `import-opencode-stats`
  imports OpenCode's own aggregate stats into the same vocabulary without a plugin,
  database access, or auth change.

No server process exists. Commands are short-lived and state is inspectable. Active application files remain at `~/.qwen/settings.json` and `/etc/systemd/system/ollama.service.d/override.conf`; no symlinks are used.

## Safety and consistency

Apply is validate → compare → resource guard → backup → atomic/user write → privileged/system write → daemon reload/restart if required → health check → actual-state verification. Failure after a write triggers restoration of the new backup. Matching state is idempotent. Status never mutates.

Backups may contain Qwen credentials, so they live in a mode-0700 state directory with mode-0600 files. They must not be uploaded or committed.

## Deliberately absent

There is no PostgreSQL, ClickHouse, DuckDB dependency, Redis/Valkey, pgvector, HTTP API, UI, daemon, broker, RAG, Docker stack, scheduler, multi-agent orchestrator, multi-hop routing, provider scoring, cost optimizer, fine-tuning, LoRA, or cloud management in v0.1. The Agent Runtime is a single-task execution layer, the Tool Runtime executes one explicitly requested call at a time, and escalation is a single reviewed hop; none of them is an autonomous platform.
