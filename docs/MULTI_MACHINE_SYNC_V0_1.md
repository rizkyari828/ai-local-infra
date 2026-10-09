# Multi-Machine Sync v0.1 — Contract and Shared/Local Boundary

Status: **frozen contract** (M1). Design only. No bootstrap, apply, or sync
mechanism is implemented in this milestone.

## 1. Objective

Use Git as the single source of truth for **portable AI-infrastructure policy and
configuration** across two machines, while every machine keeps its own **machine-local
runtime state**. Git carries desired state; each machine applies it locally.

```text
                    Git repository
                         |
                  shared desired state
                         |
               +---------+---------+
               |                   |
            Work Mac            Home WSL
               |                   |
          apply locally        apply locally
               |                   |
          local runtime        local runtime
```

The contract exists so that: (a) shared policy is edited and reviewed once, (b) a
machine can reconcile itself toward Git without clobbering machine-local state, and
(c) live runtime state, credentials, and generated artifacts never travel between
machines.

## 2. Non-goals

This milestone and v0.1 deliberately exclude:

- Live two-way sync, background daemons, or file-watching.
- Sharing runtime state, sessions, databases, caches, PIDs, logs, or graphs.
- Automatic `git push`, automatic merge, or automatic conflict resolution.
- A central Headroom server, shared Headroom cache, or cloud control plane.
- Remote execution between machines and any distributed lock.
- Syncing credentials, tokens, `.env` files, or keychain material.
- Full-directory replacement of any user-global config directory.

## 3. Current environments

### Machine A — Work Mac (macOS)

- Repository: `~/Developer/Back-End/ai-local-infra` (known good).
- Tools: OpenCode, Codex CLI, Codex VS Code extension, Codex Desktop, RTK,
  Graphify, Headroom.
- OpenCode: RTK AUTO, Graphify AUTO, Headroom proxy on `127.0.0.1:8787`, launched
  through `~/.local/bin/oc`.
- Codex: RTK ACTIVE, Graphify AUTO, Headroom MCP AVAILABLE; user-global config
  under `~/.codex/`.
- Graphify graph is stored **outside** the repository.
- Repository is clean at contract time.

### Machine B — Home WSL/Linux

- Same local-ai-infra project; machine-specific paths and configuration differ.
- Tools: OpenCode, Codex, RTK, Graphify, Headroom.
- Linux/WSL lifecycle differs from macOS; existing wrapper/lifecycle may use
  Linux-specific mechanisms.
- Runtime state must remain machine-local.

### Existing repository conventions this contract reuses

The repository already separates versioned desired state from local state. The sync
contract must extend these, not replace them:

- Versioned desired state: `profiles/`, `models/`, `config/`, `hardware/`,
  `telemetry/schemas/`, `templates/`, `docs/`.
- Local operational state: `~/.local/state/local-ai-infra/` (`LAI_STATE_DIR`),
  containing backups (`0700`) and the active-profile marker; never in Git.
- Environment overrides: `LAI_QWEN_SETTINGS`, `LAI_OLLAMA_OVERRIDE`,
  `LAI_STATE_DIR`, `LAI_SKIP_SYSTEMD`.
- Reconciliation vocabulary already used by `ai-profile`: `MATCH`, `DRIFT`,
  `UNKNOWN`; apply is validate → compare → guard → backup → write → health → verify.
- JSON-compatible YAML files, stdlib-only Python, no server process.
- `.gitignore` already excludes `.env*`, `*.secret`, `*.pem`, `*.key`, `backups/`,
  `.local/`, and all raw telemetry/result JSONL.

There is **no** existing bootstrap script, machine-setup doc, machine-profile
concept, config template for Codex/OpenCode/Headroom/RTK/Graphify, or secret-scan
gate. This document introduces the boundaries; M2 introduces the mechanism.

## 4. Shared / local classification matrix

Legend: **SHARED** — Git-managed desired state. **LOCAL** — machine-local, never in
Git. **GENERATED-LOCAL** — produced on the machine, never in Git. **UNDECIDED** —
explicitly deferred.

### Candidate shared state

