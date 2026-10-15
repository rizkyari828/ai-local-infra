# Documentation Synchronization V0.1 — Claim Inventory

Milestone: MASTER I (documentation synchronization and impact safety).
Base commit: `a8ff68acae4b20d190b8ddcb9226512b1a5817d6` (branch `docs/synchronization-v0.1`).
Scope: `docs/**/*.md` only. No code, test, config, machine, model, or template file is
changed. This file is the only new document created by the milestone.

Method: each document was read and every claim about *implementation status*
("not implemented", "future", "planned", "deferred", "pending", "will be", "design
only") was classified **CURRENT**, **STALE**, or **UNVERIFIABLE**, with evidence (a
repository file, a passing test, or a commit). A classification without evidence is
marked UNVERIFIABLE. Editing rule: a claim is edited only when STALE with concrete
evidence (E2). Frozen-contract documents are never edited (E6 / section 6).

## Coupling map verification (C1–C6)

| ID | Coupling | Verified | Evidence |
|---|---|---|---|
| C1 | `tests/test_knowledge_contract.py` uses `"docs/ARCHITECTURE.md"` as fixture data | EXISTS | `tests/test_knowledge_contract.py:29,87,173,176` |
| C2 | `tests/test_execution_contract.py` / `tests/test_context_pack.py` cite `docs/RUNTIME_AUTHORITY.md` | EXISTS | `tests/test_execution_contract.py:76`, `tests/test_context_pack.py:260` |
| C3 | `knowledge_contract.normalize_locator` normalizes doc-path locators | EXISTS | `tests/test_knowledge_contract.py:173-176` (`./docs//./ARCHITECTURE.md` → `docs/ARCHITECTURE.md`) |
| C4 | runtime docstrings name docs | EXISTS | `authority.py:28`, `bootstrap_cli.py:6`, `models_cli.py:10`, `orchestrate_cli.py:20-21`, `route_cli.py:11` |
| C5 | `docs/SHARED_KNOWLEDGE_CONTRACT.md` documents the frozen contract | EXISTS | file present (9407 bytes); not edited |
| C6 | `docs/MASTER_B.md` gate-status claims | CURRENT | `git log -- docs/MASTER_B.md` → only `965fad5`; branch not merged |

No new coupling point was found beyond C1–C6.

## Claim inventory (all 33 documents)

| # | Document | Status claim(s) (short) | Class | Evidence |
|---|---|---|---|---|
| 1 | ACCEPTANCE_CONTEXT.md | live literal-output handoff unverified; no live smoke | CURRENT | `tests/test_acceptance_context.py` (offline); `0244b6c` |
| 2 | AGENT_RUNTIME.md | ":119 Routing is not implemented"; ":34 a future DeepSeekProvider" | **STALE** | `scripts/common/routing.py`, `route_cli.py`, `failover.py`; `tests/test_routing.py`; `scripts/common/providers.py:579` |
| 3 | AGENT_TASK_TEMPLATE.md | none (authoring template) | — | file contains no implementation-status claims |
| 4 | ARCHITECTURE.md | ":70 Deliberately absent: PostgreSQL/…/multi-hop routing/provider scoring" | CURRENT | no such dependencies in `scripts/`, `config/` |
| 5 | CACHE_TELEMETRY.md | ":4 a future router can make evidence-based provider decisions" | UNVERIFIABLE | a router exists (`routing.py`) but telemetry-driven provider selection is not established |
| 6 | CONTEXT_RETRIEVAL.md | ":110 Deferred: efficiency telemetry, vector/embeddings…" | CURRENT | deferred items absent from `scripts/`; cache noted implemented |
| 7 | DERIVED_LEARNING.md | duplicate finalization refused; `--apply` deferred in V0.2 | CURRENT | `scripts/common/evidence.py`, `learn_cli.py` |
| 8 | DETERMINISTIC_FIRST.md | "V0.1 makes no live AI planner calls"; not implemented list | CURRENT | `scripts/common/efficiency.py` |
| 9 | DIRECT_MODE.md | deferred: UI, HTTP API, cross-machine caches, parallel workers | CURRENT | `scripts/common/result_cache.py`; no HTTP API |
| 10 | EFFICIENCY_TELEMETRY.md | `API_OR_UI_FUTURE` reserved; deferred aggregation | CURRENT | no UI/API; `efficiency.py` metadata-only |
| 11 | ESCALATION.md | single bounded hop; CodexProvider/multi-hop deferred | CURRENT | `providers.py` has no `CodexProvider`; `escalation.py` |
| 12 | EVAL_PROMOTION.md | `run-eval` discovers `evals/*/*.yaml` | CURRENT | `scripts/run-eval`; `evals/regression/…` |
| 13 | EXECUTION_CONTRACT.md | V0.1 adds no new quota-saving feature | SKIP (contract doc) | not edited (section 6) |
| 14 | EXECUTION.md | ":120 Not implemented: … central DB, …" | UNVERIFIABLE | a machine-local SQLite store exists (`orchestration_store.py`); "central DB" wording ambiguous |
| 15 | HARDWARE_PROFILE.md | machine RAM/GPU capture | CURRENT / UNVERIFIABLE | `hardware/current-machine.yaml`; some capture notes have no artifact |
| 16 | LEARNING_LOOP.md | no training/autonomous promotion; unknown data never success | CURRENT | `scripts/common/learning.py` |
| 17 | MASTER_B.md | gate: Home WSL DEFERRED; feature branch, no merge | CURRENT | `git log -- docs/MASTER_B.md` → `965fad5` only |
| 18 | MODEL_CAPABILITIES.md | ":92 A future milestone will seed verified historical observations" | **STALE** | `scripts/common/derived_knowledge.py:48`, `evidence.py:231`; commit `ec03e66` |
| 19 | MODEL_ROUTING.md | ":122 Actual token measurement belongs to later telemetry, not V0.1" | UNVERIFIABLE | `telemetry.py` has token fields; whether they populate actual measurement is unproven |
| 20 | MULTI_MACHINE_SYNC_V0_1.md | ":3 Design only. No bootstrap, apply, sync mechanism implemented" | **STALE** (not edited) | `scripts/bootstrap`, `scripts/common/bootstrap.py`, `machines/*.yaml` exist — but the file is a self-declared frozen contract (section 6) |
| 21 | OPERATIONAL_DATA_CONTRACT.md | title `V0.3` vs code `SCHEMA_VERSION = 4` | UNVERIFIABLE | `orchestration_store.py:34`; doc/or schema versioning intent not provable |
| 22 | OPERATIONS.md | ":78 V0.1 plans only: no model call, no worker, no persistence" | **STALE** | `orchestrate_cli.py:121 cmd_run` → `execution.execute_plan`; `OPERATIONS.md:81` documents `run` |
| 23 | ORCHESTRATION_ROUTING_FIX.md | provider calls zero; no branch merged; no runtime started | CURRENT | `tests/test_orchestration_routing.py`; `git log main..HEAD` |
| 24 | ORCHESTRATION.md | ":7 V0.1 is plan-only… persists nothing"; ":114 Not implemented… live multi-model execution, worker spawn, automatic model/provider switching… Graphify rebuild" | **STALE** | `orchestrate_cli.py:121`; `scripts/common/orchestration_store.py`; `scripts/common/failover.py`; `scripts/common/bootstrap.py:443` |
| 25 | QWEN_WORKFLOW.md | deferred experiments; compaction tuning not applied | CURRENT | no profile change in repo |
| 26 | RESULT_CACHE.md | no destructive cache ops; cross-machine caches deferred | CURRENT | `scripts/common/result_cache.py`; no prune scheduler |
| 27 | RUNTIME_AUTHORITY.md | authority invariant spec | SKIP (C2; meaning preserved) | not edited (section 6) |
| 28 | RUNTIME_GATEWAY.md | pass-through only; dynamic routing deferred | CURRENT | `scripts/common/gateway.py` |
| 29 | RUNTIME_OBSERVABILITY.md | `oc` not finalized; circuit breakers deferred | CURRENT | `scripts/record-opencode-outcome`; no breaker code |
| 30 | SHARED_KNOWLEDGE_CONTRACT.md | V0.1 implements no claim/lock/lease; later-phase participation | SKIP (frozen) | not edited (section 6) |
| 31 | TEACHER_STUDENT_LOOP.md | single opt-in hop; training deferred with consent | CURRENT | `docs/ESCALATION.md`; `learning.py` |
| 32 | TOOL_RUNTIME.md | no planner/ReAct/router; content-change detection deferred | CURRENT | `scripts/common/tool_runtime.py` |
| 33 | VERIFIED_RESULT_HANDOFF.md | no live inference; live handoff unverified | CURRENT | `tests/test_verified_handoff.py` (offline stubs) |

