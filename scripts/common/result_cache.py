"""Result Cache + Incremental DAG Reuse V0.1 (deterministic, stdlib only).

Answers one question without calling a model: *does this task actually need to
execute again?* A previously accepted result is reused only when the relevant task
contract and inputs are provably unchanged. The safety bias is explicit:

    SAFE CACHE MISS   >   UNSAFE CACHE HIT

Reuse is NEVER inferred from similar titles, similar prompt words, "same model", a
matching repository HEAD alone, or "a previous task succeeded". If eligibility
cannot be proven deterministically, the task executes normally.

This module is the cache authority. It is separate from learning evidence
(``learning/evidence.db``), operational run state (``orchestration/orchestration.db``),
and portable knowledge (``knowledge/*``). It stores only bounded, structured,
secret-safe accepted results — never raw prompts or raw model responses.

Deterministic only: no embeddings, no vector similarity, no fuzzy/AI matching, no
network, no cross-machine sync.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import uuid
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import learning  # scripts/common, on sys.path (secret guard)
import routing  # scripts/common, on sys.path (tier/effort order)

ROOT = infra.ROOT
DB_SUBDIR = "cache"
DB_FILENAME = "result-cache.db"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
MAX_RESULT_JSON = 8000  # bounded structured accepted result

TASK_SEMANTIC_SCHEMA = "task-semantic-v1"
CONTEXT_SCHEMA = "context-v1"
INPUT_SCHEMA = "input-v1"
RESULT_SCHEMA = "result-v1"
ACCEPTANCE_CONTRACT_VERSION = "acceptance-v1"

# Freshness / reuse semantics (repository-native names, conservative defaults).
FRESHNESS_PURE = "PURE"
FRESHNESS_HEAD_BOUND = "HEAD_BOUND"
FRESHNESS_INPUT_BOUND = "INPUT_BOUND"
FRESHNESS_RUNTIME_VOLATILE = "RUNTIME_VOLATILE"
FRESHNESS_ALWAYS_EXECUTE = "ALWAYS_EXECUTE"
FRESHNESS_POLICIES = (FRESHNESS_PURE, FRESHNESS_HEAD_BOUND, FRESHNESS_INPUT_BOUND,
                      FRESHNESS_RUNTIME_VOLATILE, FRESHNESS_ALWAYS_EXECUTE)
REUSABLE_FRESHNESS = (FRESHNESS_PURE, FRESHNESS_INPUT_BOUND, FRESHNESS_HEAD_BOUND)

# Incremental DAG decisions.
REUSE = "REUSE"
RERUN = "RERUN"

# Bounded, stable reason codes.
REASON_UNCHANGED_INPUTS = "UNCHANGED_INPUTS"
REASON_SOURCE_CHANGED = "SOURCE_CHANGED"
REASON_CONTEXT_CHANGED = "CONTEXT_CHANGED"
REASON_DEPENDENCY_CHANGED = "DEPENDENCY_CHANGED"
REASON_KNOWLEDGE_CHANGED = "KNOWLEDGE_CHANGED"
REASON_CONTRACT_CHANGED = "CONTRACT_CHANGED"
REASON_ACCEPTANCE_CHANGED = "ACCEPTANCE_CHANGED"
REASON_FRESHNESS_POLICY = "FRESHNESS_POLICY"
REASON_CACHE_MISS = "CACHE_MISS"
REASON_CACHE_INVALID = "CACHE_INVALID"
REASON_USER_FORCED_REFRESH = "USER_FORCED_REFRESH"
REASON_CAPABILITY_INCOMPATIBLE = "CAPABILITY_INCOMPATIBLE"
REASON_REPOSITORY_MISMATCH = "REPOSITORY_MISMATCH"
REASON_WEAKER_CAPABILITY = "WEAKER_CAPABILITY"
REASON_EXPLICIT_MODEL = "EXPLICIT_MODEL_REQUEST"
REASON_RESULT_NOT_REUSABLE = "RESULT_NOT_REUSABLE"

# Volatile AI cues: current runtime state matters, so an old result must not be
# reused. Conservative — an unrecognised task defaults to INPUT_BOUND, never PURE.
_VOLATILE_CUES = (
    "provider health", "provider availability", "provider status", "runtime capability",
    "machine availability", "current clean state", "current dirty", "working tree state",
    "live filesystem", "filesystem state", "current process", "process state",
    "rate limit state", "rate-limit state", "permission prompt", "current runtime",
    "current head", "current branch", "current provider",
)

# Ineligible-as-success statuses: never stored or returned as a reusable success.
NEVER_REUSE_AS_SUCCESS = (
    "FAILED", "TIMEOUT", "RATE_LIMIT", "PROVIDER_503", "ACCOUNT_MODEL_UNSUPPORTED",
    "MODEL_UNAVAILABLE", "BACKEND_UNAVAILABLE", "PROVIDER_UNAVAILABLE",
    "READ_ONLY_VIOLATION", "PERMISSION_REQUIRED", "CALL_BUDGET_EXHAUSTED",
    "CONTEXT_BUDGET_INSUFFICIENT", "CONTEXT_UNSAFE", "AI_REQUIRED", "PARTIAL",
    "CANCELLED", "BLOCKED", "NOT_RUN", "UNKNOWN", "SKIPPED", "FAILURE",
)

_INVALID = object()  # corruption / schema-incompatibility sentinel


class CacheError(RuntimeError):
    pass


def db_path(state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    return pathlib.Path(state_dir or infra.STATE_DIR) / DB_SUBDIR / DB_FILENAME


def _assert_safe(target: pathlib.Path) -> pathlib.Path:
    resolved = target.expanduser().resolve()
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents:
        raise CacheError(f"refusing result cache inside the repository: {resolved}")
    return resolved


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", "replace")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Identity: task semantic, context, dependency, input, result
# --------------------------------------------------------------------------- #


def task_semantic_fingerprint(node: dict[str, Any], *,
                              semantic_key: Optional[str] = None,
                              acceptance_contract: str = ACCEPTANCE_CONTRACT_VERSION) -> str:
    """Deterministic signature of *what work the task means*. Run-local task_id and
    timestamps never participate. When an explicit ``semantic_key`` is supplied the
    free-text objective is excluded, so provably-equivalent wording collapses."""
    routing_c = node.get("routing") or {}
    objective = None if semantic_key else (node.get("objective_summary") or node.get("title") or "")
    material = {
        "schema": TASK_SEMANTIC_SCHEMA,
        "semantic_key": semantic_key,
        "executor": node.get("executor", "AI"),
        "task_family": node.get("task_family"),
        "task_class": node.get("task_class"),
        "objective_summary": objective,
        "acceptance_criteria": sorted(str(c) for c in (node.get("acceptance_criteria") or [])),
        "acceptance_contract": acceptance_contract,
        "required_tier": routing_c.get("tier"),
        "required_effort": routing_c.get("effort"),
        "produces_artifacts": sorted(str(p) for p in (node.get("produces_artifacts") or [])),
        "consumes_results_from": sorted(str(c) for c in (node.get("consumes_results_from") or [])),
        "tool": node.get("tool"),
    }
    return _hash(material)


def context_identity(manifest: Optional[dict[str, Any]]) -> Optional[str]:
    """Order-independent identity of a validated ContextManifest. Excludes the
    run-local task id; canonical sorting prevents false misses on item reordering."""
    if not manifest:
        return None
    items = []
    for item in manifest.get("items") or []:
        items.append({
            "source_kind": item.get("source_kind"), "source_id": item.get("source_id"),
            "path": item.get("path"), "symbol": item.get("symbol"),
            "range": list(item.get("range") or []), "fingerprint": item.get("fingerprint"),
            "required": bool(item.get("required")),
        })
    items.sort(key=_canonical)
    return _hash({"schema": CONTEXT_SCHEMA, "budget": manifest.get("context_budget"),
                  "graphify_status": manifest.get("graphify_status"), "items": items})


def input_fingerprint(*, project_id: str, task_semantic_fingerprint: str,
                      context_fingerprint: Optional[str], dependency_fingerprints: list[str],
                      freshness_policy: str, repository_head: Optional[str] = None) -> str:
    """Deterministic semantic input identity. Never based on the raw master prompt,
    timestamps, request ids, or temporary paths."""
    material = {
        "schema": INPUT_SCHEMA,
        "project_id": project_id,
        "task_semantic": task_semantic_fingerprint,
        "context": context_fingerprint,
        "dependencies": sorted(dependency_fingerprints),
        "freshness": freshness_policy,
        "acceptance_contract": ACCEPTANCE_CONTRACT_VERSION,
    }
    if freshness_policy == FRESHNESS_HEAD_BOUND:
        material["repository_head"] = repository_head
    return _hash(material)


def _bounded_text(value: Any, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def bounded_result(node: dict[str, Any], result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Extract the bounded, structured, reusable projection of an accepted result.
    Returns None when the result is not structured enough to reuse safely (e.g. an
    unbounded prose blob or a partial/failed result)."""
    if node.get("executor") == "TOOL_ONLY":
        payload = result.get("result")
        if not isinstance(payload, dict) or not payload:
            return None
        return {"tool_result": {str(k): payload[k] for k in sorted(payload)}}
    acceptance = result.get("acceptance_results")
    if not isinstance(acceptance, dict) or not acceptance:
        return None
    if not all(bool(ok) for ok in acceptance.values()):
        return None
    return {
        "summary": _bounded_text(result.get("summary")),
        "decisions": [_bounded_text(entry) for entry in (result.get("decisions") or [])],
        "files_changed": sorted(str(f) for f in (result.get("files_changed") or [])),
        "acceptance_results": {str(k): bool(v) for k, v in sorted(acceptance.items())},
        "new_risk_signals": sorted(str(s) for s in (result.get("new_risk_signals") or [])),
    }