| Category | Class | Rationale |
|---|---|---|
| RTK behavioral rules | SHARED | Portable policy/guidance; no runtime stats. |
| Graphify guidance | SHARED | Reference text and skill guidance are portable. |
| Graphify skill/config **templates** | SHARED | Templates with placeholders; generated graph is not. |
| Headroom desired defaults | SHARED | Bind host, port, telemetry/memory policy, common flags. |
| Codex portable AGENTS guidance | SHARED | Reviewed guidance text, applied as a managed block. |
| Codex MCP declaration **templates** | SHARED | Declarations with placeholders; never live auth. |
| OpenCode portable plugin/config **templates** | SHARED | Portable behavior only, merged additively. |
| Provider/model routing policy | SHARED | Role-level intent already lives in `models/roles.yaml`. |
| Agent execution rules | SHARED | `AGENTS.md`, `docs/AGENT_*`. |
| Telemetry policy | SHARED | Schemas and retention rules, not raw events. |
| Learning/derived lessons | SHARED (reviewed only) | Only reviewed, consented, promoted artifacts. |
| Bootstrap logic | SHARED | Future M2 scripts; not created in M1. |
| Validation logic | SHARED | Future pre-commit/pre-apply checks; not created in M1. |
| Graph freshness policy | SHARED | The contract; manifests are generated locally. |
| Machine profile schema | SHARED | The schema and non-secret machine profiles. |

### Candidate local state

| Category | Class | Rationale |
|---|---|---|
| API keys | LOCAL | Secret; out-of-band only. |
| Auth tokens | LOCAL | Secret; never committed. |
| `.env` files | LOCAL | Secret; already gitignored. |
| Keychain/credential storage | LOCAL | OS credential store, machine-bound. |
| Headroom cache | GENERATED-LOCAL | Rebuildable runtime artifact. |
| Headroom PID | LOCAL | Process state, machine-bound. |
| Headroom logs | GENERATED-LOCAL | Machine output; may contain sensitive data. |
| Headroom process state | LOCAL | Live process. |
| RTK gain/runtime statistics | GENERATED-LOCAL | Local counters, not policy. |
| Raw Codex sessions | LOCAL | Sensitive transcripts. |
| Raw OpenCode sessions | LOCAL | Sensitive transcripts. |
| Graphify generated graph | GENERATED-LOCAL | Built per machine/repo; external to Git. |
| Temporary files | GENERATED-LOCAL | Disposable. |
| Machine absolute paths | LOCAL | Differ by machine; only placeholders are shared. |
| Codex project trust/runtime state | LOCAL | Trust decisions are machine-bound. |
| Model caches | GENERATED-LOCAL | Rebuildable; large. |
| SQLite/runtime databases | LOCAL | Runtime state. |

**UNDECIDED:** none at contract time. Any new candidate defaults to LOCAL until
explicitly classified.

## 5. Machine-profile model

A machine profile is a small, non-secret declaration of **what may differ per
machine**. It is not a copy of all config. Logical identities: `work-mac`,
`home-wsl`.

Identity resolution is explicit (no hostname guessing):

1. `LAI_MACHINE` environment value (authoritative, used in bootstrap/CI), or the
   equivalent explicit `--machine <id>` flag.
2. If absent, refuse to guess: `status` reports `BLOCKED` and `apply` mutates nothing.

Profiles live in `machines/<id>.yaml` and are SHARED because they carry no secrets.
Per-machine secrets and true run-only values live in
`~/.local/state/local-ai-infra/machine.local.yaml` (LOCAL), which is merged last.

Minimal profile shape (JSON-compatible YAML, matching repo style):

```json
{
  "schema_version": 1,
  "id": "work-mac",
  "platform": "macos",
  "os": "darwin",
  "shell": "zsh",
  "repo_path": "~/Developer/Back-End/ai-local-infra",
  "graph_root": "~/.local/share/local-ai-infra/graphify",
  "state_dir": "~/.local/state/local-ai-infra",
  "bin": {"oc": "~/.local/bin/oc"},
  "headroom": {"bin": "~/.local/bin/headroom", "lifecycle": "wrapper"},
  "lifecycle_capabilities": ["launchd"],
  "provider_availability": {"ollama": true, "deepseek": true, "codex": true}
}
```

**Lifecycle correction (M3).** The desired Headroom lifecycle is `wrapper` on both
machines: `~/.local/bin/oc` (or the Linux equivalent) checks and starts Headroom
locally. `launchd`/`systemd` are recorded only as **future optional capabilities**
under `lifecycle_capabilities`; they are never desired state. Bootstrap must not
create, enable, or manage launchd plists, systemd units, cron, or any background
daemon registration.

Values that MAY differ per machine: OS, repo path, graph storage root, state dir,
binary locations, lifecycle **capability** flags, shell, provider availability, and
optional feature flags. Values that MUST NOT differ conceptually are the policy
files themselves (routing, telemetry, guidance); machines only point at them.

## 6. Configuration precedence and target repository structure

### Precedence (later wins)

