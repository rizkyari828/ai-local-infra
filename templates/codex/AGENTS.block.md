## Local AI Infra — portable guidance (managed)

This block is managed by `scripts/bootstrap` and regenerated on apply. Edit the
source in `ai-local-infra/templates/codex/AGENTS.block.md`, not in this file.

- RTK: prefix shell commands with `rtk` (`rtk git status`) and keep the prefix
  inside chains. The runtime AUTO state is owned by the tool, not by this block.
- Graphify: answer architecture, dependency, and navigation questions from the
  canonical local graph at `{{graphify.graph_dir}}/{{graphify.graph_file}}`
  (generated locally, never committed) before corroborating with reads.
- Headroom: the MCP server declaration is owned as its own table in `config.toml`.
  Credentials, PID, logs, cache, and runtime state stay machine-local.
- Routing: use the cheapest sufficient reasoning. Classify first
  (`./scripts/route --task "..."`) into mechanical / normal / correctness-sensitive
  / critical and prefer the lower tier; escalate only on ambiguity, correctness
  risk, or a bounded repeated failure. Successful bounded work stops.
- Scope: inspect the minimum necessary context (targeted reads, and Graphify before
  broad scans). Do not perform unrelated cleanup or retry the same failed approach.
  Headroom compression is independent; never add its savings to routing savings.
- Derived knowledge: before a broad or expensive investigation, classify the task
  and consult relevant derived knowledge (`./scripts/route`), reusing verified
  lessons instead of rediscovering known failure causes. After a meaningful task,
  optionally record a bounded outcome (`./scripts/learn record`) — never for every
  `git status`/read, and never with prompts, responses, or secrets. Learning is
  local evidence plus Git-shared distilled knowledge; it never auto-promotes.
- Multi-step objectives: for a high-level objective, plan first with
  `./scripts/orchestrate plan --objective "..."` to get per-node routing and
  minimum-sufficient context before doing broad, expensive work.