def result_type(node: dict[str, Any]) -> str:
    return node.get("task_family") or node.get("task_class") or "generic_task"


def result_fingerprint(result_type_value: str, bounded: dict[str, Any]) -> str:
    """Deterministic fingerprint of an accepted reusable result. Used for downstream
    dependency identity, descendant invalidation, and integrity validation. Never
    depends on timestamps, run ids, or provider request ids."""
    return _hash({"schema": RESULT_SCHEMA, "type": result_type_value, "result": bounded})


# --------------------------------------------------------------------------- #
# Freshness policy
# --------------------------------------------------------------------------- #


def freshness_policy(node: dict[str, Any], *, constraints: Optional[dict[str, Any]] = None) -> str:
    explicit = (node or {}).get("freshness_policy")
    if explicit in FRESHNESS_POLICIES:
        return explicit
    if constraints:
        override = constraints.get("freshness_policy")
        if override in FRESHNESS_POLICIES:
            return override
    if (node or {}).get("executor") == "TOOL_ONLY":
        # Cheap deterministic operations are recomputed from current state; a pure
        # operation must explicitly opt in to reuse.
        return FRESHNESS_RUNTIME_VOLATILE
    text = (node.get("objective_summary") or "").lower()
    if any(cue in text for cue in _VOLATILE_CUES):
        return FRESHNESS_RUNTIME_VOLATILE
    return FRESHNESS_INPUT_BOUND