```text
1. common defaults        config/common/, models/, profiles/, telemetry/schemas/
        |
2. platform defaults      config/macos/, config/linux/, templates/<os>/
        |
3. machine overrides      machines/<id>.yaml
        |
4. local secrets/runtime  ~/.local/state/local-ai-infra/machine.local.yaml, LAI_* env
```

Rules:

- Precedence is per **key**, not per file. A later layer overrides only the keys it
  sets; unset keys fall through.
- Layer 4 is never committed and never read by another machine.
- Merge unit for tool configs is a **managed block** or a **key path**, never an
  entire directory and never a whole file when the tool mixes portable and local
  keys (see §7).
- Plain `common` + `platform` + `machine` files must be deterministic and free of
  secrets so `status`/`dry-run` can run without credentials.

### Target repository structure

The smallest structure that fits existing conventions. Items marked _(M2+)_ are
planned, **not created in M1**.

```text
config/                     # existing: portable policy (add common/, macos/, linux/ as needed)
models/                     # existing: roles/registry
profiles/                   # existing: model/runtime profiles
hardware/                   # existing: captured hardware facts (unchanged role)
machines/                   # (M2+) non-secret machine profiles: work-mac.yaml, home-wsl.yaml
templates/                  # existing: expand per tool, keep *.template naming
  codex/
  opencode/
  headroom/
  rtk/
  graphify/
bootstrap/                  # (M2+) status, dry-run, apply, verify — no daemon
docs/                       # this document and operating docs
```

Local/generated state stays outside Git:

```text
~/.local/state/local-ai-infra/     # state_dir: backups/, active marker, machine.local.yaml,
                                   #           applied-state.json, graph manifest refs
~/.local/share/local-ai-infra/     # graph_root: Graphify graphs and manifests
```

No repo-local `.codex/` or `.opencode/` is created, and Graphify graphs are never
committed.

## 7. Component ownership

For each component: **Git owns** (portable desired state), **bootstrap/apply will
own** (managed writes), **tool owns** (never touched by us), **user/manual owns**,
and **never auto-overwritten**.

### RTK

- Git owns: behavioral guidance and template references.
- Apply will own: the managed guidance reference in the consuming config, if any.
- Tool owns: gain counters, AUTO state, runtime stats, binary.
- User owns: local operational choices.
- Never overwritten: runtime stats and any live RTK state.

### Graphify

- Git owns: guidance, skill/config templates, freshness policy.
- Apply will own: template-rendered guidance; may write a local manifest.
- Tool owns: generated graph, index, caches.
- User owns: when to rebuild.
- Never overwritten: generated graphs; never committed.

### Headroom

- Git owns: desired bind host/port, telemetry policy, memory policy, common flags.
- Apply will own: the managed config block implementing those defaults.
- Tool owns: process, PID, logs, cache, runtime stats.
- User owns: upstream credentials and auth.
- Never overwritten: PID/log/cache/process state and credentials.

### Codex

- Git owns: selected AGENTS guidance, RTK/Graphify guidance references, selected MCP
  declaration templates.
- Apply will own: a delimited managed block in `AGENTS.md` and, later, known
  declaration keys in config, via merge.
- Tool owns: sessions, caches, SQLite/history, trust state, auth.
- User owns: auth, project trust, machine-specific MCP settings.
- Never overwritten: sessions, `auth.json`, project trust, caches, runtime DBs.
- **`~/.codex/` is never treated as a replaceable unit.**

### OpenCode

- Git owns: portable plugin/config templates, role-level routing pointers, telemetry
  policy.
- Apply will own: additive/merge-aware managed blocks or keys.
- Tool owns: runtime state and local endpoints.
- User owns: provider credentials, provider-specific values, local base URLs.
- Never overwritten: `auth.json`, credentials, machine-specific paths/local
  endpoints, runtime state.
- **`~/.config/opencode/` is never treated as a replaceable unit.**

## 8. Apply semantics (design only)

Future `apply` must be idempotent, bounded, reversible, backup-aware,
non-destructive, safe when re-run, and safe after `git pull`. M1 implements none of
this.

Modes:

- `status` — read-only. Reports drift states per managed unit. Never repairs.
- `dry-run` — computes and prints planned changes (files, blocks, keys, rendered
  values), backup paths, and restart/privilege needs. Writes nothing.
- `apply` — for each unit: validate → compare → back up → atomic/merge write →
  health check → verify actual state. Re-running with matching state performs no
  writes.

Intended flow (repo-native `scripts/bootstrap`, matching the `ai-profile` CLI style):

