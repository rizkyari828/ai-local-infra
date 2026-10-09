# Context Retrieval Optimization V0.1

Provide the **smallest context set that is still sufficient** for a task — not merely
less context, and never less than correctness requires. Context selection is
deterministic: no embeddings, no vector DB, no AI summarizer, no AI planner.

```text
AI Task
  ↓
Context Request      (bounded description: objective, acceptance, refs, facts)
  ↓
Retrieve             (deterministic: explicit refs, symbols, config, deps, knowledge)
  ↓
Deduplicate          (content fingerprints + overlap merge)
  ↓
Budget               (TINY/SMALL/MEDIUM/LARGE ceiling; required never dropped)
  ↓
Validate             (required present? fits? safe? provenance?)
  ↓
Context Pack         (bounded material for the adapter)
  ↓
AI Adapter
```

## Contract

`scripts/common/context_retrieval.py` defines:

- **ContextRequest** — task id/family/class/executor, `context_budget`, objective summary,
  acceptance criteria, explicit source/symbol/config/diff refs, dependency facts,
  knowledge refs, repository/HEAD identity. Never the raw master prompt.
- **ContextItem** — `source_kind` (bounded vocabulary), locator, path/symbol/range,
  knowledge id, reason, priority, required/optional, content fingerprint, size,
  truncated/redacted flags.
- **ContextManifest** — task, budget, item counts, size, omitted count, duplicate chars
  avoided, required-fits, `graphify_status`/`graphify_used`/`fallback_used`, per-item
  provenance, and a deterministic `manifest_fingerprint`.
- **ContextPack** — only bounded objective, acceptance criteria, and selected items
  (source kind, id, content). No full chat/repository/graph/knowledge.

## Budgets

`config/context-budgets.yaml` maps `TINY/SMALL/MEDIUM/LARGE` to character ceilings
(deterministic; no tokenizer is assumed). A budget is a **ceiling, not a target** — packs
are never padded. Optional items are dropped lowest-priority first; **required context is
never silently removed**. If required context cannot fit, the pack is rejected with
`CONTEXT_BUDGET_INSUFFICIENT` and the AI worker is **not** called (policy may raise the
budget instead).

## Retrieval order

1. explicit task references → 2. exact target symbols/files/config → 3. required
dependency facts → 4. changed ranges → 5. direct structural relationships →
6. relevant verified knowledge → 7. nearby support → 8. optional broader context.
Broader retrieval is a bounded fallback (symbol → enclosing range → related → bounded
file); never symbol-miss → whole repository.

- **Symbol/config**: retrieve symbol ranges and exact structured config values, not whole
  files.
- **Dependency facts**: compact structured facts (status/head/clean/…), never transcripts.
- **Knowledge**: only verified entries relevant to the task family/failure; stable ids.
- **Diff-aware**: bounded changed range plus required enclosing symbol.

## Graphify

Optional locator only; never rebuilt during retrieval. The manifest records
`graphify_status` (`CURRENT`/`STALE`/`MISSING`/`INVALID`), `graphify_used`, and
`fallback_used`. A stale/missing graph falls back to deterministic repository/file
lookup and is never trusted blindly. V0.1 uses deterministic retrieval and records
Graphify as advisory.

## Deduplication, delta, quality

Content fingerprints canonicalize identical items across paths; overlapping/adjacent
ranges for the same file merge into one item. Within a pack, a current fact supersedes an
older equivalent (e.g. the same HEAD appears once). Cross-call "memory" is **not**
assumed: a fresh worker call receives required context even if a prior attempt saw it.
Retry/failover reuses the same validated base pack (rebuilding only if required inputs
changed) and may add a new bounded failure fact. Quality floor: if analysis genuinely
needs several files/components, use MEDIUM/LARGE; never squeeze correctness into TINY.

## Validation, security, cache-readiness

Deterministic validation states: `CONTEXT_READY`, `CONTEXT_BUDGET_INSUFFICIENT`,
`CONTEXT_REQUIRED_SOURCE_MISSING`, `CONTEXT_UNSAFE`, `CONTEXT_STALE_SOURCE`. No model is
called after a validation failure (these are non-intelligence categories). Path
traversal/absolute/home reads are rejected; binaries and oversized files are skipped;
secret-like values are redacted to `[REDACTED]` while preserving structure, and never
leak into the pack or manifest. Manifest/source/dependency/knowledge fingerprints are
deterministic (no secrets, timestamps, or prompt text) and are consumed by the result
cache; see `docs/RESULT_CACHE.md` for reuse identity and eligibility.

## Operational integration (transitional)

Manifests are bounded local runtime artifacts written under
`$LAI_STATE_DIR/orchestration/<run-id>/context/<task-id>.json` (never Git, never a
database body). No operational DB schema change was required; the existing
`tasks.context_budget` field already records the budget. Full context bodies are never
persisted. TOOL_ONLY and NO_CHANGE_REQUIRED tasks build no AI pack.

Deferred: efficiency telemetry, vector/embeddings, external retrieval, AI-powered
selection, automatic surface hooks, parallel workers. Result cache + incremental DAG
reuse V0.1 is implemented on top of these fingerprints (see `docs/RESULT_CACHE.md`).