# --------------------------------------------------------------------------- #
# Cache eligibility
# --------------------------------------------------------------------------- #


def eligible_for_cache(node: dict[str, Any], result: dict[str, Any], *,
                       freshness: str) -> tuple[bool, Optional[str]]:
    """Deterministic gate for writing a reusable result. Never caches failed,
    partial, uncacheable-volatile, or non-structured results."""
    if result.get("status") in NEVER_REUSE_AS_SUCCESS or result.get("status") != "SUCCESS":
        return False, REASON_RESULT_NOT_REUSABLE
    if result.get("failure_category"):
        return False, REASON_RESULT_NOT_REUSABLE
    if freshness not in REUSABLE_FRESHNESS:
        return False, REASON_FRESHNESS_POLICY
    if node.get("executor") == "TOOL_ONLY" and freshness != FRESHNESS_PURE:
        return False, REASON_FRESHNESS_POLICY
    bounded = bounded_result(node, result)
    if bounded is None:
        return False, REASON_RESULT_NOT_REUSABLE
    if learning.contains_secret_like(_canonical(bounded)):
        return False, REASON_RESULT_NOT_REUSABLE
    return True, REASON_UNCHANGED_INPUTS


# --------------------------------------------------------------------------- #
# Capability compatibility (tier/effort floor — provider/model brand is NOT proof)
# --------------------------------------------------------------------------- #


