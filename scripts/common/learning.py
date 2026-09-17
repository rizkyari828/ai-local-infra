#!/usr/bin/env python3
"""Shared teacher/student learning-loop helpers (stdlib only).

This module holds the small, reviewable vocabulary of the learning loop:
the initial failure taxonomy, the learning-candidate lifecycle, structural
validation for escalation telemetry and candidates, a minimal accidental
secret-capture guard, and lightweight metric calculation over JSONL records.
It performs no routing, training, or autonomous promotion.

Invariant for this milestone: one escalation telemetry row is the summary of
one coding task. The teacher fields describe the final teacher escalation
represented for that task. Attempt-level (multi-tier) rows are deferred.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import uuid
from typing import Any

import infra

FAILURE_CATEGORIES = (
    "syntax_or_compile",
    "test_failure",
    "missed_existing_pattern",
    "wrong_api_usage",
    "tool_loop",
    "excessive_reread",
    "context_loss",
    "invalid_structured_output",
    "environment_issue",
    "architecture_uncertainty",
    "security_uncertainty",
    "persistence_uncertainty",
    "human_rejected",
    "unknown",
)

STUDENT_OUTCOMES = ("succeeded", "failed", "uncertain", "blocked")
TEACHER_OUTCOMES = ("accepted", "rejected", "partial", "unused", "unknown")
HUMAN_REVIEW_OUTCOMES = ("unreviewed", "accepted", "rejected", "needs-review")
TEACHER_REASONS = ("failure", "uncertainty", "policy", "timeout", "other")

PROMOTION_DESTINATIONS = ("lesson", "eval", "training-candidate")
CANDIDATE_LIFECYCLE = ("observation", "reviewed", "validated", "promoted", "rejected")

# A teacher attempt is complete only when all four fields are present together.
TEACHER_ATTEMPT_FIELDS = ("teacher_provider", "teacher_model", "teacher_reason", "teacher_outcome")

# Accidental-capture guard: representative credential shapes only. This is not a
# DLP engine and must never be treated as comprehensive secret detection.
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?key|secret|token|access[_-]?token|auth[_-]?token|bearer)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-.]{8,}"
    ),
)

ESCALATION_REQUIRED = (
    "schema_version",
    "run_id",
    "event_id",
    "timestamp_utc",
    "task_class",
    "student_model",
    "student_outcome",
)

ESCALATION_FIELDS = ESCALATION_REQUIRED + (
    "parent_run_id",
    "trace_id",
    "student_role",
    "student_profile",
    "failure_category",
    "teacher_provider",
    "teacher_model",
    "teacher_reason",
    "teacher_outcome",
    "human_review_outcome",
    "correction_required",
    "human_correction_count",
    "tests_passed",
    "lesson_candidate",
    "eval_candidate",
    "training_candidate",
    "duration_ms",
    "cost_input_tokens",
    "cost_output_tokens",
    "cost_cache_hit_tokens",
    "estimated_cost_usd",
    "pricing_source",
    "recorded_by",
    "sanitized_note",
)

_NON_NEGATIVE_INT_FIELDS = (
    "human_correction_count",
    "cost_input_tokens",
    "cost_output_tokens",
    "cost_cache_hit_tokens",
)
_NON_NEGATIVE_NUMBER_FIELDS = ("duration_ms", "estimated_cost_usd")
_BOOL_FIELDS = ("lesson_candidate", "eval_candidate", "training_candidate")
_NULLABLE_BOOL_FIELDS = ("correction_required", "tests_passed")

CANDIDATE_REQUIRED = (
    "schema_version",
    "candidate_id",
    "timestamp_utc",
    "source_run_ids",
    "task_class",
    "observed_failure",
    "failure_category",
    "proposed_destination",
    "promotion_status",
)

CANDIDATE_FIELDS = CANDIDATE_REQUIRED + (
    "reviewed_root_cause",
    "validated_correction",
    "validation_evidence",
    "teacher_provider",
    "teacher_model",
    "human_accepted",
    "reviewer",
    "project_consent",
    "sanitized_note",
)

_CANDIDATE_BOOL_FIELDS = ("human_accepted", "project_consent")


def contains_secret_like(text: str) -> bool:
    """Return True when text matches a representative credential shape."""
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require(record: dict[str, Any], required: tuple[str, ...], label: str) -> None:
    missing = sorted(key for key in required if key not in record or record[key] in (None, ""))
    if missing:
        raise infra.InfraError(f"{label} missing fields: {', '.join(missing)}")


def _reject_unknown(record: dict[str, Any], allowed: tuple[str, ...], label: str) -> None:
    unknown = sorted(set(record) - set(allowed))
    if unknown:
        raise infra.InfraError(f"{label} has unknown fields: {', '.join(unknown)}")


def _check_enum(value: Any, allowed: tuple[str, ...], label: str) -> None:
    if value is not None and value not in allowed:
        raise infra.InfraError(f"{label} must be one of: {', '.join(allowed)}")


def _check_uuid(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise infra.InfraError(f"{label} must be a UUID string")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise infra.InfraError(f"{label} must be a UUID string")


def _check_timestamp(value: Any, label: str) -> None:
    if not _present(value):
        raise infra.InfraError(f"{label} must be an ISO-8601 UTC timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        raise infra.InfraError(f"{label} must be an ISO-8601 UTC timestamp")
    if parsed.tzinfo is None:
        raise infra.InfraError(f"{label} must include a timezone (for example a trailing Z)")


def _check_non_negative_number(value: Any, label: str, integer: bool) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise infra.InfraError(f"{label} must be {'an integer' if integer else 'a number'}")
    if value < 0:
        raise infra.InfraError(f"{label} must not be negative")


def _check_bool(value: Any, label: str) -> None:
    if value is not None and not isinstance(value, bool):
        raise infra.InfraError(f"{label} must be true or false")


def _reject_secrets(record: dict[str, Any], label: str) -> None:
    for key, value in record.items():
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = [item for item in value if isinstance(item, str)]
        else:
            values = []
        if any(contains_secret_like(text) for text in values):
            raise infra.InfraError(
                f"{label} field '{key}' looks like it may contain a credential; do not record secrets"
            )


def _check_teacher_fields(record: dict[str, Any], label: str) -> None:
    if not any(record.get(field) for field in TEACHER_ATTEMPT_FIELDS):
        return
    if not all(record.get(field) for field in TEACHER_ATTEMPT_FIELDS):
        raise infra.InfraError(
            f"{label} teacher escalation requires all of: {', '.join(TEACHER_ATTEMPT_FIELDS)}"
        )


def validate_failure_category(value: Any) -> Any:
    """Validate a taxonomy id; None means unclassified and is allowed."""
    if value is not None and value not in FAILURE_CATEGORIES:
        raise infra.InfraError(
            f"Unknown failure_category '{value}'; extend the taxonomy through a reviewed edit"
        )
    return value


def validate_escalation_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one teacher/student escalation telemetry record."""
    if not isinstance(record, dict):
        raise infra.InfraError("Escalation record must be an object")
    _reject_unknown(record, ESCALATION_FIELDS, "Escalation record")
    _require(record, ESCALATION_REQUIRED, "Escalation record")
    if record["schema_version"] not in ("1.0.0", "1.1.0"):
        raise infra.InfraError("Unsupported escalation schema_version")
    _reject_secrets(record, "Escalation record")
    for field in ("run_id", "event_id", "parent_run_id", "trace_id"):
        _check_uuid(record.get(field), f"Escalation record {field}")
    if record["schema_version"] == "1.1.0" and not record.get("trace_id"):
        raise infra.InfraError("Escalation schema 1.1.0 requires trace_id")
    _check_timestamp(record.get("timestamp_utc"), "Escalation record timestamp_utc")
    _check_enum(record.get("student_outcome"), STUDENT_OUTCOMES, "student_outcome")
    _check_enum(record.get("teacher_outcome"), TEACHER_OUTCOMES, "teacher_outcome")
    _check_enum(record.get("human_review_outcome"), HUMAN_REVIEW_OUTCOMES, "human_review_outcome")
    _check_enum(record.get("teacher_reason"), TEACHER_REASONS, "teacher_reason")
    validate_failure_category(record.get("failure_category"))
    if record.get("student_outcome") != "succeeded" and not record.get("failure_category"):
        raise infra.InfraError("A non-succeeded student outcome requires a failure_category")
    for field in _NON_NEGATIVE_INT_FIELDS:
        _check_non_negative_number(record.get(field), field, integer=True)
    for field in _NON_NEGATIVE_NUMBER_FIELDS:
        _check_non_negative_number(record.get(field), field, integer=False)
    for field in _BOOL_FIELDS:
        _check_bool(record.get(field), field)
    for field in _NULLABLE_BOOL_FIELDS:
        _check_bool(record.get(field), field)
    _check_teacher_fields(record, "Escalation record")
    return record


