# Model capability discovery

Discovery answers "which providers/models exist, what effort variants they expose,
and on which surfaces?" — **without** calling a model. It is catalog-only: it reads
trustworthy local configuration and (optionally) a local generated snapshot. It
never spends tokens, installs/downloads, mutates configuration, or prints secrets.
See [`MODEL_ROUTING.md`](MODEL_ROUTING.md) for how routing consumes it.

```bash
./scripts/models detect                       # per-surface discovery
./scripts/models list                         # flat list of known models
./scripts/models list --provider claude --json
```

## Three concepts that must stay separate

1. **Task classification** — `MECHANICAL`/`NORMAL`/`CORRECTNESS_SENSITIVE`/`CRITICAL` (provider independent).
2. **Routing requirement** — symbolic tier `FAST`/`BALANCED`/`STRONG`/`MAX` + reasoning effort (provider independent).
3. **Model capability** — concrete provider/model, its effort variants, and surface availability.

Discovery owns only (3). It never changes (1) or (2).

## Status model

| State | Meaning |
|---|---|
| `KNOWN` | Present in a trustworthy local catalog (`models/roles.yaml`, config, snapshot). |
| `AVAILABLE` | Usable by the relevant surface/account per current evidence. |
| `VERIFIED` | A concrete model **and** native variant actually completed a successful execution. |
| `UNAVAILABLE` | Evidence says the surface/account cannot use it (e.g. `ACCOUNT_MODEL_UNSUPPORTED`). |
| `UNVERIFIED` | Present/available but no successful execution recorded for that variant. |
| `UNKNOWN` | No trustworthy evidence; never guessed. |

These are deliberately not equivalent: `KNOWN != AVAILABLE != VERIFIED`. A model
can be `KNOWN` with no surface evidence, `AVAILABLE` with no `VERIFIED` variant,
or `UNAVAILABLE` on one surface while `AVAILABLE` on another. Availability may be
reused across surfaces that demonstrably share config; **verification is
surface-specific** and never transfers.

## Model vs effort variant

A model's identity is separate from its reasoning variants. Each variant keeps the
**provider-native** name (authoritative for invocation) alongside a
**normalized** ordinal used only for comparison:

```text
native: "xhigh"   normalized: VERY_HIGH
native: "high"    normalized: HIGH
native: "nonsense" normalized: UNKNOWN
```

Provider-native → normalized mappings live in `config/model-capabilities.yaml`
(`effort_mappings`), not in routing code. An unknown native variant stays `UNKNOWN`
rather than being guessed. Verification is per model+variant: `model/HIGH` verified
does **not** imply `model/LOW` or `model/MAX` verified.

## Discovery sources and precedence

Read-only sources (no inference): `models/roles.yaml` (role → provider/model),
`config/model-capabilities.yaml` (aliases, effort mappings, manual overrides,
surface declarations), and the optional local snapshot
`$LAI_STATE_DIR/model-capabilities.json` (discovered/verified evidence).

Resolution precedence per model+surface:

```text
explicit user override (manual_override)
  -> verified local evidence (observed_runtime / historical_verified)
     -> discovered local capability (discovered)
        -> portable registry (models/roles.yaml)
           -> unknown
```

Missing information is never fabricated: if a surface's availability or a
variant's verification is not evidenced, it is reported `UNKNOWN`/`UNVERIFIED`.

## Manual override

`config/model-capabilities.yaml` supports provider aliases, model aliases,
provider-native effort mappings, and bounded `manual_overrides` (provider, model,
surfaces, status, efforts). Overrides are the highest precedence but should not be
required to enumerate everything.

## Portable config vs local generated state

- **Portable config** (`config/model-capabilities.yaml`, committed): discovery
  rules, aliases, effort mappings, overrides. No secrets, no machine catalog.
- **Local generated state** (`$LAI_STATE_DIR/model-capabilities.json`,
  machine-local, never committed): actually discovered models, current
  availability, surface inventory, verification evidence/timestamps.

Verified **historical** observations are seeded into the local snapshot (source
labels: `discovered`, `historical_verified`, `observed_runtime`, `manual_override`;
see `../scripts/common/derived_knowledge.py`). Historical evidence stays
distinguishable from live discovery; raw chats/prompts are never imported. This
prepares the separate
model-performance evidence layer for Derived Learning (provider, model, surface,
tier, effort, outcome) — routing lessons key on `task_family`/`task_class`/
`required_tier`/`required_effort`, never "always use model XYZ".

## Failure categories

Infrastructure trouble is not model inadequacy. `ACCOUNT_MODEL_UNSUPPORTED` is
availability evidence; `AUTH_FAILURE`, `RATE_LIMIT`, `PROVIDER_503`, `TIMEOUT`,
and `STALE_SESSION` are not; only `REASONING_INSUFFICIENT` is outcome evidence
about reasoning capability. See `discovery.classify_failure`.