def _axis_ok(order: list[str], cached: Optional[str], required: Optional[str]) -> Optional[bool]:
    if required is None:
        return True  # this axis carries no capability requirement (e.g. TOOL_ONLY)
    if cached is None or cached not in order or required not in order:
        return None  # unknown cached capability -> incompatible, never assumed sufficient
    return order.index(cached) >= order.index(required)


def _axis_stronger(order: list[str], cached: Optional[str], required: Optional[str]) -> bool:
    return (cached in order and required in order and order.index(cached) > order.index(required))


def capability_compatible(entry: dict[str, Any], *, required_tier: Optional[str],
                          required_effort: Optional[str], allow_stronger: bool = True) -> dict[str, Any]:
    """A weaker cached result must not satisfy a stronger task. A stronger cached
    result may satisfy a weaker task only when policy permits."""
    tier_order = list(routing.DEFAULT_TIER_ORDER)
    effort_order = list(routing.DEFAULT_EFFORT_ORDER)
    cached_tier = entry.get("original_required_tier")
    cached_effort = entry.get("original_required_effort")
    tier_ok = _axis_ok(tier_order, cached_tier, required_tier)
    effort_ok = _axis_ok(effort_order, cached_effort, required_effort)
    if tier_ok is None or effort_ok is None:
        return {"ok": False, "weaker": False, "reason": REASON_CAPABILITY_INCOMPATIBLE}
    if not tier_ok or not effort_ok:
        return {"ok": False, "weaker": True, "reason": REASON_WEAKER_CAPABILITY}
    stronger = (_axis_stronger(tier_order, cached_tier, required_tier)
                or _axis_stronger(effort_order, cached_effort, required_effort))
    if stronger and not allow_stronger:
        return {"ok": False, "weaker": False, "reason": REASON_CAPABILITY_INCOMPATIBLE}
    return {"ok": True, "weaker": False, "reason": REASON_UNCHANGED_INPUTS}


# --------------------------------------------------------------------------- #
# Storage (local machine state; never Git; never cross-machine)
# --------------------------------------------------------------------------- #

_MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
        """CREATE TABLE IF NOT EXISTS cache_entries (
            cache_entry_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            task_semantic_fingerprint TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
            context_manifest_fingerprint TEXT, result_fingerprint TEXT NOT NULL,
            result_type TEXT NOT NULL, bounded_result TEXT NOT NULL,
            original_run_id TEXT, original_task_id TEXT, original_backend TEXT,
            original_provider TEXT, original_model TEXT, original_required_tier TEXT,
            original_required_effort TEXT, original_native_effort TEXT,
            original_normalized_effort TEXT, freshness_policy TEXT NOT NULL,
            created_at TEXT, schema_version INTEGER)""",
        "CREATE INDEX IF NOT EXISTS idx_cache_lookup ON cache_entries(project_id, input_fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_cache_semantic ON cache_entries(task_semantic_fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_cache_created ON cache_entries(created_at)",
    ]),
]


