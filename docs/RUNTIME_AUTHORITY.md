# Runtime authority

One effective-resolution model decides what the runtime may use on this machine.
`scripts/common/authority.py` is that model; runtime execution, routing, Doctor,
`verify-environment`, and bootstrap all read their authoritative state from it
instead of re-deriving it.

The goal is not more configuration. It is one owner per fact.

## Hard policy versus overridable defaults

**Hard policy** is a prohibition declared by a committed machine profile
(`machines/<id>.yaml`). It is config-owned, never inferred, and cannot be lifted
by an explicit invocation, a machine-local preference, a discovered binary, a
reachable endpoint, an installed model, or a previous success. Availability is
not authorization.

| Field | Owner | Kind |
|---|---|---|
| `provider_availability.<provider> == false` | committed machine descriptor | hard prohibition |
| `local_inference: NOT_CONFIGURED_BY_MACHINE_POLICY` | committed machine descriptor | hard prohibition (implies `ollama`) |
| everything else in a descriptor (bin paths, `state_dir`, `graph_root`, `feature`, `codex.model`) | descriptor, overridable per the layers below | preference/default |

`authority.machine_policy(env)` returns the prohibitions plus provenance
(`machine_id`, `machine_id_source`, `descriptor`). An unresolved or unknown
machine id has no prohibition; a *declared but malformed* profile is an explicit
error, never a silent "permitted".

## Overridable precedence

Highest first:

```
explicit invocation / user-owned runtime setting     (explicit_invocation)
        ↓
machine-local preference   $state_dir/machine.local.yaml   (machine_local)
        ↓
portable repository default   config/*.yaml             (portable_default)
```

Every effective value carries the source that supplied it
(`authority.effective(...)` returns `value`, `source`, `allowed`, `blocked_by`,
`layers`). All layers remain bounded by applicable hard constraints.

## Field-specific exceptions (single authoritative owner)

These fields are **not** merged, because a second source would be a second
authority. They are read from their one owner:

- **`provider_availability`, `local_inference`** — the committed machine
  descriptor only. `machine.local.yaml` may carry preferences; it can never grant
  a provider permission the machine profile prohibits.
- **Provider/model resolution** (`config/model-routing.yaml` -> `models/roles.yaml`
  -> provider) — routing resolves names; it never changes classification or a
  user's selected model.
- **Machine identity** — `LAI_MACHINE` when set, else the descriptor whose
  declared `repo_path` is exactly this repository. Never guessed.
- **Repository identity** — remote URL (credential-stripped) or repository name.
  Path spelling is never identity.
- **Managed guidance content** — `templates/codex/AGENTS.block.md` between the
  managed markers. User text outside the markers is never owned.

## Observation is evidence

`authority.observation_gate(provider, status, policy=..., tripped=...)` expresses
the rule once: a runtime observation may narrow eligibility (unhealthy, missing,
in cooldown) and may supply bounded provenance, but it can never grant permission
or become a second configuration authority.

```
configured authority + hard policy + runtime observation filter = effective eligibility
```

Transient health is never persisted as durable authority.

## Structured configuration parsing

Structured files are parsed with a real parser, not with pattern extraction.
`authority.read_toml` uses the standard-library `tomllib` (imported lazily so this
module stays importable on an older interpreter) and returns `(data, error)`.
Absent files are absent, not errors; malformed files are an explicit error string.
A locally configured Codex model is read with `authority.read_codex_model`, so an
exact top-level `model` key is read — not `model_provider` or
`model_reasoning_effort`. Writing a managed TOML key/table keeps the existing
line-based edit in `bootstrap.py`, which preserves unrelated formatting and
comments.

## Workspace authority

`authority.resolve_workspace(explicit=None, *, invocation_dir=..., infra_root=...)`
resolves the ONE active workspace and reports its provenance:

- `source`: `explicit`, `discovered_git_toplevel`, `invocation_directory`, or
  `infrastructure_repository`.
- `repository`: `{id, name, root, head, remote, is_repository, worktree}`.
- `subdirectory`, `worktree`, `is_infrastructure_repo`, `invocation_dir`.
- `conflict`: set when an explicit workspace belongs to a different repository
  than the invocation directory. The explicit workspace stays authoritative.

`cwd == workspace` and `ai-local-infra == product workspace` are never assumed: a
repository subdirectory resolves to the repository toplevel, and `orchestrate
run/direct --workspace PATH` overrides discovery. `execution.workspace_root` uses
the same validation rule, so Git, context retrieval, tools, and the worker working
directory all agree on one root.

## Consumers

- **Runtime** — `orchestrate_cli` binds `authority.machine_policy` into the run's
  hard constraints (`prohibited_providers`) and into `failover.Health`. Both the
  health-aware and alternate resolution paths pass the one hard-policy check
  (`failover.policy_block`), so an explicit invocation cannot bypass policy. The
  policy is part of execution identity, so a run resolved while a provider was
  prohibited cannot be resumed where it is permitted.
- **Doctor** — reports the same permitted/prohibited state (`authority`
  `machine-policy` check). Doctor never probes a provider: availability is a
  runtime observation, not a readiness fact.
- **`verify-environment`** — skips the Ollama probe when policy prohibits local
  inference (`Ollama health: SKIPPED_BY_POLICY`) and otherwise probes unchanged.
- **Bootstrap** — owns declared managed blocks/keys only; see
  [Multi-machine sync](MULTI_MACHINE_SYNC_V0_1.md).
