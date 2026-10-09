# Result Cache + Incremental DAG Reuse V0.1

The third quota-saving layer. It answers one question deterministically, without any
model:

> **Does this task actually need to execute again?**

Layers:

1. Should this task use AI at all? → `TOOL_ONLY` / `NO_CHANGE_REQUIRED` / `AI`
2. If AI is required, what is the minimum sufficient context? → bounded context retrieval
3. Has this exact meaningful work already been completed against equivalent inputs?
   → **reuse safely instead of calling AI again** (this document)

The safety bias is explicit:

```text
SAFE CACHE MISS   >   UNSAFE CACHE HIT
```

If reuse eligibility cannot be proven deterministically, the task **executes normally**.
Reuse is never inferred from similar titles, similar prompt words, "the same model",
a matching repository HEAD alone, or "a previous task succeeded". Cache correctness
matters more than hit rate.

## Flows

```text
Master Objective
   ↓
Deterministic Plan
   ↓
Task Contract
   ↓
Context/Input Fingerprint
   ↓
Cache Lookup
   ├─ HIT  → Validate → Reuse → 0 AI calls
   └─ MISS
        ↓
      Execute
        ↓
      Acceptance
        ↓
      Eligible?
        ├─ yes → Cache bounded result
        └─ no  → no cache
```

Incremental propagation:

```text
Node input changes
   ↓
Node reruns
   ↓
Result fingerprint changed?
   ├─ no  → descendants may reuse
   └─ yes → affected descendants reevaluate
```

## Storage separation

The result cache is **local machine state**, never synchronized through Git and never
assumed to exist on another machine. Responsibilities stay separate:

| Location | Authority |
|---|---|
| `$LAI_STATE_DIR/orchestration/orchestration.db` | Run / Task / Attempt / Event (operational state) |
| `$LAI_STATE_DIR/learning/evidence.db` | learning outcome evidence |
| `knowledge/*` | portable verified knowledge (Git) |
| **`$LAI_STATE_DIR/cache/result-cache.db`** | **reusable local task results** |

The cache stores only bounded, structured, secret-safe accepted results. It is **not**
a knowledge store, learning database, prompt archive, or raw-response archive. No cache
files are committed. The operational DB records *provenance only* — it never duplicates
accepted result bodies; the cache remains the cache authority.

Engine: `scripts/common/result_cache.py` (stdlib `sqlite3`, WAL, `busy_timeout`,
incremental non-destructive migrations, `schema_version`). Also reachable via
`./scripts/orchestrate cache`.

## Cache eligibility

Not every successful task is cached. An AI task is reusable only when **all** required
conditions hold:

- task completed `SUCCESS`, acceptance criteria satisfied
- result is bounded and structured enough to reuse (an unstructured prose-only result is
  not cached)
- input identity is known (semantic contract + context + dependencies + freshness)
- repository/project identity matches
- dependencies are complete (each dependency has a known accepted-result fingerprint)
- freshness policy permits reuse; no unresolved failure / partial / read-only violation
- no explicit fresh-execution request

Failure outcomes are **never** stored or returned as successful cached results:
`FAILED`, `TIMEOUT`, `RATE_LIMIT`, `PROVIDER_503`, `ACCOUNT_MODEL_UNSUPPORTED`,
`MODEL_UNAVAILABLE`, `BACKEND_UNAVAILABLE`, `PROVIDER_UNAVAILABLE`,
`READ_ONLY_VIOLATION`, `PERMISSION_REQUIRED`, `CALL_BUDGET_EXHAUSTED`,
`CONTEXT_BUDGET_INSUFFICIENT`, `CONTEXT_UNSAFE`, `AI_REQUIRED`, `PARTIAL`, `CANCELLED`.
Failure evidence may still live in learning/operational data; it is not a cache entry.

`NO_CHANGE_REQUIRED` remains a deterministic no-op computed from current state. The
result cache does **not** replace no-op detection: a deterministic no-op is normally
recomputed where cheap and safe rather than blindly reused.

## Freshness policy

Repository-native, conservative names:

| Policy | Meaning |
|---|---|
| `PURE` | deterministic result derived only from immutable explicit inputs |
| `HEAD_BOUND` | requires matching relevant repository HEAD (HEAD participates in identity) |
| `INPUT_BOUND` | safe while exact relevant inputs/context/dependencies are identical (default for AI) |
| `RUNTIME_VOLATILE` | must execute (current runtime state matters) |
| `ALWAYS_EXECUTE` | policy explicitly disables reuse |

Volatile tasks never reuse an old result: current clean/dirty state, current provider
health, current runtime capability health, machine availability, live filesystem state,
current process state, permission prompts, rate-limit state. `TOOL_ONLY` defaults to
`RUNTIME_VOLATILE`; a pure deterministic operation reuses **only** when it explicitly
declares `PURE`. `repository_state` and `run_tests` therefore always rerun by default.
Freshness is never determined from vague task wording alone, and the default for an
unrecognised AI task is the safe middle (`INPUT_BOUND`).

## Cache identity

The primary key is **never** `hash(full user prompt)`, and the full master prompt is never
persisted. Identity is a deterministic semantic hash over bounded structured fields:

- project/repository identity (so unrelated repositories can never collide)
- task semantic fingerprint: executor, task family, task class, objective summary,
  acceptance criteria, acceptance contract version, required tier/effort, produced/
  consumed artifacts, tool args. **Run-local `task_id` (T1/T2/T6), timestamps, run ids,
  and list indices never participate.**
- context manifest fingerprint (order-independent)
- dependency fingerprints (accepted-result fingerprints of upstream tasks)
- freshness policy, schema/contract version, and (for `HEAD_BOUND`) repository HEAD

