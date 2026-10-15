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
(`machine_id`, `machine_id_source`, `descriptor`) and an explicit **state**:

| State | Meaning |
|---|---|
| `VALID_REGISTERED_MACHINE` | a registered profile resolved; its prohibitions apply |
| `GENUINELY_UNREGISTERED_MACHINE` | no registered profile describes this checkout: portable V0.1 behavior, reported as `NO_MACHINE_POLICY` |
| `EXPLICIT_MACHINE_SELECTOR_INVALID` | `LAI_MACHINE` was set and does not resolve — authoritative intent that must fail, never a fallback to discovery |
| `MACHINE_DESCRIPTOR_MALFORMED` | a registered descriptor (the selected one, or one encountered while matching) is unreadable |
| `MACHINE_POLICY_FIELD_INVALID` | a hard-policy field has the wrong type |

The three INVALID states are never collapsed into "no policy" or "permitted": a
prohibition that cannot be read exactly is not a permission. Hard-policy fields
are validated strictly — `false` is a boolean prohibition, `"false"` is a mistyped
value that is rejected rather than interpreted by truthiness.

- `authority.machine_policy(...)` reports the state and never raises: read-only
  diagnostics (Doctor, `verify-environment`) render it.
- `authority.machine_policy_or_fail(...)` raises for the INVALID states: any
  execution-critical caller fails closed.

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
  a provider permission the machine profile prohibits, and it can never repair an
  invalid hard policy (an INVALID policy stays INVALID).
- **Provider/model resolution** (`config/model-routing.yaml` -> `models/roles.yaml`
  -> provider) — routing resolves names; it never changes classification or a
  user's selected model.
- **`codex.model` managed unit** — observed and reported, not enforced: the unit
  is adopt-only, so a user's existing `model` value in `config.toml` is preserved
  and reported as DRIFTED rather than overwritten (only an absent key is seeded).
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
An authority-relevant value never silently degrades: a malformed Codex `config.toml`
is reported as `MODEL_CONFIG_INVALID` (a blocking runtime status) rather than being
read as "no model configured". A locally configured Codex model is read with
`authority.read_codex_model`, so an exact top-level `model` key is read — not
`model_provider` or `model_reasoning_effort`. Writing a managed TOML key/table
keeps the existing line-based edit in `bootstrap.py`, which preserves unrelated
formatting and comments.

## Workspace authority

`authority.resolve_workspace(explicit=None, *, invocation_dir=..., infra_root=...)`
resolves the ONE active workspace and reports its provenance:

- `source`: `explicit`, `discovered_git_toplevel`, `invocation_directory`, or
  `infrastructure_repository`.
- `repository`: `{id, kind, portable, workspace_instance, name, root, head, remote,
  is_repository, worktree}`.
- `subdirectory`, `worktree`, `workspace_instance`, `is_infrastructure_repo`,
  `invocation_dir`.
- `conflict`: set when an explicit workspace belongs to a different repository
  than the invocation directory. The explicit workspace stays authoritative.

`cwd == workspace` and `ai-local-infra == product workspace` are never assumed: a
repository subdirectory resolves to the repository toplevel, and `orchestrate
run/direct --workspace PATH` overrides discovery. `execution.workspace_root` uses
the same validation rule, so Git, context retrieval, tools, and the worker working
directory all agree on one root.

### One repository identity, collision resistant

`authority.repository_identity(root)` is the single identity implementation, used
by workspace authority, `orchestration_store.repository_identity`, the result
cache/resume identity (`project_id`), and bootstrap's graph manifest.

- A sanitized remote URL when present (`remote:<url>`, `portable: true`). HTTPS
  userinfo and `ssh://user:pass@host` credentials are stripped.
- Otherwise a remote-less Git repository uses a hash of its **canonical common git
  directory** (`gitdir:<hash>`). This is machine-local (`portable: false`) but
  collision-free: two unrelated repositories are distinct even when they share a
  directory name, the same tree, and the same HEAD. A shared basename is never
  proof of equivalence, and identity never depends on HEAD, so it survives
  ordinary commits, path spelling, and symlink aliases. Git reports a *relative*
  metadata path relative to the directory its command ran in, so the common
  directory is resolved in that Git invocation context: identity is identical
  whether the caller runs at the repository root, a nested subdirectory, or
  through a symlink.
- A non-Git workspace uses a hash of its canonical path (`path:<hash>`,
  `portable: false`). Consumers must treat that as machine-local and must not
  fabricate cross-machine equivalence from it.

**Repository identity versus workspace instance.** `id` is shared by every
worktree and clone of one repository (a linked worktree belongs to the same
repository). `workspace_instance` (`worktree:<hash of that worktree's git dir>`)
distinguishes mutable workspaces. Cache and resume bucket on repository identity,
which also includes the repository HEAD, the task/input fingerprints, and the
execution policy fingerprint, so unrelated repositories can never reuse each
other's results.

## Consumers

- **Runtime** — `orchestrate_cli` supplies machine policy to health-aware
  resolution, and `execution.execute_plan` is the binding boundary:
  `authority.bind_constraints` is applied there for every run, so no caller
  (CLI, `lifecycle.run_direct`, Direct Mode, any library caller) can bypass a
  machine prohibition by omitting it. The caller may narrow policy; it can never
  weaken it. The bound constraints are used for resolution, the policy
  fingerprint, resume validation, cache eligibility, and routing/failover, so a
  run resolved while a provider was prohibited cannot be resumed where it is
  permitted.
- **Launch eligibility** — a prohibition *blocks* a candidate only when the run
  can actually reach a provider: a registry containing an adapter that declares
  itself `SUPPORTED` (`execution.launch_capable`). A registry of only pretend
  adapters (the SIMULATED fake, or the abstract `UNSUPPORTED` base) has no launch
  to authorize. The policy is still recorded in that run's identity, so the
  identity is never adapter-dependent.
- **Agent Runtime** — `AgentRuntime.run` checks authoritative machine permission
  immediately before the first provider invocation
  (`runtime.require_machine_permission`), so **no construction path can omit or
  disable it**: a provider this machine prohibits never reaches `execute`, whether
  it was resolved from configuration, supplied directly, or handed over by an
  escalation factory, and no flag can switch the check off. Tool-only invocations
  and `--health` are unaffected (neither performs inference).
  `EscalationController.from_config` and `scripts/agent-run --escalate`
  additionally check early, for fail-fast diagnostics — the boundary check, not the
  factory, is what makes the invariant hold.
- **Doctor** — reports the same permission state, with the state kept distinct
  (`machine-policy` check). Doctor never probes a provider: availability is a
  runtime observation, not a readiness fact.
- **`verify-environment`** — consumes the canonical policy state: `PROHIBITED`
  skips the probe and reports the prohibition; an INVALID policy is never probed
  and fails verification; a `GENUINELY_UNREGISTERED` checkout reports
  `NO_MACHINE_POLICY` and follows the portable behavior; otherwise it probes.
- **Bootstrap** — owns declared managed blocks/keys only; see
  [Multi-machine sync](MULTI_MACHINE_SYNC_V0_1.md).