Totals: 33 documents classified; **4 STALE claims edited** (docs 2, 18, 22, 24);
**3 STALE claims recorded but not edited** (doc 20 frozen-contract; docs 5, 14, 19
and 21 marked UNVERIFIABLE rather than asserted); 0 UNVERIFIED placeholders added.

## Dispositions

Edits applied (each one document, one topic, one commit):

- **AGENT_RUNTIME.md** — routing/provider integration status (evidence:
  `routing.py`, `route_cli.py`, `failover.py`, `tests/test_routing.py`,
  `providers.py:579`).
- **MODEL_CAPABILITIES.md** — historical seeding is implemented (evidence:
  `derived_knowledge.py:48`, `evidence.py:231`, `ec03e66`).
- **OPERATIONS.md** — the `plan` command is not the whole product; `run` executes
  (evidence: `orchestrate_cli.py:121`, `OPERATIONS.md:81`). The specifically cited
  line 141 ("Future storage split (documented, not implemented)") was re-measured and
  is **CURRENT**, not stale — no PostgreSQL/ClickHouse/pgvector/Redis exists in the
  tree — so it was left unchanged; the accurate stale claim in the same file is at
  line 70–78.
- **ORCHESTRATION.md** — plan-only and not-implemented claims (evidence:
  `orchestrate_cli.py:121`, `orchestration_store.py`, `failover.py`,
  `bootstrap.py:443`).

Skipped with owner (section 6):

- `docs/MULTI_MACHINE_SYNC_V0_1.md` — self-declared **frozen contract** (M1).
  Owner: multi-machine sync milestone. Its `:3` status claim is stale
  (`scripts/bootstrap`, `scripts/common/bootstrap.py`, `machines/*.yaml` exist) but
  editing a frozen contract's claims is out of scope for this milestone.
- `docs/SHARED_KNOWLEDGE_CONTRACT.md` — frozen contract semantics.
- `docs/RUNTIME_AUTHORITY.md` — coupling C2; invariant meaning must not change.
- `docs/EXECUTION_CONTRACT.md` — contract document.
- `docs/MASTER_B.md` — gate-status document; no evidence-backed change.
- `docs/SHARED_KNOWLEDGE_RUNTIME.md` — owned by MASTER F; exists only in the Master D
  worktree and does not appear in this checkout.

Claims needing a code change (not fixable in docs):

- None. Every edited claim had a matching repository artifact; no claim required a
  code change to become consistent.