```bash
git pull
LAI_MACHINE=work-mac ./scripts/bootstrap status
LAI_MACHINE=work-mac ./scripts/bootstrap apply --dry-run
LAI_MACHINE=work-mac ./scripts/bootstrap apply
LAI_MACHINE=work-mac ./scripts/bootstrap status
```

`status` is read-only. `apply` validates, backs up, mutates owned blocks/keys only,
re-reads, and verifies. `rollback` restores the latest bootstrap backup.

Boundaries: `sudo` only where the existing `ai-profile` already does (system Ollama
drop-in). User-level configs never use `sudo`. Managed writes are atomic or
merge/block-scoped. Failure after a write triggers restore of the just-created
backup (existing behavior).

## 9. Drift states and ownership

Drift state and ownership are separate concepts. Drift states apply only to units we
manage or can classify. Ownership/capability says whether we manage a unit at all.

Per managed unit, `status` reports exactly one drift state:

- **ALIGNED** — actual matches the resolved desired value.
- **DRIFTED** — present but differs; safely reconcilable.
- **MISSING** — managed unit or required target absent.
- **LOCAL-OVERRIDE** — the final local-only layer (`machine.local.yaml`) intentionally
  supersedes shared policy (§6 layer 4); reported, not changed. A committed machine
  profile differing from common defaults is **not** a local override.
- **STALE** — present and plausible, but its recorded provenance is older or
  unverified (for example a generated graph from another HEAD or version).
- **BLOCKED** — a real unsafe/unresolved condition: unknown machine identity,
  unsupported machine/platform, parse failure, duplicate/malformed managed markers,
  malformed ownership region, secret violation, unsafe ambiguity/path, unreadable
  required target, invalid config.

The frozen drift vocabulary stays exactly
`ALIGNED / DRIFTED / MISSING / LOCAL-OVERRIDE / STALE / BLOCKED`. There is no
`DEFERRED` drift state.

Ownership capability is reported separately:

- `MANAGED` — the unit has a drift state and may be applied or classified.
- `NOT_MANAGED` — intentionally outside current ownership (RTK runtime/config,
  OpenCode global config). Reported with a reason; **never** shown as `BLOCKED`.
- Capability vs mutability: a `MANAGED` unit may still be `READ_ONLY`/`NONE`
  (for example Graphify freshness is classified but never written by apply).

Mapping to the existing `ai-profile` vocabulary so no parallel system is created:
`ALIGNED`≈`MATCH`, `DRIFTED`≈`DRIFT`, `BLOCKED`/`MISSING`⊂`UNKNOWN`.

## 10. Graphify freshness contract

Graphs remain **generated locally** and are never committed. Freshness is judged
from a locally stored sidecar manifest beside the graph, not from Git and not from
graph existence alone. Manifest file: `lai-freshness.json` in the graph directory.

Manifest fields (schema_version 1):

- `repository_identity` — `remote:<sanitized-url>` when a remote exists, else
  `name:<repo-name>`. Path is diagnostics only; identity never depends on path.
- `repository_name` / `repository_root` — diagnostics (root is local-only).
- `git_head` — commit recorded at build time.
- `graphify_version` — builder version.
- `built_at` — build timestamp (UTC).
- `graph_path` — location of the generated graph.
- optional `node_count` / `edge_count` — only when cheaply and reliably available.

Decision rule:

```text
graph dir resolves inside the repository            -> BLOCKED (unsafe)
graph.json absent                                    -> MISSING
sidecar manifest absent                              -> MISSING (provenance unverified)
manifest unreadable / malformed / missing fields     -> BLOCKED
identity mismatch or identity unresolved             -> BLOCKED
manifest.git_head != current HEAD                    -> STALE
installed graphify version unavailable/unparseable   -> STALE (unverified)
manifest.graphify_version != installed version       -> STALE
manifest.graph_path != located graph                 -> STALE
working tree has uncommitted changes                 -> STALE (matches committed HEAD only)
otherwise                                            -> CURRENT
```

`CURRENT` requires a clean working tree: Graphify extracts from the working tree, so
a manifest that matches committed HEAD is only `CURRENT` while the worktree is also
clean. Uncommitted changes report `STALE` (warn-only, never `BLOCKED`, never
rebuilt by status/apply).

`CURRENT` is never claimed from graph existence alone, and the existing external
graph is **not** adopted by writing a manifest with the current HEAD. Adoption would
require a fresh rebuild or independently trustworthy build provenance. Graph
provenance is valid only for a **clean Git working tree** at build time: Graphify
extracts from the working tree, so HEAD plus uncommitted changes is not equivalent
to HEAD, and `graph rebuild` refuses to write a manifest unless the worktree is
clean (see §17).

