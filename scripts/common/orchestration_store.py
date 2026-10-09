"""Operational Data Contract V0.1 — stable, versioned operational run state.

Separate from learning evidence (``$LAI_STATE_DIR/learning/evidence.db``) and from
shared portable knowledge (``knowledge/*``). This is data infrastructure, not UI:
Project -> Run -> Task -> Attempt, plus TaskDependency and RunEvent.

- SQLite (stdlib), incremental transactional migrations, explicit schema version.
- WAL + busy_timeout + foreign_keys for future concurrent readers.
- Current-state tables for fast queries + a small append-only event timeline
  (NOT full event sourcing).
- No duplicated authoritative config (rules/skills/registry/knowledge stay in
  YAML/config); no raw prompts/responses/logs; bounded structured payloads;
  secret-bearing values rejected.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import subprocess
import uuid
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import learning  # scripts/common, on sys.path (secret guard)

ROOT = infra.ROOT
DB_SUBDIR = "orchestration"
DB_FILENAME = "orchestration.db"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
MAX_PAYLOAD = 2000  # bounded structured event payload

RUN_STATUSES = ("PLANNED", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED")
TASK_STATUSES = ("PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED", "NO_CHANGE_REQUIRED",
                 "AI_REQUIRED", "BLOCKED")
EXECUTOR_TYPES = ("TOOL_ONLY", "AI")
EVENT_TYPES = (
    "RUN_CREATED", "RUN_STARTED", "RUN_COMPLETED", "RUN_FAILED",
    "TASK_CREATED", "TASK_STARTED", "TASK_COMPLETED", "TASK_FAILED", "TASK_NO_CHANGE_REQUIRED",
    "TASK_AI_REQUIRED", "ATTEMPT_STARTED", "ATTEMPT_SUCCEEDED", "ATTEMPT_FAILED",
    "FAILOVER_SELECTED", "RETRY_SELECTED", "CALL_BUDGET_UPDATED", "READ_ONLY_VIOLATION",
    "CALL_BUDGET_EXHAUSTED",
)
# Superset failure taxonomy (non-intelligence categories never imply model weakness).
FAILURE_TAXONOMY = (
    "RATE_LIMIT", "PROVIDER_503", "TIMEOUT", "ACCOUNT_MODEL_UNSUPPORTED", "MODEL_UNAVAILABLE",
    "BACKEND_UNAVAILABLE", "PROVIDER_UNAVAILABLE", "REASONING_INSUFFICIENT", "PERMISSION_REQUIRED",
    "READ_ONLY_VIOLATION", "CALL_BUDGET_EXHAUSTED", "TOOL_EXECUTION_FAILED",
    "DETERMINISTIC_CHECK_FAILED", "INSUFFICIENT_AI_CALL_BUDGET", "NO_CHANGE_REQUIRED",
    "AI_REQUIRED", "IMPLEMENTATION_ERROR", "INFRA_FAILURE", "AUTH_FAILURE", "SAFETY_GUARD",
    "STALE_SESSION", "CONFIG_DRIFT", "UNKNOWN_FAILURE",
)

# Known legacy-semantics learning observations (tier mislabelled as effort). Not rewritten.
LEGACY_SEMANTIC_INVALID_OBSERVATIONS = ("868087de1dda47059713f076ce26a5d9",)


class StoreError(RuntimeError):
    pass


def db_path(state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    return pathlib.Path(state_dir or infra.STATE_DIR) / DB_SUBDIR / DB_FILENAME


def _assert_safe(target: pathlib.Path) -> pathlib.Path:
    resolved = target.expanduser().resolve()
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents:
        raise StoreError(f"refusing operational DB inside the repository: {resolved}")
    return resolved


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _clean(value: Any, field: str, *, max_len: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if learning.contains_secret_like(text):
        raise StoreError(f"field '{field}' looks like a credential; refusing to store")
    if "\n" in text or len(text) > max_len:
        raise StoreError(f"field '{field}' must be short single-line text")
    return text


def _bounded_payload(payload: Optional[dict[str, Any]]) -> Optional[str]:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise StoreError("event payload must be an object")
    allowed = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            allowed[str(key)] = value
        elif isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
            allowed[str(key)] = value
        else:
            raise StoreError(f"unsupported event payload value for '{key}'")
    text = json.dumps(allowed, ensure_ascii=False, sort_keys=True)
    if len(text) > MAX_PAYLOAD:
        raise StoreError("event payload exceeds bounded size")
    if learning.contains_secret_like(text):
        raise StoreError("event payload looks like it may contain a credential")
    return text


# --------------------------------------------------------------------------- #
# Migrations (incremental, transactional; no destructive recreate)
# --------------------------------------------------------------------------- #

_MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
        """CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY, repository_id TEXT, name TEXT, fingerprint TEXT,
            created_at TEXT, schema_version INTEGER)""",
        """CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, project_id TEXT, machine_id TEXT, status TEXT,
            intent TEXT, planner_mode TEXT, planner_tier TEXT, planner_native_effort TEXT,
            planner_normalized_effort TEXT, hard_max_ai_calls INTEGER, planned_ai_calls INTEGER,
            used_ai_calls INTEGER, minimum_required_ai_calls INTEGER, tool_only_count INTEGER,
            start_head TEXT, final_head TEXT, objective_summary TEXT, failure_category TEXT,
            created_at TEXT, started_at TEXT, completed_at TEXT, schema_version INTEGER)""",
        """CREATE TABLE IF NOT EXISTS tasks (
            run_id TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT, summary TEXT,
            task_family TEXT, task_class TEXT, executor_type TEXT, status TEXT, intent TEXT,
            required_tier TEXT, required_effort TEXT, context_budget TEXT, conditional INTEGER,
            tool_operation TEXT, acceptance_type TEXT, failure_category TEXT, created_at TEXT,
            started_at TEXT, completed_at TEXT, schema_version INTEGER,
            PRIMARY KEY (run_id, task_id))""",
        """CREATE TABLE IF NOT EXISTS task_dependencies (
            run_id TEXT NOT NULL, task_id TEXT NOT NULL, depends_on_task_id TEXT NOT NULL,
            PRIMARY KEY (run_id, task_id, depends_on_task_id))""",
        """CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, attempt_no INTEGER,
            backend TEXT, surface TEXT, provider TEXT, model TEXT, required_tier TEXT,
            required_effort TEXT, native_effort TEXT, normalized_effort TEXT, status TEXT,
            failure_category TEXT, is_retry INTEGER, is_fallback INTEGER, completion_signal TEXT,
            started_at TEXT, completed_at TEXT, schema_version INTEGER)""",
        """CREATE TABLE IF NOT EXISTS run_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT,
            attempt_id TEXT, event_type TEXT, payload TEXT, created_at TEXT, schema_version INTEGER)""",
        "CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_runs_machine ON runs(machine_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_run_status ON tasks(run_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_run_task ON tasks(run_id, task_id)",
        "CREATE INDEX IF NOT EXISTS idx_attempts_task_no ON attempts(task_id, attempt_no)",
        "CREATE INDEX IF NOT EXISTS idx_attempts_model ON attempts(provider, model)",
        "CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status, completed_at)",
        "CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, event_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_task ON run_events(task_id, event_id)",
    ]),
]


class OrchestrationStore:
    def __init__(self, db: Optional[pathlib.Path] = None, *, state_dir: Optional[pathlib.Path] = None) -> None:
        self.path = _assert_safe(db if db is not None else db_path(state_dir))
        self.connection = self._connect()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            connection = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open operational DB {self.path}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
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
                with self.connection:  # transaction: commits or rolls back atomically
                    for statement in statements:
                        self.connection.execute(statement)
                    self.connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                                            (str(version),))
            except sqlite3.DatabaseError as exc:
                raise StoreError(f"migration {version} failed and rolled back: {exc}") from exc

    def _version(self) -> int:
        row = self.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self.connection.close()

    # -- write: events + state in one transaction where practical --------------- #
    def _event(self, run_id, event_type, *, task_id=None, attempt_id=None, payload=None) -> int:
        if event_type not in EVENT_TYPES:
            raise StoreError(f"unknown event type: {event_type}")
        cursor = self.connection.execute(
            "INSERT INTO run_events(run_id, task_id, attempt_id, event_type, payload, created_at, schema_version)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, task_id, attempt_id, event_type, _bounded_payload(payload), _now(), SCHEMA_VERSION))
        return cursor.lastrowid

    def ensure_project(self, project_id: str, *, repository_id: Optional[str] = None,
                       name: Optional[str] = None, fingerprint: Optional[str] = None) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO projects(project_id, repository_id, name, fingerprint, created_at, schema_version)"
                " VALUES (?,?,?,?,?,?)",
                (_clean(project_id, "project_id"), _clean(repository_id, "repository_id", max_len=300),
                 _clean(name, "name", max_len=300), _clean(fingerprint, "fingerprint", max_len=300),
                 _now(), SCHEMA_VERSION))

    def create_run(self, run_id: str, *, project_id: str, machine_id: str, status: str = "PLANNED",
                   intent: Optional[str] = None, planner_mode: Optional[str] = None,
                   planner_tier: Optional[str] = None, planner_native_effort: Optional[str] = None,
                   planner_normalized_effort: Optional[str] = None, hard_max_ai_calls: Optional[int] = None,
                   planned_ai_calls: Optional[int] = None, used_ai_calls: int = 0,
                   minimum_required_ai_calls: Optional[int] = None, tool_only_count: int = 0,
                   start_head: Optional[str] = None, objective_summary: Optional[str] = None) -> str:
        if status not in RUN_STATUSES:
            raise StoreError(f"unknown run status: {status}")
        now = _now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs(run_id, project_id, machine_id, status, intent, planner_mode, planner_tier,"
                " planner_native_effort, planner_normalized_effort, hard_max_ai_calls, planned_ai_calls,"
                " used_ai_calls, minimum_required_ai_calls, tool_only_count, start_head, objective_summary,"
                " created_at, started_at, schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_clean(run_id, "run_id"), _clean(project_id, "project_id"), _clean(machine_id, "machine_id"),
                 status, _clean(intent, "intent"), _clean(planner_mode, "planner_mode"),
                 _clean(planner_tier, "planner_tier"), _clean(planner_native_effort, "planner_native_effort"),
                 _clean(planner_normalized_effort, "planner_normalized_effort"), hard_max_ai_calls,
                 planned_ai_calls, used_ai_calls, minimum_required_ai_calls, tool_only_count,
                 _clean(start_head, "start_head", max_len=64),
                 _clean(objective_summary, "objective_summary", max_len=200), now, now, SCHEMA_VERSION))
            self._event(run_id, "RUN_CREATED", payload={"status": status, "intent": intent})
        return run_id

    def create_task(self, run_id: str, task_id: str, *, title: str, executor_type: str,
                    status: str = "PENDING", task_family: Optional[str] = None,
                    task_class: Optional[str] = None, intent: Optional[str] = None,
                    required_tier: Optional[str] = None, required_effort: Optional[str] = None,
                    context_budget: Optional[str] = None, conditional: bool = False,
                    tool_operation: Optional[str] = None, acceptance_type: Optional[str] = None,
                    summary: Optional[str] = None) -> None:
        if executor_type not in EXECUTOR_TYPES:
            raise StoreError(f"unknown executor type: {executor_type}")
        if status not in TASK_STATUSES:
            raise StoreError(f"unknown task status: {status}")
        with self.connection:
            self.connection.execute(
                "INSERT INTO tasks(run_id, task_id, title, summary, task_family, task_class, executor_type,"
                " status, intent, required_tier, required_effort, context_budget, conditional, tool_operation,"
                " acceptance_type, created_at, schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, _clean(task_id, "task_id"), _clean(title, "title", max_len=200),
                 _clean(summary, "summary", max_len=200), _clean(task_family, "task_family"),
                 _clean(task_class, "task_class"), executor_type, status, _clean(intent, "intent"),
                 _clean(required_tier, "required_tier"), _clean(required_effort, "required_effort"),
                 _clean(context_budget, "context_budget"), 1 if conditional else 0,
                 _clean(tool_operation, "tool_operation"), _clean(acceptance_type, "acceptance_type"),
                 _now(), SCHEMA_VERSION))
            self._event(run_id, "TASK_CREATED", task_id=task_id,
                        payload={"executor_type": executor_type, "status": status})
        return None

    def add_dependency(self, run_id: str, task_id: str, depends_on_task_id: str) -> None:
        if task_id == depends_on_task_id:
            raise StoreError("task cannot depend on itself")
        known = {row["task_id"] for row in self.connection.execute(
            "SELECT task_id FROM tasks WHERE run_id=?", (run_id,))}
        if task_id not in known or depends_on_task_id not in known:
            raise StoreError("dependency references an unknown task")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO task_dependencies(run_id, task_id, depends_on_task_id) VALUES (?,?,?)",
                (run_id, task_id, depends_on_task_id))

    def start_task(self, run_id: str, task_id: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE tasks SET status='RUNNING', started_at=? WHERE run_id=? AND task_id=?",
                                    (_now(), run_id, task_id))
            self._event(run_id, "TASK_STARTED", task_id=task_id)

    def finish_task(self, run_id: str, task_id: str, status: str, *, failure_category: Optional[str] = None) -> None:
        if status not in TASK_STATUSES:
            raise StoreError(f"unknown task status: {status}")
        event = {"SUCCESS": "TASK_COMPLETED", "NO_CHANGE_REQUIRED": "TASK_NO_CHANGE_REQUIRED",
                 "AI_REQUIRED": "TASK_AI_REQUIRED", "FAILED": "TASK_FAILED"}.get(status, "TASK_COMPLETED")
        with self.connection:
            self.connection.execute("UPDATE tasks SET status=?, completed_at=?, failure_category=?"
                                    " WHERE run_id=? AND task_id=?",
                                    (status, _now(), _clean(failure_category, "failure_category"), run_id, task_id))
            self._event(run_id, event, task_id=task_id, payload={"status": status, "failure_category": failure_category})

    def record_attempt(self, run_id: str, task_id: str, *, attempt_no: int, backend: Optional[str],
                       provider: Optional[str], model: Optional[str], native_effort: Optional[str],
                       normalized_effort: Optional[str], status: str, required_tier: Optional[str] = None,
                       required_effort: Optional[str] = None, failure_category: Optional[str] = None,
                       is_retry: bool = False, is_fallback: bool = False,
                       completion_signal: Optional[str] = None, started_at: Optional[str] = None) -> str:
        attempt_id = uuid.uuid4().hex
        now = _now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO attempts(attempt_id, run_id, task_id, attempt_no, backend, surface, provider, model,"
                " required_tier, required_effort, native_effort, normalized_effort, status, failure_category,"
                " is_retry, is_fallback, completion_signal, started_at, completed_at, schema_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, run_id, task_id, attempt_no, _clean(backend, "backend"),
                 _clean(backend, "surface"), _clean(provider, "provider"), _clean(model, "model"),
                 _clean(required_tier, "required_tier"), _clean(required_effort, "required_effort"),
                 _clean(native_effort, "native_effort"), _clean(normalized_effort, "normalized_effort"),
                 status, _clean(failure_category, "failure_category"), 1 if is_retry else 0,
                 1 if is_fallback else 0, _clean(completion_signal, "completion_signal"),
                 started_at or now, now, SCHEMA_VERSION))
            self._event(run_id, "ATTEMPT_STARTED", task_id=task_id, attempt_id=attempt_id,
                        payload={"attempt_no": attempt_no, "backend": backend, "model": model})
            if status in ("SUCCESS",):
                self._event(run_id, "ATTEMPT_SUCCEEDED", task_id=task_id, attempt_id=attempt_id,
                            payload={"normalized_effort": normalized_effort})
            else:
                self._event(run_id, "ATTEMPT_FAILED", task_id=task_id, attempt_id=attempt_id,
                            payload={"failure_category": failure_category})
            if is_fallback:
                self._event(run_id, "FAILOVER_SELECTED", task_id=task_id, attempt_id=attempt_id,
                            payload={"backend": backend})
            elif is_retry:
                self._event(run_id, "RETRY_SELECTED", task_id=task_id, attempt_id=attempt_id,
                            payload={"backend": backend})
        return attempt_id

    def finish_run(self, run_id: str, status: str, *, used_ai_calls: Optional[int] = None,
                   final_head: Optional[str] = None, failure_category: Optional[str] = None) -> None:
        if status not in RUN_STATUSES:
            raise StoreError(f"unknown run status: {status}")
        event = "RUN_COMPLETED" if status == "SUCCEEDED" else "RUN_FAILED"
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status=?, completed_at=?, used_ai_calls=COALESCE(?, used_ai_calls),"
                " final_head=COALESCE(?, final_head), failure_category=? WHERE run_id=?",
                (status, _now(), used_ai_calls, _clean(final_head, "final_head", max_len=64),
                 _clean(failure_category, "failure_category"), run_id))
            self._event(run_id, event, payload={"status": status})

    # -- read/query layer (no CLI text parsing) -------------------------------- #
    def status(self) -> dict[str, Any]:
        def count(table: str) -> int:
            return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        span = self.connection.execute("SELECT MIN(created_at), MAX(created_at) FROM runs").fetchone()
        return {"db_path": str(self.path), "schema_version": self._version(),
                "projects": count("projects"), "runs": count("runs"), "tasks": count("tasks"),
                "dependencies": count("task_dependencies"), "attempts": count("attempts"),
                "events": count("run_events"), "oldest": span[0], "newest": span[1],
                "db_size_bytes": self.path.stat().st_size if self.path.exists() else 0}

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT run_id, project_id, machine_id, status, intent, planner_mode, hard_max_ai_calls,"
            " used_ai_calls, minimum_required_ai_calls, tool_only_count, failure_category, created_at"
            " FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def run_tasks(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY task_id", (run_id,))
        return [dict(row) for row in rows]

    def run_dependencies(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT task_id, depends_on_task_id FROM task_dependencies WHERE run_id=?",
                                       (run_id,))
        return [dict(row) for row in rows]

    def task_attempts(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no", (task_id,))
        return [dict(row) for row in rows]

    def run_events(self, run_id: str, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM run_events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?",
            (run_id, after, limit))
        return [dict(row) for row in rows]

    def execution_summary(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT t.status AS status, COUNT(*) AS n, COALESCE(SUM(r.used_ai_calls),0) AS ai FROM tasks t"
            " JOIN runs r ON r.run_id=t.run_id WHERE t.run_id=? GROUP BY t.status", (run_id,)).fetchall()
        return {"run_id": run_id, "tasks_by_status": {r["status"]: r["n"] for r in row}}

    def legacy_observation_excluded(self, observation_id: str) -> bool:
        return observation_id in LEGACY_SEMANTIC_INVALID_OBSERVATIONS


def repository_identity(root: Optional[pathlib.Path] = None) -> dict[str, str]:
    """Stable project identity independent of absolute local path."""
    root = pathlib.Path(root or ROOT)

    def git(*args: str) -> Optional[str]:
        try:
            result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError):
            return None
        return result.stdout.strip() or None

    remote = git("config", "--get", "remote.origin.url")
    from bootstrap import sanitize_remote  # reuse existing sanitizer
    clean_remote = sanitize_remote(remote) if remote else None
    name = pathlib.Path(git("rev-parse", "--show-toplevel") or str(root)).name
    identity = f"remote:{clean_remote}" if clean_remote else f"name:{name}"
    return {"repository_id": identity, "name": name}


def persist_run(plan: dict[str, Any], report: dict[str, Any], *, machine_id: str,
                store: Optional[OrchestrationStore] = None, state_dir: Optional[pathlib.Path] = None,
                project_id: Optional[str] = None) -> str:
    """Persist one orchestration lifecycle (run/tasks/deps/attempts/events) from a
    plan + execution report. No raw prompts; bounded summaries only."""
    owns = store is None
    store = store or OrchestrationStore(state_dir=state_dir)
    identity = repository_identity()
    project_id = project_id or identity["repository_id"]
    store.ensure_project(project_id, repository_id=identity["repository_id"], name=identity["name"])
    run_id = report.get("run_id") or plan.get("run_id") or uuid.uuid4().hex[:12]
    budget = plan.get("planning_budget", {})
    store.create_run(run_id, project_id=project_id, machine_id=machine_id,
                     status="RUNNING", intent=plan.get("intent"),
                     planner_mode=(plan.get("planner_mode") or {}).get("mode"),
                     hard_max_ai_calls=budget.get("hard_max_ai_calls", report.get("max_ai_calls")),
                     planned_ai_calls=budget.get("planned_ai_calls"),
                     minimum_required_ai_calls=budget.get("minimum_required_ai_calls"),
                     tool_only_count=budget.get("tool_only_nodes", 0),
                     start_head=report.get("head"), objective_summary=plan.get("objective_summary"),
                     used_ai_calls=report.get("calls_used", 0))
    for node in plan.get("nodes", []):
        executor = node.get("executor", "AI")
        required_tier = (node.get("routing") or {}).get("tier")
        required_effort = (node.get("routing") or {}).get("effort")
        store.create_task(run_id, node["task_id"], title=node.get("title", node["task_id"]),
                          executor_type=executor, task_family=node.get("task_family"),
                          task_class=node.get("task_class"), intent=node.get("intent"),
                          required_tier=required_tier, required_effort=required_effort,
                          context_budget=(node.get("context") or {}).get("budget"),
                          conditional=bool(node.get("conditional")),
                          tool_operation=(node.get("tool") or {}).get("operation"))
    for node in plan.get("nodes", []):
        for dep in node.get("depends_on", []):
            store.add_dependency(run_id, node["task_id"], dep)
    for attempt in report.get("attempts", []):
        store.record_attempt(run_id, attempt["task_id"], attempt_no=attempt["attempt_no"],
                             backend=attempt.get("backend"), provider=attempt.get("provider"),
                             model=attempt.get("model"), native_effort=attempt.get("native_effort"),
                             normalized_effort=attempt.get("normalized_effort"), status=attempt.get("status"),
                             required_tier=attempt.get("required_tier"), required_effort=attempt.get("required_effort"),
                             failure_category=attempt.get("failure_category"),
                             is_retry=attempt.get("is_retry", False), is_fallback=attempt.get("is_fallback", False),
                             completion_signal=attempt.get("completion_signal"))
    for task_id, result in (report.get("nodes") or {}).items():
        store.finish_task(run_id, task_id, result.get("status", "FAILED"),
                          failure_category=result.get("failure_category"))
    failed = report.get("final", {}).get("failed", 0)
    run_status = "SUCCEEDED" if not failed and not report.get("error") else "FAILED"
    store.finish_run(run_id, run_status, used_ai_calls=report.get("calls_used"),
                     final_head=report.get("head"), failure_category=report.get("error"))
    if owns:
        store.close()
    return run_id
