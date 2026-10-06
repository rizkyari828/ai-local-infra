## Local AI Infra — portable guidance (managed)

This block is managed by `scripts/bootstrap` and regenerated on apply. Edit the
source in `ai-local-infra/templates/codex/AGENTS.block.md`, not in this file.

- RTK: prefix shell commands with `rtk` (`rtk git status`) and keep the prefix
  inside chains. The runtime AUTO state is owned by the tool, not by this block.
- Graphify: answer architecture, dependency, and navigation questions from the
  prebuilt graph first, then corroborate with reads. The graph is generated
  locally and never committed.
- Headroom: the MCP server declaration is owned as its own table in `config.toml`.
  Credentials, PID, logs, cache, and runtime state stay machine-local.
