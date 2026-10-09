"""Local evidence ledger V0.2 (stdlib sqlite3, machine-local, read/write).

Bounded, structured outcome evidence for meaningful AI tasks. One compact final
row per execution, correlated by a stable decision id. Never stores raw prompts,
responses, chain-of-thought, transcripts, diffs, sessions, or credentials. Never
committed. No service, scheduler, or network.

Storage bounds: bounded text columns, normalized enums, no blobs, no JSON dumps.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import uuid
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import derived_knowledge as knowledge  # scripts/common, on sys.path

ROOT = infra.ROOT
SCHEMA_VERSION = "2"
BUSY_TIMEOUT_MS = 5000
MAX_FIELD_TEXT = 200

EVIDENCE_FIELDS = (
    "observation_id", "recorded_at", "started_at", "completed_at", "duration_bucket",
    "task_family", "task_class", "surface", "provider", "model", "native_variant",
    "normalized_effort", "initial_tier", "initial_effort", "final_tier", "final_effort",
    "outcome", "escalated", "failure_category", "source", "confidence", "completion_signal",
    "attempt_count", "user_override_tier", "user_override_effort", "user_override_model",
    "execution_id", "lesson_ids", "routing_decision_id",
)
_REQUIRED = ("observation_id", "recorded_at", "outcome", "source")
_BOOL_FIELDS = ("escalated",)
_NUM_FIELDS = ("attempt_count",)
_JSON_FIELDS = ("lesson_ids",)
_TEXT_FIELDS = tuple(field for field in EVIDENCE_FIELDS if field not in _BOOL_FIELDS + _NUM_FIELDS + _JSON_FIELDS)

PENDING_FIELDS = (
    "decision_id", "started_at", "task_family", "task_class", "surface",
    "provider", "model", "native_variant", "initial_tier", "initial_effort",
    "user_override_tier", "user_override_effort", "user_override_model",
)

COMPLETION_SIGNALS = ("task_finalized", "acceptance_passed", "tests_passed", "agent_reported")
DURATION_BUCKETS = ("lt_1m", "lt_5m", "lt_15m", "lt_1h", "gte_1h")


class EvidenceError(RuntimeError):
    pass


def state_root(state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    return pathlib.Path(state_dir) if state_dir else infra.STATE_DIR


def db_path(state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    return state_root(state_dir) / "learning" / "evidence.db"


def _assert_safe(target: pathlib.Path) -> pathlib.Path:
    resolved = target.expanduser().resolve()
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents:
        raise EvidenceError(f"refusing evidence DB inside the repository: {resolved}")
    return resolved


def connect(db: Optional[pathlib.Path] = None, *, state_dir: Optional[pathlib.Path] = None,
            initialize: bool = True) -> sqlite3.Connection:
    target = _assert_safe(db if db is not None else db_path(state_dir))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        connection = sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        raise EvidenceError(f"cannot open evidence DB {target}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if initialize:
            _initialize(connection)
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise EvidenceError(f"evidence DB unreadable or corrupt ({target}): {exc}") from exc
    return connection


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS evidence ("
        "observation_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, started_at TEXT,"
        "completed_at TEXT, duration_bucket TEXT, task_family TEXT, task_class TEXT, surface TEXT,"
        "provider TEXT, model TEXT, native_variant TEXT, normalized_effort TEXT,"
        "initial_tier TEXT, initial_effort TEXT, final_tier TEXT, final_effort TEXT,"
        "outcome TEXT NOT NULL, escalated INTEGER NOT NULL DEFAULT 0, failure_category TEXT,"
        "source TEXT NOT NULL, confidence TEXT, completion_signal TEXT, attempt_count INTEGER,"
        "user_override_tier TEXT, user_override_effort TEXT, user_override_model TEXT,"
        "execution_id TEXT, lesson_ids TEXT, routing_decision_id TEXT)")
    _ensure_columns(connection, "evidence", EVIDENCE_FIELDS)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS pending ("
        "decision_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, task_family TEXT, task_class TEXT,"
        "surface TEXT, provider TEXT, model TEXT, native_variant TEXT, initial_tier TEXT,"
        "initial_effort TEXT, user_override_tier TEXT, user_override_effort TEXT,"
        "user_override_model TEXT)")
    _ensure_columns(connection, "pending", PENDING_FIELDS)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_family ON evidence(task_family)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_model ON evidence(provider, model)")
    connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                       (SCHEMA_VERSION,))
    connection.commit()


def _ensure_columns(connection: sqlite3.Connection, table: str, fields: tuple[str, ...]) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for field in fields:
        if field not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {field} TEXT")


def _clean_text(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if knowledge.learning.contains_secret_like(text):
        raise EvidenceError(f"evidence field '{field}' looks like a credential; refusing to store")
    if "\n" in text or len(text) > MAX_FIELD_TEXT:
        raise EvidenceError(f"evidence field '{field}' must be short single-line text")
    return text


def validate_observation(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise EvidenceError("observation must be an object")
    unknown = sorted(set(record) - set(EVIDENCE_FIELDS))
    if unknown:
        raise EvidenceError(f"observation has unknown fields: {', '.join(unknown)}")
    missing = [field for field in _REQUIRED if not record.get(field)]
    if missing:
        raise EvidenceError(f"observation missing fields: {', '.join(missing)}")
    if record["outcome"] not in knowledge.EVIDENCE_OUTCOMES:
        raise EvidenceError(f"outcome must be one of: {', '.join(knowledge.EVIDENCE_OUTCOMES)}")
    if record["source"] not in knowledge.SOURCE_TYPES:
        raise EvidenceError(f"source must be one of: {', '.join(knowledge.SOURCE_TYPES)}")
    category = record.get("failure_category")
    if category and category not in knowledge.FAILURE_CATEGORIES:
        raise EvidenceError(f"unknown failure_category '{category}'")
    signal = record.get("completion_signal")
    if signal and signal not in COMPLETION_SIGNALS:
        raise EvidenceError(f"completion_signal must be one of: {', '.join(COMPLETION_SIGNALS)}")
    if record["outcome"] == "SUCCESS" and record.get("source") == "observed_runtime" and not signal:
        raise EvidenceError("SUCCESS requires a bounded completion_signal (exit code alone is not proof)")
    bucket = record.get("duration_bucket")
    if bucket and bucket not in DURATION_BUCKETS:
        raise EvidenceError(f"duration_bucket must be one of: {', '.join(DURATION_BUCKETS)}")
    for field in _TEXT_FIELDS:
        if field in record:
            _clean_text(record.get(field), field)
    if "escalated" in record and not isinstance(record["escalated"], bool):
        raise EvidenceError("escalated must be true or false")
    return record


def duration_bucket(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    if seconds < 60:
        return "lt_1m"
    if seconds < 300:
        return "lt_5m"
    if seconds < 900:
        return "lt_15m"
    if seconds < 3600:
        return "lt_1h"
    return "gte_1h"


def _row_values(record: dict[str, Any]) -> list[Any]:
    values = []
    for field in EVIDENCE_FIELDS:
        value = record.get(field)
        if field in _BOOL_FIELDS:
            value = 1 if value else 0
        elif field in _JSON_FIELDS and isinstance(value, list):
            value = json.dumps(value)
        values.append(value)
    return values


_insert_sql = None


def _insert(connection: sqlite3.Connection, record: dict[str, Any], *, ignore: bool) -> None:
    verb = "INSERT OR IGNORE" if ignore else "INSERT OR REPLACE"
    placeholders = ", ".join("?" for _ in EVIDENCE_FIELDS)
    connection.execute(f"{verb} INTO evidence ({', '.join(EVIDENCE_FIELDS)}) VALUES ({placeholders})",
                       _row_values(record))


def record(observation: dict[str, Any], db: Optional[pathlib.Path] = None, *,
           state_dir: Optional[pathlib.Path] = None) -> str:
    entry = dict(observation)
    entry.setdefault("observation_id", uuid.uuid4().hex)
    entry.setdefault("recorded_at", infra.utc_now())
    validate_observation(entry)
    connection = connect(db, state_dir=state_dir)
    try:
        _insert(connection, entry, ignore=False)
        connection.commit()
    finally:
        connection.close()
    return entry["observation_id"]


def import_seed(observations: list[dict[str, Any]], db: Optional[pathlib.Path] = None, *,
                state_dir: Optional[pathlib.Path] = None) -> dict[str, int]:
    inserted = 0
    connection = connect(db, state_dir=state_dir)
    try:
        existing = {row["observation_id"] for row in connection.execute("SELECT observation_id FROM evidence")}
        for row in observations:
            entry = {field: row[field] for field in EVIDENCE_FIELDS if field in row}
            entry.setdefault("recorded_at", infra.utc_now())
            entry.setdefault("source", "historical_verified")
            validate_observation(entry)
            if entry["observation_id"] in existing:
                continue
            _insert(connection, entry, ignore=False)
            existing.add(entry["observation_id"])
            inserted += 1
        connection.commit()
    finally:
        connection.close()
    return {"inserted": inserted, "total": len(observations)}


# --------------------------------------------------------------------------- #
# Bounded execution correlation: start -> finish (one compact final row)
# --------------------------------------------------------------------------- #


def start(*, decision_id: Optional[str] = None, started_at: Optional[str] = None,
          db: Optional[pathlib.Path] = None, state_dir: Optional[pathlib.Path] = None,
          **fields: Any) -> str:
    decision_id = decision_id or uuid.uuid4().hex
    entry = {"decision_id": decision_id, "started_at": started_at or infra.utc_now()}
    for field in PENDING_FIELDS:
        if field in fields and fields[field] is not None:
            _clean_text(fields[field], field) if isinstance(fields[field], str) else None
            entry[field] = fields[field]
    unknown = sorted(set(entry) - set(PENDING_FIELDS))
    if unknown:
        raise EvidenceError(f"start has unknown fields: {', '.join(unknown)}")
    placeholders = ", ".join("?" for _ in PENDING_FIELDS)
    connection = connect(db, state_dir=state_dir)
    try:
        connection.execute(f"INSERT OR REPLACE INTO pending ({', '.join(PENDING_FIELDS)}) "
                           f"VALUES ({placeholders})", [entry.get(f) for f in PENDING_FIELDS])
        connection.commit()
    finally:
        connection.close()
    return decision_id


def _elapsed_bucket(started_at: Optional[str], ended_at: str) -> Optional[str]:
    if not started_at:
        return None
    try:
        start = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return duration_bucket((end - start).total_seconds())


def finish(decision_id: str, *, outcome: str, db: Optional[pathlib.Path] = None,
           state_dir: Optional[pathlib.Path] = None, **fields: Any) -> str:
    """Finalize one run into a single compact evidence row. Prevents duplicate finalization."""
    connection = connect(db, state_dir=state_dir)
    try:
        pending = connection.execute("SELECT * FROM pending WHERE decision_id = ?", (decision_id,)).fetchone()
        if pending is None:
            existing = connection.execute("SELECT 1 FROM evidence WHERE observation_id = ?", (decision_id,)).fetchone()
            if existing:
                raise EvidenceError(f"decision_id {decision_id} was already finalized")
            raise EvidenceError(f"unknown decision_id {decision_id}; call 'start' first or use 'outcome'")
        now = infra.utc_now()
        entry = {field: (pending[field] if field in pending.keys() else None) for field in PENDING_FIELDS}
        entry.update({k: v for k, v in fields.items() if v is not None})
        entry.pop("decision_id", None)
        entry["observation_id"] = decision_id
        entry["routing_decision_id"] = decision_id
        entry["recorded_at"] = now
        entry["completed_at"] = fields.get("completed_at") or now
        entry["started_at"] = pending["started_at"]
        entry["duration_bucket"] = _elapsed_bucket(pending["started_at"], entry["completed_at"])
        entry["outcome"] = outcome
        entry.setdefault("source", "observed_runtime")
        if entry.get("final_tier") and entry.get("initial_tier"):
            entry["escalated"] = entry["final_tier"] != entry["initial_tier"]
        validate_observation(entry)
        _insert(connection, entry, ignore=False)
        connection.execute("DELETE FROM pending WHERE decision_id = ?", (decision_id,))
        connection.commit()
    finally:
        connection.close()
    return decision_id


def outcome(*, decision_id: Optional[str], outcome: str, db: Optional[pathlib.Path] = None,
            state_dir: Optional[pathlib.Path] = None, replace: bool = False, **fields: Any) -> str:
    """Record a final outcome directly (no prior 'start'). Refuses duplicates unless replace."""
    decision_id = decision_id or uuid.uuid4().hex
    entry = {k: v for k, v in fields.items() if v is not None}
    entry.update({"observation_id": decision_id, "routing_decision_id": decision_id,
                  "recorded_at": infra.utc_now(), "outcome": outcome,
                  "source": entry.get("source", "observed_runtime")})
    validate_observation(entry)
    connection = connect(db, state_dir=state_dir)
    try:
        existing = connection.execute("SELECT 1 FROM evidence WHERE observation_id = ?", (decision_id,)).fetchone()
        if existing and not replace:
            raise EvidenceError(f"observation {decision_id} already recorded (duplicate finalization refused)")
        _insert(connection, entry, ignore=False)
        connection.commit()
    finally:
        connection.close()
    return decision_id


def query(*, task_family: Optional[str] = None, failure_category: Optional[str] = None,
          outcome: Optional[str] = None, db: Optional[pathlib.Path] = None,
          state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    clauses, params = [], []
    if task_family:
        clauses.append("task_family = ?")
        params.append(task_family)
    if failure_category:
        clauses.append("failure_category = ?")
        params.append(failure_category)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    connection = connect(db, state_dir=state_dir)
    try:
        rows = connection.execute(f"SELECT * FROM evidence{where}", params).fetchall()
    finally:
        connection.close()
    return _summarize(rows, task_family)


def _summarize(rows: list[sqlite3.Row], task_family: Optional[str]) -> dict[str, Any]:
    categories: dict[str, int] = {}
    for row in rows:
        if row["failure_category"]:
            categories[row["failure_category"]] = categories.get(row["failure_category"], 0) + 1
    return {
        "task_family": task_family,
        "total": len(rows),
        "historical": sum(1 for row in rows if row["source"] == "historical_verified"),
        "local": sum(1 for row in rows if row["source"] != "historical_verified"),
        "success": sum(1 for row in rows if row["outcome"] == "SUCCESS"),
        "failure": sum(1 for row in rows if row["outcome"] == "FAILURE"),
        "unknown": sum(1 for row in rows if row["outcome"] == "UNKNOWN"),
        "escalated": sum(1 for row in rows if row["escalated"]),
        "failure_categories": categories,
    }


def all_rows(db: Optional[pathlib.Path] = None, state_dir: Optional[pathlib.Path] = None) -> list[sqlite3.Row]:
    target = _assert_safe(db if db is not None else db_path(state_dir))
    if not target.exists():
        return []
    connection = connect(db, state_dir=state_dir)
    try:
        return connection.execute("SELECT * FROM evidence").fetchall()
    finally:
        connection.close()


def status(db: Optional[pathlib.Path] = None, *, state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    target = _assert_safe(db if db is not None else db_path(state_dir))
    result = {"db_path": str(target), "exists": target.exists(), "schema_version": SCHEMA_VERSION,
              "db_size_bytes": target.stat().st_size if target.exists() else 0,
              "observations": 0, "historical": 0, "observed_runtime": 0,
              "oldest": None, "newest": None, "seed_observations": len(knowledge.load_historical_seed()),
              "lessons": len(knowledge.load_lessons()), "last_prune": _last_prune(db, state_dir)}
    if not target.exists():
        return result
    connection = connect(db, state_dir=state_dir)
    try:
        result["observations"] = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        result["historical"] = connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE source = 'historical_verified'").fetchone()[0]
        result["observed_runtime"] = connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE source = 'observed_runtime'").fetchone()[0]
        span = connection.execute("SELECT MIN(recorded_at), MAX(recorded_at) FROM evidence").fetchone()
        result["oldest"], result["newest"] = span[0], span[1]
        result["pending"] = connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    finally:
        connection.close()
    return result


def _last_prune(db: Optional[pathlib.Path], state_dir: Optional[pathlib.Path]) -> Optional[str]:
    target = _assert_safe(db if db is not None else db_path(state_dir))
    if not target.exists():
        return None
    connection = connect(db, state_dir=state_dir)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'last_prune'").fetchone()
        return row[0] if row else None
    finally:
        connection.close()


def reset_local(state_dir: Optional[pathlib.Path] = None, *, confirm: bool = False) -> dict[str, Any]:
    target = _assert_safe(db_path(state_dir))
    result = {"db_path": str(target), "removed": False}
    if not confirm:
        return result
    if target.exists():
        os.remove(target)
        result["removed"] = True
    return result