**V0.1 decision: WARN ONLY.** `status` and `apply` never rebuild the graph;
`apply` never fails because the graph is `STALE`/`MISSING`. Graph freshness is
classified by `scripts/bootstrap status` and `scripts/bootstrap graph status`.
Rebuild happens only through the explicit `scripts/bootstrap graph rebuild`
command (see §17); it writes the sidecar manifest atomically only after a
successful generation and preserves the previous usable graph on failure.

## 11. Secret safety

### Hard deny-list (never committed, never copied into Git to make config portable)

- API keys and bearer/refresh tokens.
- Auth/session files (`auth.json`, cookies, browser sessions).
- `.env` secrets and provider credentials.
- Private credential stores and keychain material.
- Any value matching the existing `.gitignore` secret patterns
  (`.env*`, `*.secret`, `*.pem`, `*.key`).

### Future validation expectations (M2+)

- Pre-commit: scan the staged diff for the deny-list and known secret patterns;
  block on match.
- Pre-apply: refuse to render any template whose resolved value would embed a
  secret; require local-only inclusion by reference (env var name, not value).
- Both gates are deny-by-default and report the offending path only, never the
  value.
- Portability is achieved by **placeholder + local reference**, never by copying a
  secret between machines or into the repository.
- Repository identity written into the graph manifest is sanitized: credential
  userinfo (for example `https://token@host/repo.git`) is stripped before it is
  persisted, and the manifest is secret-scanned before an atomic write.

## 12. Update workflow

```text
Machine A                          Machine B
  edit shared policy                 git pull
  run status / dry-run               ./scripts/bootstrap status
  verify locally                     ./scripts/bootstrap apply --dry-run
  commit + push                      ./scripts/bootstrap apply
                                     ./scripts/bootstrap status
```

Explicitly rejected for v0.1: live two-way sync, auto push, automatic conflict
resolution, shared network filesystem, cloud control plane, central Headroom,
syncing raw sessions, syncing runtime databases. The only transport is Git, and the
only sync action is a human-initiated pull + review + apply.

## 13. Rollback contract

The apply system may only remove or restore files and blocks **it recorded as
owned**. It MUST NOT:

- `rm -rf` an entire user config directory.
- Delete unrelated local state.
- Remove tool-generated state (sessions, caches, graphs, logs).
- Remove credentials.
- Silently overwrite unknown manual changes.

Backups:

- Reuse `~/.local/state/local-ai-infra/backups/` (mode `0700`, files `0600`), one
  timestamped directory per apply with a manifest listing each changed path, whether
  it existed, and its backup filename, plus a `known_good` flag.
- Restore is atomic (`atomic_restore`) for user files; the system drop-in is
  restored only if it was part of the backup.
- Block/merge writes also record their previous rendered content so a managed block
  can be reverted without touching surrounding user content.
- After a `git pull` that changes desired state, `rollback` still refers to the last
  applied backup, not to Git history.

## 14. V0.1 stop boundary (frozen exclusions)

V0.1 MUST NOT include:

- Central Headroom server or shared Headroom cache.
- Graphify graph committed to Git.
- Raw session synchronization.
- Live telemetry database synchronization.
- Automatic `git push` or automatic merge/conflict resolution.
- Cloud/Kubernetes service.
- Distributed locking.
- Remote execution between machines.
- Secrets sync.
- Any repo-local `.codex/` or `.opencode/`.
- Any bootstrap script or config change (that is M2).

## 15. Milestone acceptance criteria

### M1 — Contract and shared/local boundary (this document)

- `docs/MULTI_MACHINE_SYNC_V0_1.md` exists and covers §1–§15 of this milestone.
- Every candidate shared/local item is classified.
- Precedence, ownership, drift states, and stop boundary are explicit.
- Repository changes are documentation-only; no `.codex/`, `.opencode/`,
  `graphify-out/`, `.env`, credentials, or runtime state appear.
- `git diff --check` clean; `git status --short` shows only the new doc.

### M2 — Declarative configuration + bootstrap status/dry-run/apply

Entry criteria: M1 approved. Exit criteria:

- `machines/<id>.yaml` schema exists and validates; identity resolution via
  `LAI_MACHINE` works and refuses to guess.
- `bootstrap/status` is read-only and reports the §9 states.
- `bootstrap/apply --dry-run` prints planned writes, backups, and privilege/restart
  needs without writing.
- `bootstrap/apply` is idempotent, backs up before every change, and only touches
  owned files/blocks.