An explicit `semantic_key` lets callers *prove* equivalence across superficial wording;
without it, different wording produces a different fingerprint (conservative).
If semantic equivalence cannot be proven, the task is a cache miss.

### Context manifest integration

Cache identity reuses Context Retrieval V0.1's validated `ContextManifest`. The context
identity is order-independent and excludes the run-local task id, so reordered duplicate
context does not create false misses. Material context changes invalidate:

- source range changed → `FILE_RANGE`/`SYMBOL` fingerprint changed
- knowledge/rule version changed → `KNOWLEDGE` fingerprint changed
- dependency fact changed → dependency fingerprint changed
- acceptance criteria changed → semantic fingerprint changed

Secret-like values are redacted by context retrieval before hashing, so they never leak
into key material.

### Result fingerprint

The accepted reusable result is projected to a bounded structure (summary, decisions,
files changed, acceptance results, risk signals) with a deterministic fingerprint. It is
used for downstream dependency identity, descendant invalidation, and integrity
validation. It never depends on timestamps, run ids, or provider request ids.

## Incremental DAG reuse

Each node is classified `REUSE`, `RERUN`, or `UNKNOWN → RERUN safely`, with a bounded
reason code:

`UNCHANGED_INPUTS`, `SOURCE_CHANGED`, `CONTEXT_CHANGED`, `DEPENDENCY_CHANGED`,
`KNOWLEDGE_CHANGED`, `CONTRACT_CHANGED`, `ACCEPTANCE_CHANGED`, `FRESHNESS_POLICY`,
`CACHE_MISS`, `CACHE_INVALID`, `USER_FORCED_REFRESH`, `CAPABILITY_INCOMPATIBLE`,
`REPOSITORY_MISMATCH`, `WEAKER_CAPABILITY`, `EXPLICIT_MODEL_REQUEST`.

Descendants are **not** blindly rerun when an ancestor reruns. Invalidation follows the
meaningful dependency data:

```text
ancestor reruns
   ↓
compare accepted dependency fingerprint
   ├─ same  → descendant may stay reusable
   └─ changed → affected descendant reruns
```

Independent siblings are unaffected: if only one branch's relevant source changes, only
that branch (and descendants whose dependency fingerprint changed) reruns. If dependency
completeness cannot be proven, the decision falls back to a conservative rerun.

### Capability floor

A result produced under a weaker reasoning requirement never satisfies a stronger task:
cached `BALANCED`/`MEDIUM` does **not** satisfy `STRONG`/`HIGH`. A stronger cached result
(`STRONG`/`HIGH`) may satisfy a weaker task only when the semantic contract and
acceptance are otherwise equivalent, policy permits it, and no model-specific behavior is
required. Provider/model brand is never treated as intelligence proof.

## Explicit refresh and explicit model intent

- `--refresh` (or `force_refresh=True`): a cache entry is discovered but bypassed; the
  task executes and a new eligible result may supersede/add an entry. Entries are kept,
  never deleted.
- `--no-cache`: disables both reuse and writes for the run.
- An explicit model/backend request bypasses incompatible cache reuse unless reuse is
  explicitly allowed (`EXPLICIT_MODEL_REQUEST`).

Automatic fingerprint-based invalidation is preferred. V0.1 performs **no** destructive
cache operations (no "delete all because one file changed") and **no** pruning scheduler.
Invalid entries simply stop matching.

## Corruption and secret safety

Every entry is validated on read: schema version, project identity, result fingerprint,
and bounded-structure parse. A corrupt or incompatible entry yields `CACHE_INVALID`,
is ignored, and the run executes normally — a corrupt entry never fails a safe run.
Secret-like data is rejected on write and never logged. Never stored: API keys, tokens,
authorization headers, passwords, private keys, secret environment values, raw terminal
transcripts, full prompts, full model responses.

## AI-call accounting

A reused result consumes **0 AI calls**:

- no new `Attempt` row is created
- the AI-call budget is not consumed
- no provider/model resolution work is required to satisfy the task
- the original historical attempt stays attached to the original run/task
- the new task references the reused cache provenance

Reuse is represented as `status = SUCCESS` with `execution_source = CACHE` (not an
invented `CACHED` status). A bounded `TASK_REUSED` event communicates reuse without
overloading success/failure semantics. `CACHE_HIT`, `CACHE_MISS`, `CACHE_REJECTED`, and
`CACHE_ENTRY_WRITTEN` are bounded operational events; fingerprint comparisons are not
flooded into the timeline.

Hard `--max-ai-calls` applies to actual invocation attempts. Valid cache reuse is not
counted against it. Planning remains conservative when reuse cannot be proven until
later; a known-safe hit is not charged, and the guardrail is not weakened.

## Outcome learning separation

A cache hit must not create fake model-performance observations. Reused tasks record **no**
model success evidence (`source=observed_runtime` is not written), and the model success
statistics are unchanged. A cache miss or `CACHE_INVALID` is normal and never implies
model/provider weakness. Freshly executed AI successes keep their normal evidence
behavior.

## Local-machine scope

The cache is local machine state in V0.1. Cross-machine shared caches are out of scope;
safe cross-machine reuse is deferred. Project/repository identity is embedded in every
entry so results from unrelated repositories can never collide.

## Deferred

semantic/embedding cache, vector similarity, fuzzy or AI-powered cache matching, cloud /
distributed / cross-machine cache, Redis, external database, telemetry dashboard,
automatic surface hooks, direct-mode UI, parallel workers, cache pruning/retention
scheduler, and AI-based cache validation. Also deferred: moving cache lookup ahead of the
symbolic provider/health resolution step, and storing operational result bodies in the
orchestration DB.

No model is ever called to decide whether a result may be reused — the cache is
deterministic, and reuse is decided from the fingerprint comparison above.
