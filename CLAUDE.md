# Claude Code — project entry point

This repository's authoritative agent guide is `AGENTS.md`. Read it first; it is
the source of truth for scope, model roles, working rules, constraints and safety.
Do not duplicate that content here. If the two ever disagree, `AGENTS.md` wins.

## Operating commands

`AGENTS.md` states the rules but not the commands. The canonical operating
commands live in `docs/OPERATIONS.md`:

- multi-machine configuration: `LAI_MACHINE=<machine> ./scripts/bootstrap ...`
- task graph: `./scripts/orchestrate plan|run ...`
- environment check: `./scripts/verify-environment`
- machine descriptors: `machines/*.yaml`
- local state root: `LAI_STATE_DIR` (default `~/.local/state/local-ai-infra`)

## Verification note

Prefer per-module test runs with a bounded timeout:
`python3.13 -m unittest tests.<module>`
over one long `discover` run. The interpreter must be >= 3.11.

## Boundaries

- Do not modify `model-capabilities.yaml`, `model-routing.yaml`,
  `models/roles.yaml`, or `machines/*.yaml` to encode a preference about which
  account or model to use.
- Never print, commit, or replace secrets.
- `git status` first. Keep commits bounded. No force push, amend, or history
  rewrite.
