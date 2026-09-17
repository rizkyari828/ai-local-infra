#!/usr/bin/env python3
"""Shared teacher/student learning-loop helpers (stdlib only).

This module holds the small, reviewable vocabulary of the learning loop:
the initial failure taxonomy, the learning-candidate lifecycle, structural
validation for escalation telemetry and candidates, and lightweight metric
calculation over JSONL records. It performs no routing, training, or
autonomous promotion.
"""

from __future__ import annotations

import json
import pathlib
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
    if record["schema_version"] != "1.0.0":
        raise infra.InfraError("Unsupported escalation schema_version")
    _check_enum(record.get("student_outcome"), STUDENT_OUTCOMES, "student_outcome")
    _check_enum(record.get("teacher_outcome"), TEACHER_OUTCOMES, "teacher_outcome")
    _check_enum(record.get("human_review_outcome"), HUMAN_REVIEW_OUTCOMES, "human_review_outcome")
    _check_enum(record.get("teacher_reason"), TEACHER_REASONS, "teacher_reason")
    validate_failure_category(record.get("failure_category"))
    if record.get("student_outcome") != "succeeded" and not record.get("failure_category"):
        raise infra.InfraError("A non-succeeded student outcome requires a failure_category")
    if (record.get("teacher_provider") or record.get("teacher_model")) and not record.get("teacher_reason"):
        raise infra.InfraError("Escalated records should record teacher_reason")
    return record


def validate_learning_candidate(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one learning candidate and its promotion preconditions."""
    if not isinstance(record, dict):
        raise infra.InfraError("Learning candidate must be an object")
    _reject_unknown(record, CANDIDATE_FIELDS, "Learning candidate")
    _require(record, CANDIDATE_REQUIRED, "Learning candidate")
    if record["schema_version"] != "1.0.0":
        raise infra.InfraError("Unsupported candidate schema_version")
    validate_failure_category(record.get("failure_category"))
    _check_enum(record.get("proposed_destination"), PROMOTION_DESTINATIONS, "proposed_destination")
    _check_enum(record.get("promotion_status"), CANDIDATE_LIFECYCLE, "promotion_status")
    if not isinstance(record.get("source_run_ids"), list) or not record["source_run_ids"]:
        raise infra.InfraError("source_run_ids must be a non-empty list")
    evidence = record.get("validation_evidence")
    if record.get("promotion_status") == "promoted":
        if not record.get("human_accepted"):
            raise infra.InfraError("A promoted candidate requires human_accepted = true")
        if not record.get("reviewer"):
            raise infra.InfraError("A promoted candidate requires a reviewer")
        if not record.get("reviewed_root_cause") or not record.get("validated_correction"):
            raise infra.InfraError("A promoted candidate requires a reviewed root cause and validated correction")
        if not isinstance(evidence, list) or not evidence:
            raise infra.InfraError("A promoted candidate requires validation_evidence")
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

    Rates are null when their denominator is empty; they are never invented.
    """
    total = len(records)
    escalated = sum(1 for record in records if _is_escalated(record))
    local_success = sum(1 for record in records if record.get("student_outcome") == "succeeded")
    teacher_accepted = sum(
        1 for record in records if _is_escalated(record) and record.get("teacher_outcome") == "accepted"
    )
    first_pass = sum(
        1 for record in records
        if record.get("student_outcome") == "succeeded"
        and not _is_escalated(record)
        and record.get("correction_required") is not True
    )
    corrections = sum(int(record.get("human_correction_count") or 0) for record in records)
    return {
        "total_tasks": total,
        "escalated_tasks": escalated,
        "local_success_tasks": local_success,
        "teacher_accepted_tasks": teacher_accepted,
        "first_pass_local_success_tasks": first_pass,
        "paid_escalation_rate": _rate(escalated, total),
        "local_success_rate": _rate(local_success, total),
        "teacher_acceptance_rate": _rate(teacher_accepted, escalated),
        "first_pass_local_success_rate": _rate(first_pass, total),
        "mean_human_corrections_per_task": _rate(corrections, total),
    }
