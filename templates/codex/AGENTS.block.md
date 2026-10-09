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