- Secret deny-list gate runs before apply and before commit.
- No daemon, no network sync, no full-directory replacement.
- Tests cover: idempotent re-apply, drift reporting, rollback of an owned block,
  and secret-gate rejection.

### M3 — Graphify freshness + generalized drift detection

- Local graph sidecar manifest schema implemented per §10; never committed.
- `status`/`graph status` report `CURRENT`/`STALE`/`MISSING`/`BLOCKED` for the graph
  and never rebuild it.
- Repository identity is remote-based (path-independent) and credential-sanitized.
- Graph root resolving inside the repository (including via symlink) is `BLOCKED`.
- No false `CURRENT` without provenance; an existing graph is not adopted.
- Drift generalizes to managed units; `NOT_MANAGED` capability is separate from
  drift; `LOCAL-OVERRIDE` only from the final local layer.
- `STALE`/`MISSING` graph never blocks unrelated safe apply.

### M4 — Cross-machine verification + documentation

- A repeatable two-machine verification procedure confirms equivalent behavior on
  work-mac and home-wsl after `pull → status → dry-run → apply → verify`.
- Derived-learning sharing is documented as reviewed/consented-only (no raw data).
- Operating docs updated; `verify-environment` still passes.

## 16. M2 implementation (delivered)

Repo-native CLI `scripts/bootstrap` (Python stdlib only), matching the
`scripts/ai-profile` style. `bootstrap/status` in §8 is realized as
`scripts/bootstrap status`. The entrypoint is a portable `sh` launcher that
selects an installed Python >= 3.11 (stdlib `tomllib`; no vendored parser, no
downloaded interpreter) and forwards arguments and exit code unchanged; set
`LAI_PYTHON` to force an interpreter.

```bash
LAI_MACHINE=work-mac ./scripts/bootstrap status
LAI_MACHINE=work-mac ./scripts/bootstrap apply --dry-run
LAI_MACHINE=work-mac ./scripts/bootstrap apply
LAI_MACHINE=work-mac ./scripts/bootstrap rollback          # restore latest backup
LAI_MACHINE=work-mac ./scripts/bootstrap rollback --dry-run
./scripts/bootstrap secrets                                 # scan managed sources
```

`--machine <id>` is accepted as an explicit alternative to `LAI_MACHINE`; hostname
is never used to guess. Without a machine, `status` reports `BLOCKED` and `apply`
refuses to mutate anything.

Source of truth and layout:

- `config/bootstrap.yaml` — portable desired state (units + defaults). No secrets.
- `config/platform/macos.yaml`, `config/platform/linux.yaml` — platform layer.
- `machines/work-mac.yaml`, `machines/home-wsl.yaml` — committed, non-secret profiles.
- `templates/codex/AGENTS.block.md`, `templates/headroom/mcp-server.toml` — managed
  content.
- `scripts/common/bootstrap.py` — load/precedence/plan/apply engine.
- `scripts/common/secret_guard.py` — deny-by-default secret scanner.
- Local override: `$LAI_STATE_DIR/machine.local.yaml` (git-ignored location), merged
  last per key; `disabled_units` there yields `LOCAL-OVERRIDE`.
- Backups: `$LAI_STATE_DIR/backups/bootstrap/<stamp>/manifest.json` (0700/0600),
  distinct from `ai-profile` backups so the two rollbacks never cross.

Owned vs not-managed in M2/M3 (see §9 for the ownership model):

- Applied: `codex.guidance` (managed block in `~/.codex/AGENTS.md`),
  `codex.model` (owned top-level `model` key in `~/.codex/config.toml`, machine-scoped:
  active only where the profile declares `codex.model`) and `headroom.mcp` (owned
  `[mcp_servers.headroom]` table in the same file), `headroom.defaults` (generated
  desired-defaults file under the state dir).
- When `~/.codex/config.toml` is absent, the two `config.toml` units report `MISSING`
  and never fabricate a whole file. When it exists, an absent table/key is created in
  place and a drifted one is rewritten only within its owned table/key; unrelated
  keys, tables, comments, and project-trust blocks are preserved.
- Classified read-only: `graphify.freshness` (warn-only; never written by apply).
- `NOT_MANAGED` (reported with a reason, never `BLOCKED`): `rtk.guidance`,
  `opencode.config`. Runtime AUTO verification remains M4.

No daemon, no network sync, no whole-directory replacement, and no file outside the
owned blocks/keys is written.

## 17. M3 implementation (delivered)

Repository identity, freshness, and generalized status/ownership.

```bash
LAI_MACHINE=work-mac ./scripts/bootstrap status        # managed + not-managed, read-only
LAI_MACHINE=work-mac ./scripts/bootstrap graph status  # graph freshness only
```