def validate_learning_candidate(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one learning candidate and its lifecycle requirements.

    Lifecycle (cumulative):
      observation: failure observation only.
      reviewed:    requires reviewed_root_cause.
      validated:   requires root cause, validated_correction, and evidence.
      promoted:    requires all validated requirements plus human acceptance
                   and a reviewer.
      rejected:    recordable without promotion evidence.

    A training-candidate destination always requires explicit project_consent.
    """
    if not isinstance(record, dict):
        raise infra.InfraError("Learning candidate must be an object")
    _reject_unknown(record, CANDIDATE_FIELDS, "Learning candidate")
    _require(record, CANDIDATE_REQUIRED, "Learning candidate")
    if record["schema_version"] != "1.0.0":
        raise infra.InfraError("Unsupported candidate schema_version")
    _reject_secrets(record, "Learning candidate")
    _check_uuid(record.get("candidate_id"), "Learning candidate candidate_id")
    _check_timestamp(record.get("timestamp_utc"), "Learning candidate timestamp_utc")
    validate_failure_category(record.get("failure_category"))
    _check_enum(record.get("proposed_destination"), PROMOTION_DESTINATIONS, "proposed_destination")
    _check_enum(record.get("promotion_status"), CANDIDATE_LIFECYCLE, "promotion_status")
    for field in _CANDIDATE_BOOL_FIELDS:
        _check_bool(record.get(field), field)
    source_runs = record.get("source_run_ids")
    if not isinstance(source_runs, list) or not source_runs:
        raise infra.InfraError("source_run_ids must be a non-empty list")
    for run_id in source_runs:
        _check_uuid(run_id, "Learning candidate source_run_ids entry")
    status = record.get("promotion_status")
    evidence = record.get("validation_evidence")
    has_evidence = isinstance(evidence, list) and any(_present(item) for item in evidence)
    if status in ("reviewed", "validated", "promoted") and not _present(record.get("reviewed_root_cause")):
        raise infra.InfraError("A reviewed candidate requires reviewed_root_cause")
    if status in ("validated", "promoted"):
        if not _present(record.get("validated_correction")):
            raise infra.InfraError("A validated candidate requires validated_correction")
        if not has_evidence:
            raise infra.InfraError("A validated candidate requires validation_evidence")
    if status == "promoted":
        if not record.get("human_accepted"):
            raise infra.InfraError("A promoted candidate requires human_accepted = true")
        if not _present(record.get("reviewer")):
            raise infra.InfraError("A promoted candidate requires a reviewer")
    if record.get("proposed_destination") == "training-candidate" and not record.get("project_consent"):
        raise infra.InfraError("A training-data candidate requires explicit project_consent")
    return record


def load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    """Read a JSONL file, ignoring blank lines, with a clear error on bad JSON."""
    if not path.exists():
        raise infra.InfraError(f"Missing telemetry file: {path}")
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise infra.InfraError(f"Invalid JSONL at {path}:{number}: {exc}") from exc
        if not isinstance(value, dict):
            raise infra.InfraError(f"Expected an object at {path}:{number}")
        records.append(value)
    return records


def _is_escalated(record: dict[str, Any]) -> bool:
    return bool(record.get("teacher_provider") or record.get("teacher_model"))


def _rate(numerator: int, denominator: int) -> Any:
    return (numerator / denominator) if denominator else None


def compute_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute escalation/acceptance metrics over escalation records.

    Unknown is never treated as success:
      - teacher acceptance requires human_review_outcome == "accepted".
      - first-pass local success requires student success, no escalation,
        correction_required is explicitly False, and human review accepted.
      - mean human corrections uses only rows with a known correction count.
    Rates are null when their denominator is empty; they are never invented.
    """
    total = len(records)
    escalated = sum(1 for record in records if _is_escalated(record))
    local_success = sum(1 for record in records if record.get("student_outcome") == "succeeded")
    teacher_accepted = sum(
        1
        for record in records
        if _is_escalated(record) and record.get("human_review_outcome") == "accepted"
    )
    first_pass = sum(
        1
        for record in records
        if record.get("student_outcome") == "succeeded"
        and not _is_escalated(record)
        and record.get("correction_required") is False
        and record.get("human_review_outcome") == "accepted"
    )
    known_corrections = [
        record["human_correction_count"]
        for record in records
        if isinstance(record.get("human_correction_count"), int)
        and not isinstance(record.get("human_correction_count"), bool)
    ]
    return {
        "total_tasks": total,
        "escalated_tasks": escalated,
        "local_success_tasks": local_success,
        "teacher_accepted_tasks": teacher_accepted,
        "first_pass_local_success_tasks": first_pass,
        "tasks_with_known_correction_count": len(known_corrections),
        "paid_escalation_rate": _rate(escalated, total),
        "local_success_rate": _rate(local_success, total),
        "teacher_acceptance_rate": _rate(teacher_accepted, escalated),
        "first_pass_local_success_rate": _rate(first_pass, total),
        "mean_human_corrections_per_task": _rate(sum(known_corrections), len(known_corrections)),
    }
