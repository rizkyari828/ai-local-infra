"""Local evidence ledger V0.1 (stdlib sqlite3, machine-local, read/write).

Stores bounded structured metadata about task outcomes in a machine-local SQLite
database under the state root (``$LAI_STATE_DIR/learning/evidence.db``). It is
generated runtime state: never committed, never copied between machines, and
never containing raw prompts, responses, transcripts, terminal logs, tokens, or
session databases. Concurrency-safe for a couple of local surfaces via a busy
timeout. No network, no daemon, no external service.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import uuid
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import derived_knowledge as knowledge  # scripts/common, on sys.path

ROOT = infra.ROOT
SCHEMA_VERSION = "1"
BUSY_TIMEOUT_MS = 5000
MAX_FIELD_TEXT = 200

# Bounded, normalized evidence fields (no raw prompt/response/session fields).
EVIDENCE_FIELDS = (
    "observation_id", "recorded_at", "task_family", "task_class", "surface",
    "provider", "model", "native_variant", "normalized_effort",
    "initial_tier", "initial_effort", "final_tier", "final_effort",
    "outcome", "escalated", "failure_category", "source", "confidence",
    "duration_category", "attempt_count", "lesson_ids", "routing_decision_id",
)
_REQUIRED = ("observation_id", "recorded_at", "outcome", "source")
_TEXT_FIELDS = tuple(field for field in EVIDENCE_FIELDS
                     if field not in ("escalated", "attempt_count", "lesson_ids", "recorded_at"))


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
        "observation_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, task_family TEXT,"
        "task_class TEXT, surface TEXT, provider TEXT, model TEXT, native_variant TEXT,"
        "normalized_effort TEXT, initial_tier TEXT, initial_effort TEXT, final_tier TEXT,"
        "final_effort TEXT, outcome TEXT NOT NULL, escalated INTEGER NOT NULL DEFAULT 0,"
        "failure_category TEXT, source TEXT NOT NULL, confidence TEXT, duration_category TEXT,"
        "attempt_count INTEGER, lesson_ids TEXT, routing_decision_id TEXT)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_family ON evidence(task_family)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source)")
    connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                       (SCHEMA_VERSION,))
    connection.commit()


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
    for field in _TEXT_FIELDS:
        if field in record:
            _clean_text(record.get(field), field)
    if "escalated" in record and not isinstance(record["escalated"], bool):
        raise EvidenceError("escalated must be true or false")
    return record


def _row_values(record: dict[str, Any]) -> list[Any]:
    values = []
    for field in EVIDENCE_FIELDS:
        value = record.get(field)
        if field == "escalated":
            value = 1 if value else 0
        elif field == "lesson_ids":
            value = json.dumps(value) if isinstance(value, list) else value
        values.append(value)
    return values


def record(observation: dict[str, Any], db: Optional[pathlib.Path] = None, *,
           state_dir: Optional[pathlib.Path] = None) -> str:
    entry = dict(observation)
    entry.setdefault("observation_id", uuid.uuid4().hex)
    entry.setdefault("recorded_at", infra.utc_now())
    validate_observation(entry)
    placeholders = ", ".join("?" for _ in EVIDENCE_FIELDS)
    connection = connect(db, state_dir=state_dir)
    try:
        connection.execute(f"INSERT OR REPLACE INTO evidence ({', '.join(EVIDENCE_FIELDS)}) "
                           f"VALUES ({placeholders})", _row_values(entry))
        connection.commit()
    finally:
        connection.close()
    return entry["observation_id"]


def import_seed(observations: list[dict[str, Any]], db: Optional[pathlib.Path] = None, *,
                state_dir: Optional[pathlib.Path] = None) -> dict[str, int]:
    """Idempotent seed import keyed by stable observation_id. Safe to run repeatedly."""
    inserted = 0
    connection = connect(db, state_dir=state_dir)
    try:
        existing = {row["observation_id"] for row in connection.execute("SELECT observation_id FROM evidence")}
        placeholders = ", ".join("?" for _ in EVIDENCE_FIELDS)
        for row in observations:
            # Seed files may carry human "notes"; only evidence schema fields import.
            entry = {field: row[field] for field in EVIDENCE_FIELDS if field in row}
            entry.setdefault("recorded_at", infra.utc_now())
            entry.setdefault("source", "historical_verified")
            validate_observation(entry)
            if entry["observation_id"] in existing:
                continue
            connection.execute(f"INSERT INTO evidence ({', '.join(EVIDENCE_FIELDS)}) "
                               f"VALUES ({placeholders})", _row_values(entry))
            existing.add(entry["observation_id"])
            inserted += 1
        connection.commit()
    finally:
        connection.close()
    return {"inserted": inserted, "total": len(observations)}


def status(db: Optional[pathlib.Path] = None, *, state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    target = _assert_safe(db if db is not None else db_path(state_dir))
    result = {"db_path": str(target), "exists": target.exists(), "schema_version": SCHEMA_VERSION,
              "observations": 0, "historical": 0, "local": 0,
              "seed_observations": len(knowledge.load_historical_seed()),
              "lessons": len(knowledge.load_lessons()),
              "promotion_candidates": 0}
    if not target.exists():
        return result
    connection = connect(db, state_dir=state_dir)
    try:
        result["observations"] = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        result["historical"] = connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE source = 'historical_verified'").fetchone()[0]
        result["local"] = result["observations"] - result["historical"]
    finally:
        connection.close()
    result["promotion_candidates"] = len(candidates(db=db, state_dir=state_dir)["candidates"])
    return result


def query(*, task_family: Optional[str] = None, failure_category: Optional[str] = None,
          db: Optional[pathlib.Path] = None, state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    clauses, params = [], []
    if task_family:
        clauses.append("task_family = ?")
        params.append(task_family)
    if failure_category:
        clauses.append("failure_category = ?")
        params.append(failure_category)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    connection = connect(db, state_dir=state_dir)
    try:
        rows = connection.execute(f"SELECT * FROM evidence{where}", params).fetchall()
    finally:
        connection.close()
    historical = [row for row in rows if row["source"] == "historical_verified"]
    local = [row for row in rows if row["source"] != "historical_verified"]
    categories: dict[str, int] = {}
    for row in rows:
        if row["failure_category"]:
            categories[row["failure_category"]] = categories.get(row["failure_category"], 0) + 1
    return {
        "task_family": task_family,
        "total": len(rows),
        "historical": len(historical),
        "local": len(local),
        "success": sum(1 for row in rows if row["outcome"] == "SUCCESS"),
        "failure": sum(1 for row in rows if row["outcome"] == "FAILURE"),
        "escalated": sum(1 for row in rows if row["escalated"]),
        "failure_categories": categories,
    }


def candidates(*, db: Optional[pathlib.Path] = None,
               state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Local, non-mutating suggestions. Never promotes anything automatically."""
    policy = knowledge.load_learned_defaults().get("policy", {})
    min_samples = int(policy.get("min_samples", 3))
    min_ratio = float(policy.get("min_success_ratio", 0.8))
    rows = _all_rows(db, state_dir)
    suggestions: list[dict[str, Any]] = []
    groups: dict[tuple, dict[str, int]] = {}
    for row in rows:
        if row["outcome"] == "UNKNOWN":
            continue
        key = (row["task_family"], row["final_tier"], row["final_effort"])
        bucket = groups.setdefault(key, {"success": 0, "failure": 0})
        bucket["success" if row["outcome"] == "SUCCESS" else "failure"] += 1
    for (family, tier, effort), counts in sorted(groups.items(), key=lambda item: str(item[0])):
        total = counts["success"] + counts["failure"]
        ratio = counts["success"] / total if total else 0.0
        if family and total >= min_samples and ratio >= min_ratio:
            suggestions.append({"kind": "repeated_success", "task_family": family,
                                "tier": tier, "effort": effort, "samples": total,
                                "success_ratio": round(ratio, 3)})
    weak = knowledge.classify_failure
    recurring: dict[str, int] = {}
    for row in rows:
        if row["failure_category"] and not weak(row["failure_category"])["intelligence_escalation"]:
            recurring[row["failure_category"]] = recurring.get(row["failure_category"], 0) + 1
    for category, count in sorted(recurring.items()):
        if count >= min_samples:
            suggestions.append({"kind": "recurring_non_intelligence_failure",
                                "failure_category": category, "samples": count})
    return {"candidates": suggestions, "policy": {"min_samples": min_samples, "min_success_ratio": min_ratio}}


def _all_rows(db: Optional[pathlib.Path], state_dir: Optional[pathlib.Path]) -> list[sqlite3.Row]:
    target = _assert_safe(db if db is not None else db_path(state_dir))
    if not target.exists():
        return []
    connection = connect(db, state_dir=state_dir)
    try:
        return connection.execute("SELECT * FROM evidence").fetchall()
    finally:
        connection.close()


def reset_local(state_dir: Optional[pathlib.Path] = None, *, confirm: bool = False) -> dict[str, Any]:
    """Delete ONLY the local DB (shared knowledge is untouched). Requires confirm."""
    target = _assert_safe(db_path(state_dir))
    result = {"db_path": str(target), "removed": False}
    if not confirm:
        return result
    if target.exists():
        os.remove(target)
        result["removed"] = True
    return result