- Repository identity (`scripts/common/bootstrap.py:compute_repository`): sanitized
  remote (`remote:<url>` with credentials stripped) when present, else
  `name:<repo-name>`. Absolute path is diagnostics only; identity does not depend on
  path equality.
- Graphify version (`detect_graphify_version`): `graphify --version` parsed
  conservatively; never installs anything. Missing/unparseable version yields
  `STALE` (unverified), not a false `CURRENT`.
- Freshness (`evaluate_graph_freshness`): rule in §10. Path safety rejects any graph
  dir that resolves inside the repository, including via symlink, as `BLOCKED`.
- Manifest writer (`write_graph_manifest`): atomic (`0600`), sanitized, secret-scanned;
  used by a future rebuild step and by tests. It refuses to write inside the repo.
- Ownership vs drift: each unit reports `ownership` (`MANAGED`/`NOT_MANAGED`),
  `mutability` (`APPLY`/`NONE`), `capability`, and — only when managed — a drift
  state. Deferred components are `NOT_MANAGED`, never `BLOCKED`. `status` prints a
  `MANAGED` block (with states) and a separate `NOT MANAGED` block (no states).
- Lifecycle: desired is `wrapper`; `launchd`/`systemd` appear only under
  `lifecycle_capabilities`. No service registration is created or managed.
- Apply safety: only hard `BLOCKED` units with `mutability=APPLY` abort an apply.
  `STALE`/`MISSING` graph freshness never blocks unrelated config repair.
- Placeholder resolution is recursive and deterministic: known placeholders expand
  until none remain, bounded by `MAX_RESOLVE_DEPTH`; a cycle or an unresolved
  placeholder is a config error (`BLOCKED`), never a silent guess. This is why the
  portable `graphify.graph_dir` (`{{graph_root}}/{{repository.name}}/graphify-out`)
  resolves for machines that do not override it (for example `home-wsl`).
- Several owned units may share one file (both `codex.model` and `headroom.mcp`
  target `config.toml`). Apply re-plans before writing each unit so the edits compose
  instead of overwriting one another from a stale snapshot.

### Status exit-code contract

- `0` — report produced; `ALIGNED`/`DRIFTED`/`MISSING`/`LOCAL-OVERRIDE`/`STALE`
  are all success (status is informational; CI can inspect the text/JSON later).
- `1` — a true `BLOCKED` condition (unsafe/malformed/ambiguous).
- `2` — command/config error (missing `LAI_MACHINE`, unreadable config, bad args).

`apply`: `0` success or no-op; `1` hard `BLOCKED` refusal (no mutation); `2` error.
`rollback`: `0` success; `2` error.

### Graph rebuild (M3.1)

Explicit, user-commanded only:

```bash
LAI_MACHINE=work-mac ./scripts/bootstrap graph rebuild
```

- Never automatic: `status`, `apply`, `apply --dry-run`, `oc`, OpenCode/Codex
  startup, and shell startup never invoke a rebuild. Only this command does.
- Invocation uses the installed Graphify CLI with local, key-free extraction:
  `graphify extract <repo> --code-only --no-cluster --out <staging>`. No
  LLM/network extraction; `GRAPHIFY_*` env vars are cleared for the child so no
  external redirect/compare leaks in.
- Staging/promotion: the graph is built in a sibling `.lai-staging-<id>` directory,
  validated, then promoted by moving the previous `graphify-out` aside and renaming
  the staged artifact into place (same filesystem). The previous graph and manifest
  remain untouched until the new graph validates. On any failure the previous graph
  is preserved, staging (owned by this run) is removed, and the command returns
  non-zero.
- Clean worktree required: before creating staging or invoking Graphify, rebuild runs
  `git status --porcelain --untracked-files=normal` on the repository. Tracked,
  staged, deleted, and untracked files all block; ignored local/runtime files do
  not. On a dirty tree the rebuild refuses with a concise reason, invokes nothing,
  changes neither the graph nor the manifest, and returns non-zero. Nothing is
  committed, stashed, reset, or cleaned automatically. Provenance is therefore
  valid only for a clean Git working tree at build time.
- HEAD consistency: HEAD is captured before the build and re-checked after; if it
  changed, promotion is refused and provenance is not written. Clean-worktree and
  HEAD stability are both required.
- Manifest written last: `lai-freshness.json` is written only after a non-empty,
  parseable graph is promoted, and passes the secret gate. Counts
  (`node_count`/`edge_count`/`source_count`) are captured only when present.
