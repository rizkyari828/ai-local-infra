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
import authority  # scripts/common, on sys.path
import learning  # scripts/common, on sys.path (secret guard)

ROOT = infra.ROOT
DB_SUBDIR = "orchestration"
DB_FILENAME = "orchestration.db"
SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 5000
MAX_PAYLOAD = 2000  # bounded structured event payload

RUN_STATUSES = ("PLANNED", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED")
# How a run started. The local engine never guesses a controller's cost: a
# CHAT_HANDOFF controller may have already spent AI calls the engine cannot see.
ENTRY_MODES = ("CHAT_HANDOFF", "DIRECT_CLI", "AUTOMATION", "API_OR_UI_FUTURE")
TASK_STATUSES = ("PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED", "NO_CHANGE_REQUIRED",
                 "AI_REQUIRED", "BLOCKED", "CANCELLED", "NOT_RUN", "UNVERIFIED")
# One canonical terminal vocabulary. Every engine outcome maps here explicitly; an
# unrecognised terminal status is recorded as FAILED, never silently dropped.
TASK_TERMINAL_MAP = {
    "SUCCESS": "SUCCESS", "NO_CHANGE_REQUIRED": "NO_CHANGE_REQUIRED", "AI_REQUIRED": "AI_REQUIRED",
    "SKIPPED": "SKIPPED", "BLOCKED": "BLOCKED", "NOT_RUN": "NOT_RUN", "CANCELLED": "CANCELLED",
    "UNVERIFIED": "UNVERIFIED", "FAILURE": "FAILED", "FAILED": "FAILED",
}


def to_operational_task_status(status: Optional[str]) -> str:
    """Map any engine outcome into the one canonical operational task vocabulary."""
    if status in TASK_TERMINAL_MAP:
        return TASK_TERMINAL_MAP[status]
    if status in TASK_STATUSES:
        return status
    return "FAILED"


def is_task_success(status: Optional[str]) -> bool:
    return to_operational_task_status(status) in ("SUCCESS", "NO_CHANGE_REQUIRED", "SKIPPED")

EXECUTOR_TYPES = ("TOOL_ONLY", "AI")
EVENT_TYPES = (
    "RUN_CREATED", "RUN_STARTED", "RUN_COMPLETED", "RUN_FAILED",
    "TASK_CREATED", "TASK_STARTED", "TASK_COMPLETED", "TASK_FAILED", "TASK_NO_CHANGE_REQUIRED",
    "TASK_AI_REQUIRED", "ATTEMPT_STARTED", "ATTEMPT_SUCCEEDED", "ATTEMPT_FAILED",
    "FAILOVER_SELECTED", "RETRY_SELECTED", "CALL_BUDGET_UPDATED", "READ_ONLY_VIOLATION",
    "CALL_BUDGET_EXHAUSTED",
    # Result Cache + incremental DAG reuse (Layer 3).
    "CACHE_HIT", "CACHE_MISS", "CACHE_REJECTED", "TASK_REUSED", "CACHE_ENTRY_WRITTEN",
    "ACTIVE_WORK_FOUND", "WAIT_STARTED", "WAIT_COMPLETED", "WAIT_REVALIDATION_FAILED",
    "RESULT_ADOPTED", "WAIT_TIMEOUT", "CLAIM_ACQUIRED", "CLAIM_RECOVERED", "CLAIM_UNCERTAIN",
)
# Superset failure taxonomy (non-intelligence categories never imply model weakness).
FAILURE_TAXONOMY = (
    "RATE_LIMIT", "PROVIDER_503", "TIMEOUT", "ACCOUNT_MODEL_UNSUPPORTED", "MODEL_UNAVAILABLE",
    "BACKEND_UNAVAILABLE", "PROVIDER_UNAVAILABLE", "REASONING_INSUFFICIENT", "PERMISSION_REQUIRED",
    "READ_ONLY_VIOLATION", "CALL_BUDGET_EXHAUSTED", "TOOL_EXECUTION_FAILED",
    "DETERMINISTIC_CHECK_FAILED", "ACCEPTANCE_FAILED", "ACCEPTANCE_UNVERIFIED",
    "INSUFFICIENT_AI_CALL_BUDGET", "NO_CHANGE_REQUIRED",
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
    # v2: additive Result Cache reuse provenance (no result bodies; cache stays the
    # cache authority). Provenance/state only, never duplicated accepted results.
    (2, [
        "ALTER TABLE tasks ADD COLUMN execution_source TEXT",
        "ALTER TABLE tasks ADD COLUMN cache_entry_id TEXT",
        "ALTER TABLE tasks ADD COLUMN reused_from_run_id TEXT",
        "ALTER TABLE tasks ADD COLUMN reused_from_task_id TEXT",
        "ALTER TABLE tasks ADD COLUMN input_fingerprint TEXT",
        "ALTER TABLE tasks ADD COLUMN result_fingerprint TEXT",
    ]),
    # v3: entry mode / controller origin. Genuinely not derivable from existing
    # data, and never used to invent a controller AI cost. Bounded metadata only.
    (3, [
        "ALTER TABLE runs ADD COLUMN entry_mode TEXT",
        "ALTER TABLE runs ADD COLUMN controller_ai_calls INTEGER",
    ]),
    (4, [
        "ALTER TABLE attempts ADD COLUMN requested_model TEXT",
        "ALTER TABLE attempts ADD COLUMN requested_effort TEXT",
        "ALTER TABLE attempts ADD COLUMN resolved_model TEXT",
        "ALTER TABLE attempts ADD COLUMN resolved_effort TEXT",
        "ALTER TABLE attempts ADD COLUMN actual_model TEXT",
        "ALTER TABLE attempts ADD COLUMN actual_effort TEXT",
        """CREATE TABLE active_work (
            work_key TEXT PRIMARY KEY, version TEXT NOT NULL, workspace TEXT NOT NULL,
            mode TEXT NOT NULL, owner_run TEXT NOT NULL, owner_task TEXT NOT NULL,
            owner_pid INTEGER NOT NULL, generation INTEGER NOT NULL, token TEXT NOT NULL,
            state TEXT NOT NULL, launch_intent INTEGER NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL, result_id TEXT)""",
        "CREATE INDEX active_workspace ON active_work(workspace, state)",
        """CREATE TABLE accepted_work (
            result_id TEXT PRIMARY KEY, work_key TEXT NOT NULL, version TEXT NOT NULL,
            generation INTEGER NOT NULL, token TEXT NOT NULL, result TEXT NOT NULL,
            identity TEXT NOT NULL, owner_run TEXT NOT NULL, owner_task TEXT NOT NULL,
            postcondition_verified INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX accepted_work_key ON accepted_work(work_key)",
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
        for version, statements in _MIGRATIONS:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                if version > self._version():
                    for statement in statements:
                        self.connection.execute(statement)
                    self.connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                                            (str(version),))
                self.connection.commit()
            except sqlite3.DatabaseError as exc:
                self.connection.rollback()
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
                   start_head: Optional[str] = None, objective_summary: Optional[str] = None,
                   entry_mode: Optional[str] = None, controller_ai_calls: Optional[int] = None) -> str:
        if status not in RUN_STATUSES:
            raise StoreError(f"unknown run status: {status}")
        if entry_mode is not None and entry_mode not in ENTRY_MODES:
            raise StoreError(f"unknown entry mode: {entry_mode}")
        now = _now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs(run_id, project_id, machine_id, status, intent, planner_mode, planner_tier,"
                " planner_native_effort, planner_normalized_effort, hard_max_ai_calls, planned_ai_calls,"
                " used_ai_calls, minimum_required_ai_calls, tool_only_count, start_head, objective_summary,"
                " entry_mode, controller_ai_calls, created_at, started_at, schema_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_clean(run_id, "run_id"), _clean(project_id, "project_id"), _clean(machine_id, "machine_id"),
                 status, _clean(intent, "intent"), _clean(planner_mode, "planner_mode"),
                 _clean(planner_tier, "planner_tier"), _clean(planner_native_effort, "planner_native_effort"),
                 _clean(planner_normalized_effort, "planner_normalized_effort"), hard_max_ai_calls,
                 planned_ai_calls, used_ai_calls, minimum_required_ai_calls, tool_only_count,
                 _clean(start_head, "start_head", max_len=64),
                 _clean(objective_summary, "objective_summary", max_len=200),
                 _clean(entry_mode, "entry_mode"), controller_ai_calls, now, now, SCHEMA_VERSION))
            self._event(run_id, "RUN_CREATED",
                        payload={"status": status, "intent": intent, "entry_mode": entry_mode})
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

    def finish_task(self, run_id: str, task_id: str, status: str, *, failure_category: Optional[str] = None,
                    execution_source: Optional[str] = None, cache_entry_id: Optional[str] = None,
                    reused_from_run_id: Optional[str] = None, reused_from_task_id: Optional[str] = None,
                    input_fingerprint: Optional[str] = None,
                    result_fingerprint: Optional[str] = None) -> None:
        if status not in TASK_STATUSES:
            raise StoreError(f"unknown task status: {status}")
        event = {"SUCCESS": "TASK_COMPLETED", "NO_CHANGE_REQUIRED": "TASK_NO_CHANGE_REQUIRED",
                 "AI_REQUIRED": "TASK_AI_REQUIRED"}.get(status, "TASK_FAILED")
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET status=?, completed_at=?, failure_category=?, execution_source=?,"
                " cache_entry_id=?, reused_from_run_id=?, reused_from_task_id=?, input_fingerprint=?,"
                " result_fingerprint=? WHERE run_id=? AND task_id=?",
                (status, _now(), _clean(failure_category, "failure_category"),
                 _clean(execution_source, "execution_source"), _clean(cache_entry_id, "cache_entry_id"),
                 _clean(reused_from_run_id, "reused_from_run_id"), _clean(reused_from_task_id, "reused_from_task_id"),
                 _clean(input_fingerprint, "input_fingerprint", max_len=64),
                 _clean(result_fingerprint, "result_fingerprint", max_len=64), run_id, task_id))
            self._event(run_id, event, task_id=task_id, payload={"status": status, "failure_category": failure_category})
            if execution_source == "CACHE":
                self._event(run_id, "TASK_REUSED", task_id=task_id,
                            payload={"cache_entry_id": cache_entry_id, "reused_from_run_id": reused_from_run_id,
                                     "reused_from_task_id": reused_from_task_id})

    def cache_event(self, run_id: str, event_type: str, *, task_id: Optional[str] = None,
                    payload: Optional[dict[str, Any]] = None) -> None:
        """Bounded cache operational timeline event (CACHE_HIT/MISS/REJECTED/WRITTEN)."""
        if event_type not in EVENT_TYPES:
            raise StoreError(f"unknown event type: {event_type}")
        with self.connection:
            self._event(run_id, event_type, task_id=task_id, payload=payload)

    def record_attempt(self, run_id: str, task_id: str, *, attempt_no: int, backend: Optional[str],
                       provider: Optional[str], model: Optional[str], native_effort: Optional[str],
                       normalized_effort: Optional[str], status: str, required_tier: Optional[str] = None,
                       required_effort: Optional[str] = None, failure_category: Optional[str] = None,
                       is_retry: bool = False, is_fallback: bool = False,
                       completion_signal: Optional[str] = None, started_at: Optional[str] = None,
                       requested_model: Optional[str] = None, requested_effort: Optional[str] = None,
                       requested_provider: Optional[str] = None, role: Optional[str] = None) -> str:
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
                 started_at or now, None if status == "LAUNCHED" else now, SCHEMA_VERSION))
            self.connection.execute('UPDATE attempts SET requested_model=?, requested_effort=?, resolved_model=?, resolved_effort=? WHERE attempt_id=?',
                (_clean(requested_model, 'requested_model'), _clean(requested_effort, 'requested_effort'),
                 _clean(model, 'resolved_model'), _clean(native_effort, 'resolved_effort'), attempt_id))
            if status == "LAUNCHED":
                self.connection.execute("UPDATE runs SET used_ai_calls=COALESCE(used_ai_calls,0)+1 WHERE run_id=?",
                                        (run_id,))
            self._event(run_id, "ATTEMPT_STARTED", task_id=task_id, attempt_id=attempt_id,
                        payload={"attempt_no": attempt_no, "backend": backend, "model": model,
                            'provider': provider, 'role': role, 'requested_provider': requested_provider,
                            'requested_model': requested_model, 'requested_effort': requested_effort,
                            'resolved_effort': native_effort})
            if status in ("SUCCESS",):
                self._event(run_id, "ATTEMPT_SUCCEEDED", task_id=task_id, attempt_id=attempt_id,
                            payload={"normalized_effort": normalized_effort})
            elif status != "LAUNCHED":
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
            " used_ai_calls, minimum_required_ai_calls, tool_only_count, failure_category, entry_mode,"
            " created_at FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
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

    def run_attempts(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM attempts WHERE run_id=? ORDER BY task_id, attempt_no", (run_id,))
        return [dict(row) for row in rows]

    def model_usage(self, run_id: Optional[str] = None, *, machine_id: Optional[str] = None,
                    project_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Grouped real-invocation attempts by backend/provider/model/tier/effort and
        status. No model name is hardcoded; callers get facts, not labels."""
        clauses, params = [], []
        if run_id is not None:
            clauses.append("a.run_id = ?")
            params.append(run_id)
        if machine_id is not None:
            clauses.append("r.machine_id = ?")
            params.append(machine_id)
        if project_id is not None:
            clauses.append("r.project_id = ?")
            params.append(project_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT a.backend, a.surface, a.provider, a.model, a.required_tier, a.required_effort,"
            " a.native_effort, a.normalized_effort, a.status, a.failure_category, COUNT(*) AS n"
            f" FROM attempts a JOIN runs r ON r.run_id = a.run_id{where}"
            " GROUP BY a.backend, a.surface, a.provider, a.model, a.required_tier, a.required_effort,"
            " a.native_effort, a.normalized_effort, a.status, a.failure_category"
            " ORDER BY a.provider, a.model, a.required_tier", params)
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

    def reuse_summary(self, run_id: str) -> dict[str, Any]:
        """Reuse provenance from a run without parsing CLI prose: which tasks executed,
        were TOOL_ONLY, were AI, and which reused a cache entry and from where."""
        rows = self.connection.execute(
            "SELECT task_id, executor_type, status, execution_source, reused_from_run_id,"
            " reused_from_task_id, input_fingerprint, result_fingerprint FROM tasks"
            " WHERE run_id=? ORDER BY task_id", (run_id,)).fetchall()
        reused = [r["task_id"] for r in rows if r["execution_source"] == "CACHE"]
        return {"run_id": run_id,
                "tasks": [dict(r) for r in rows],
                "executed": [r["task_id"] for r in rows if r["execution_source"] == "EXECUTED"],
                "tool_only": [r["task_id"] for r in rows if r["executor_type"] == "TOOL_ONLY"],
                "ai": [r["task_id"] for r in rows if r["executor_type"] == "AI"],
                "reused": reused,
                "reused_from": {r["task_id"]: r["reused_from_run_id"] for r in rows
                                if r["execution_source"] == "CACHE"}}

    def legacy_observation_excluded(self, observation_id: str) -> bool:
        return observation_id in LEGACY_SEMANTIC_INVALID_OBSERVATIONS


def repository_identity(root: Optional[pathlib.Path] = None) -> dict[str, str]:
    """Stable project identity independent of absolute local path.

    Delegates to the ONE canonical identity contract (scripts/common/authority.py) so
    cache/reuse/resume identity cannot drift from workspace authority: a sanitized
    remote URL when present, else a hash of the canonical common git directory. A bare
    directory name is never identity.
    """
    identity = authority.repository_identity(pathlib.Path(root or ROOT))
    return {"repository_id": identity["id"], "name": identity["name"]}


def persist_run(plan: dict[str, Any], report: dict[str, Any], *, machine_id: str,
                store: Optional[OrchestrationStore] = None, state_dir: Optional[pathlib.Path] = None,
                project_id: Optional[str] = None, entry_mode: Optional[str] = None,
                controller_ai_calls: Optional[int] = None) -> str:
    """Persist one orchestration lifecycle (run/tasks/deps/attempts/events) from a
    plan + execution report. No raw prompts; bounded summaries only.

    Idempotent: re-persisting an already-recorded run is a no-op (no duplicate run,
    attempts, or events). ``entry_mode`` defaults to ``DIRECT_CLI`` for a local
    programmatic invocation. A CHAT_HANDOFF controller cost is never invented: the
    engine records 0 worker calls for a direct path, and ``None`` (unknown) for a
    chat handoff unless a trusted integration explicitly supplies a value.
    """
    owns = store is None
    store = store or OrchestrationStore(state_dir=state_dir)
    identity = repository_identity()
    project_id = project_id or identity["repository_id"]
    run_id = report.get("run_id") or plan.get("run_id") or uuid.uuid4().hex[:12]
    if store.get_run(run_id) is not None:
        if owns:
            store.close()
        return run_id  # already recorded; never duplicate run/task/attempt/event rows
    entry_mode = entry_mode or report.get("entry_mode") or "DIRECT_CLI"
    if entry_mode not in ENTRY_MODES:
        raise StoreError(f"unknown entry mode: {entry_mode}")
    if controller_ai_calls is None and entry_mode != "CHAT_HANDOFF":
        controller_ai_calls = 0  # a local/automation path launches no controller model
    store.ensure_project(project_id, repository_id=identity["repository_id"], name=identity["name"])
    budget = plan.get("planning_budget", {})
    store.create_run(run_id, project_id=project_id, machine_id=machine_id,
                     status="RUNNING", intent=plan.get("intent"),
                     planner_mode=(plan.get("planner_mode") or {}).get("mode"),
                     hard_max_ai_calls=budget.get("hard_max_ai_calls", report.get("max_ai_calls")),
                     planned_ai_calls=budget.get("planned_ai_calls"),
                     minimum_required_ai_calls=budget.get("minimum_required_ai_calls"),
                     tool_only_count=budget.get("tool_only_nodes", 0),
                     start_head=report.get("head"), objective_summary=plan.get("objective_summary"),
                     used_ai_calls=report.get("calls_used", 0),
                     entry_mode=entry_mode, controller_ai_calls=controller_ai_calls)
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
        store.finish_task(run_id, task_id, to_operational_task_status(result.get("status", "FAILED")),
                          failure_category=result.get("failure_category"),
                          execution_source=result.get("execution_source"),
                          cache_entry_id=result.get("cache_entry_id"),
                          reused_from_run_id=result.get("reused_from_run_id"),
                          reused_from_task_id=result.get("reused_from_task_id"),
                          input_fingerprint=result.get("input_fingerprint"),
                          result_fingerprint=result.get("result_fingerprint"))
    cache = report.get("cache") or {}
    if cache.get("decisions"):
        for decision in cache["decisions"]:
            task_id = decision.get("task_id")
            kind = decision.get("decision")
            if kind == "CACHE_WRITTEN":
                store.cache_event(run_id, "CACHE_ENTRY_WRITTEN", task_id=task_id,
                                  payload={"cache_entry_id": decision.get("cache_entry_id")})
            elif kind == "WRITE_REJECTED":
                store.cache_event(run_id, "CACHE_REJECTED", task_id=task_id,
                                  payload={"reason": decision.get("reason")})
            elif kind == "REUSE":
                store.cache_event(run_id, "CACHE_HIT", task_id=task_id,
                                  payload={"cache_entry_id": decision.get("cache_entry_id"),
                                           "reason": decision.get("reason")})
            else:  # RERUN
                store.cache_event(run_id, "CACHE_MISS", task_id=task_id,
                                  payload={"reason": decision.get("reason")})
    failed = report.get("final", {}).get("failed", 0)
    statuses = [result.get("status") for result in (report.get("nodes") or {}).values()]
    unresolved = [s for s in statuses if not is_task_success(s)]
    run_status = "SUCCEEDED" if not unresolved and not failed and not report.get("error") else "FAILED"
    store.finish_run(run_id, run_status, used_ai_calls=report.get("calls_used"),
                     final_head=report.get("head"), failure_category=report.get("error"))
    if owns:
        store.close()
    return run_id