class ResultCache:
    """Local, machine-scope reusable result cache. SQLite (stdlib), WAL, busy
    timeout, incremental non-destructive migrations."""

    def __init__(self, db: Optional[pathlib.Path] = None, *,
                 state_dir: Optional[pathlib.Path] = None) -> None:
        self.path = _assert_safe(db if db is not None else db_path(state_dir))
        self.connection = self._connect()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            connection = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000)
        except sqlite3.Error as exc:
            raise CacheError(f"cannot open result cache {self.path}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        return connection

    def _migrate(self) -> None:
        self.connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        current = self._version()
        for version, statements in _MIGRATIONS:
            if version <= current:
                continue
            try:
                with self.connection:
                    for statement in statements:
                        self.connection.execute(statement)
                    self.connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                                            (str(version),))
            except sqlite3.DatabaseError as exc:
                raise CacheError(f"cache migration {version} failed and rolled back: {exc}") from exc

    def _version(self) -> int:
        row = self.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self.connection.close()

    def _clean(self, value: Any, field: str, *, max_len: int = 200) -> Optional[str]:
        if value is None:
            return None
        text = str(value)
        if learning.contains_secret_like(text):
            raise CacheError(f"cache field '{field}' looks like a credential; refusing to store")
        if "\n" in text or len(text) > max_len:
            raise CacheError(f"cache field '{field}' must be short single-line text")
        return text

    def write(self, *, project_id: str, task_semantic_fingerprint: str, input_fingerprint: str,
              context_manifest_fingerprint: Optional[str], result_fingerprint: str, result_type: str,
              result: dict[str, Any], freshness_policy: str, original_run_id: Optional[str] = None,
              original_task_id: Optional[str] = None, original_backend: Optional[str] = None,
              original_provider: Optional[str] = None, original_model: Optional[str] = None,
              original_required_tier: Optional[str] = None, original_required_effort: Optional[str] = None,
              original_native_effort: Optional[str] = None,
              original_normalized_effort: Optional[str] = None) -> str:
        if freshness_policy not in FRESHNESS_POLICIES:
            raise CacheError(f"unknown freshness policy: {freshness_policy}")
        payload = _canonical(result)
        if len(payload) > MAX_RESULT_JSON:
            raise CacheError("result exceeds bounded cache size")
        if learning.contains_secret_like(payload):
            raise CacheError("result looks like a credential; refusing to cache")
        entry_id = uuid.uuid4().hex
        values = (entry_id, self._clean(project_id, "project_id"), self._clean(task_semantic_fingerprint,
                  "task_semantic_fingerprint", max_len=64), self._clean(input_fingerprint,
                  "input_fingerprint", max_len=64), self._clean(context_manifest_fingerprint,
                  "context_manifest_fingerprint", max_len=64), self._clean(result_fingerprint,
                  "result_fingerprint", max_len=64), self._clean(result_type, "result_type"),
                  payload, self._clean(original_run_id, "original_run_id"), self._clean(original_task_id,
                  "original_task_id"), self._clean(original_backend, "original_backend"),
                  self._clean(original_provider, "original_provider"), self._clean(original_model, "original_model"),
                  self._clean(original_required_tier, "original_required_tier"),
                  self._clean(original_required_effort, "original_required_effort"),
                  self._clean(original_native_effort, "original_native_effort"),
                  self._clean(original_normalized_effort, "original_normalized_effort"),
                  freshness_policy, _now(), SCHEMA_VERSION)
        with self.connection:
            self.connection.execute(
                "INSERT INTO cache_entries (cache_entry_id, project_id, task_semantic_fingerprint,"
                " input_fingerprint, context_manifest_fingerprint, result_fingerprint, result_type,"
                " bounded_result, original_run_id, original_task_id, original_backend, original_provider,"
                " original_model, original_required_tier, original_required_effort, original_native_effort,"
                " original_normalized_effort, freshness_policy, created_at, schema_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        return entry_id

    def lookup(self, project_id: str, input_fingerprint: str) -> Any:
        """Return the newest matching entry, None on miss, or the INVALID sentinel
        when the stored entry is corrupt or schema-incompatible. A corrupt entry is
        ignored safely; it never fails the run."""
        row = self.connection.execute(
            "SELECT * FROM cache_entries WHERE project_id=? AND input_fingerprint=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (project_id, input_fingerprint)).fetchone()
        if row is None:
            return None
        if int(row["schema_version"]) != SCHEMA_VERSION:
            return _INVALID
        try:
            bounded = json.loads(row["bounded_result"])
        except (json.JSONDecodeError, TypeError):
            return _INVALID
        if not isinstance(bounded, dict) or not row["result_fingerprint"]:
            return _INVALID
        if result_fingerprint(row["result_type"], bounded) != row["result_fingerprint"]:
            return _INVALID
        entry = dict(row)
        entry["result"] = bounded
        return entry

    def status(self) -> dict[str, Any]:
        count = self.connection.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
        span = self.connection.execute("SELECT MIN(created_at), MAX(created_at) FROM cache_entries").fetchone()
        per_type = {row[0]: row[1] for row in self.connection.execute(
            "SELECT result_type, COUNT(*) FROM cache_entries GROUP BY result_type")}
        return {"db_path": str(self.path), "schema_version": self._version(), "entries": count,
                "db_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "oldest": span[0], "newest": span[1], "result_types": per_type}


INVALID = _INVALID


def is_invalid(entry: Any) -> bool:
    return entry is _INVALID


# --------------------------------------------------------------------------- #
# Reuse decision engine
# --------------------------------------------------------------------------- #


def decide(*, store: ResultCache, project_id: str, node: dict[str, Any], identity: dict[str, Any],
           required_tier: Optional[str], required_effort: Optional[str], freshness: str,
           force_refresh: bool = False, explicit_model: bool = False,
           allow_stronger_reuse: bool = True, dependencies_complete: bool = True) -> dict[str, Any]:
    """Deterministic per-node decision: REUSE vs RERUN with a bounded reason code.
    Unknown/uncertain state always falls back to RERUN (safe cache miss)."""
    base = {"task_id": node.get("task_id"), "decision": RERUN, "reason": None, "entry": None,
            "input_fingerprint": identity.get("input_fingerprint"),
            "task_semantic_fingerprint": identity.get("task_semantic_fingerprint"),
            "context_manifest_fingerprint": identity.get("context_manifest_fingerprint"),
            "freshness_policy": freshness}
    if force_refresh:
        return {**base, "reason": REASON_USER_FORCED_REFRESH}
    if explicit_model:
        return {**base, "reason": REASON_EXPLICIT_MODEL}
    if not dependencies_complete:
        return {**base, "reason": REASON_DEPENDENCY_CHANGED}
    if freshness not in REUSABLE_FRESHNESS:
        return {**base, "reason": REASON_FRESHNESS_POLICY}
    if node.get("executor") == "TOOL_ONLY" and freshness != FRESHNESS_PURE:
        return {**base, "reason": REASON_FRESHNESS_POLICY}
    entry = store.lookup(project_id, identity["input_fingerprint"])
    if entry is None:
        return {**base, "reason": REASON_CACHE_MISS}
    if is_invalid(entry):
        return {**base, "reason": REASON_CACHE_INVALID}
    if entry.get("project_id") != project_id:
        return {**base, "reason": REASON_REPOSITORY_MISMATCH}
    compatibility = capability_compatible(entry, required_tier=required_tier,
                                          required_effort=required_effort, allow_stronger=allow_stronger_reuse)
    if not compatibility["ok"]:
        reason = REASON_WEAKER_CAPABILITY if compatibility["weaker"] else REASON_CAPABILITY_INCOMPATIBLE
        return {**base, "reason": reason}
    return {**base, "decision": REUSE, "reason": REASON_UNCHANGED_INPUTS, "entry": entry}