- Lifecycle: after a successful rebuild `graph status` reports `CURRENT`; a later
  HEAD or Graphify version change, or a dirty working tree, reports `STALE`; a
  missing manifest is `MISSING`.

M4 uses this explicit rebuild on each machine before final cross-machine
verification. Nothing rebuilds automatically.

### M4 boundary

Cross-machine runtime verification (work-mac and home-wsl), RTK AUTO runtime
verification, Graphify AUTO agent behavior, and Headroom health/integration are M4.
M3 makes no home-wsl runtime claims.

## 18. Runtime Doctor V0.1 (delivered)

Answers one question read-only: **"Is this machine ready for AI coding right
now?"** It is diagnostic only and mutates nothing.

```bash
LAI_MACHINE=work-mac ./scripts/bootstrap doctor
LAI_MACHINE=work-mac ./scripts/bootstrap doctor --json
```

### Doctor vs status

Three commands, three semantics, deliberately separate:

- `bootstrap status` — declarative desired-state alignment (`ALIGNED / DRIFTED /
  MISSING / LOCAL-OVERRIDE / STALE / BLOCKED`).
- `bootstrap graph status` — Graphify provenance/freshness only.
- `bootstrap doctor` — runtime readiness (`PASS / WARN / FAIL / SKIP`).

Doctor never applies config, creates backups, rebuilds Graphify, starts/stops
Headroom (or anything else), launches OpenCode/Codex agent tasks, edits shell
config, installs/updates tools, or writes repo files. It does not reuse bootstrap
drift states; their semantics differ.

### Diagnostic statuses

- `PASS` — runtime prerequisite is healthy.
- `WARN` — usable but outside the tested baseline, or a non-critical capability
  missing (for example a dirty worktree or an unverified newer tool version).
- `FAIL` — a required runtime prerequisite is broken.
- `SKIP` — not applicable or intentionally unavailable (component not required).

`repository.worktree` is `WARN` when dirty — coding normally dirties a tree, so a
dirty tree never fails doctor. Graphify freshness may separately become `STALE`.

### Exit codes

- `0` — no `FAIL` checks (`PASS`/`WARN`/`SKIP` permitted); `ready` is true.
- `1` — one or more `FAIL` checks; `ready` is false.
- `2` — doctor could not execute (missing `LAI_MACHINE`, unknown machine,
  unreadable config/command failure).

### Known-good baseline

`config/tool-baseline.yaml` is the declarative, versioned tested baseline (schema
1) describing the versions and capabilities actually verified on the approved
commit. It is a **tested baseline, not a hard pin**:

- installed version `==` verified → `PASS`/verified.
- installed version differs (newer or older) → `WARN` (exact-match is the safe
  V0.1 default deliberately). Doctor never upgrades or downgrades anything.

Versions legitimately differ per machine, so each tool's `verified_version` is the
default (work-mac verified) and an optional `per_machine` map overrides it by
machine id (for example `home-wsl`). Capability flags stay **separate** from
versions: they record verified baseline behavior, never live runtime state.

### Capability vs runtime AUTO

The baseline capability flags (`rtk_auto`, `graphify_auto`, `headroom_proxy`,
`headroom_mcp`, ...) record **verified baseline capability**, not live runtime
state. Doctor reports current probes separately and never claims runtime AUTO from
the baseline or from config alone. For example: RTK AUTO is baseline-verified and
doctor reports the guidance/integration files present, but runtime AUTO is **not**
re-tested by doctor.

### Checked components

Repository (HEAD, branch, clean/dirty, local remote-tracking alignment without
network), bootstrap (managed-state alignment via `status` semantics + secret
scan), OpenCode (binary, version, wrapper presence; never launches an agent),
Codex (binary, version, `~/.codex` config paths readable; no account/model call),
RTK (binary, version, integration files), Graphify (binary, version, external
graph path safety + reused M3 freshness engine; never rebuilds), Headroom (binary,
version, bounded HTTP `/health` on the configured loopback endpoint; never
restarts), and the Codex Headroom MCP declaration (command resolvable; never
executes the MCP or a model request).

Requirement is per profile (`config/bootstrap.yaml` `doctor.components.<name>.required`;
`false` yields `SKIP` for absent pieces). Doctor honors `LAI_MACHINE` and never
guesses identity. It prints no secrets, tokens, credentials, full configs, or
environment dumps.

### Home WSL

Portable support exists through the shared profile/config model, but **home-wsl is
not runtime-verified here**. M4 home-wsl remains pending; doctor makes no home-wsl
runtime claims and is not run against it in this milestone.
