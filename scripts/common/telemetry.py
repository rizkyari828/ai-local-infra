#!/usr/bin/env python3
"""Runtime observability helpers (stdlib only).

This module provides the small, local-first pieces needed to diagnose runtime
bottlenecks from telemetry instead of guesswork:

- a central, conservative limits policy (``config/limits.yaml``);
- the runtime error vocabulary used to classify provider/tool/environment
  failures without conflating them with model reasoning quality;
- structural validation for persisted run and span events (ingress + writers);
- privacy-preserving target hashing for repeated-operation detection;
- lightweight per-trace aggregation over span JSONL.

It performs no routing, retries, or model invocation. Spans are *completed*
spans: each carries a duration and a completion timestamp, which is sufficient
for per-stage aggregation without a tracing backend.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import pathlib
import re
import uuid
from typing import Any, Optional

import infra
import learning
import pricing

SPAN_SCHEMA_VERSION = "1.0.0"

SPAN_STAGES = ("inference", "tool", "validation", "review", "escalation", "retry", "queue", "other")
TOOL_KINDS = ("read", "write", "search", "shell", "test", "git", "other")
TOOL_OUTCOMES = ("succeeded", "failed", "timeout", "cancelled")

# Runtime failure classes. ``model_reasoning_failure`` is the only category that
# should ever be interpreted as "the model reasoned poorly"; the rest are
# environment/provider/tool/validation problems and must not become negative
# learning signal about the student model.
RUNTIME_ERROR_CATEGORIES = (
    "model_reasoning_failure",
    "provider_failure",
    "provider_timeout",
    "tool_failure",
    "tool_timeout",
    "validation_failure",
    "environment_failure",
    "configuration_failure",
    "resource_limit",
    "context_limit",
    "user_cancelled",
    "unknown",
)

SPAN_REQUIRED = ("schema_version", "trace_id", "span_id", "timestamp_utc", "stage", "duration_ms")
SPAN_FIELDS = SPAN_REQUIRED + (
    "parent_span_id",
    "run_id",
    "model",
    "provider",
    "role",
    "attempt",
    "retry_count",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_miss_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cache_hit_ratio",
    "estimated_input_cost",
    "estimated_cached_input_cost",
    "estimated_output_cost",
    "estimated_total_cost",
    "pricing_profile",
    "pricing_currency",
    "prompt_prefix_hash",
    "context_size",
    "context_utilization",
    "tool_name",
    "tool_kind",
    "tool_outcome",
    "target_hash",
    "repeated",
    "bytes_read",
    "bytes_written",
    "exit_code",
    "result_count",
    "error_category",
    "error_code",
    "limits_version",
    "sanitized_note",
)

AI_RUN_REQUIRED = ("schema_version", "run_id", "event_id", "timestamp_utc", "event_type", "status")
AI_RUN_FIELDS = AI_RUN_REQUIRED + (
    "project",
    "agent_client",
    "model",
    "model_digest",
    "profile",
    "operation_tool",
    "task_type",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "context_configured",
    "context_used",
    "prompt_tokens_per_second",
    "generation_tokens_per_second",
    "tool_call_count",
    "error_category",
    "error_code",
    "error_fingerprint",
    "retry_count",
    "eval_suite",
    "eval_version",
    "eval_score",
    "structured_output_compliant",
    "test_status",
    "review_status",
    "profile_version",
    "ollama_version",
    "client_version",
    "hardware_profile",
    "config_version",
)

AI_RUN_STATUSES = ("started", "succeeded", "failed", "cancelled", "unknown")
REVIEW_STATUSES = ("unreviewed", "accepted", "rejected", "needs-review")

# One compact outcome record per completed task. It aggregates the existing
# runtime results and escalation outcome for a trace; it is metadata only and
# never stores prompts, responses, source, reasoning, or credentials.
TASK_OUTCOME_SCHEMA_VERSION = "1.0.0"
TASK_OUTCOME_STATUSES = ("succeeded", "failed", "cancelled", "unknown")
TASK_OUTCOME_REQUIRED = ("schema_version", "task_id", "event_id", "completed_at")
TASK_OUTCOME_FIELDS = TASK_OUTCOME_REQUIRED + (
    "started_at",
    "run_id",
    "task_type",
    "status",
    "success",
    "tests_passed",
    "escalated",
    "escalation_count",
    "attempt_count",
    "initial_provider",
    "initial_model",
    "initial_variant",
    "final_provider",
    "final_model",
    "final_variant",
    "total_latency_ms",
    "provider_reported_cost",
    "estimated_total_cost",
    "pricing_currency",
    "error_category",
    "error_code",
)
_TASK_OUTCOME_BOOL_FIELDS = ("success", "tests_passed", "escalated")
_TASK_OUTCOME_NON_NEGATIVE_INT_FIELDS = ("escalation_count", "attempt_count")
_TASK_OUTCOME_NON_NEGATIVE_NUMBER_FIELDS = (
    "total_latency_ms",
    "provider_reported_cost",
    "estimated_total_cost",
)

# OpenCode `stats --json` aggregate rows, normalized into the same usage
# vocabulary as runtime spans. OpenCode's ``tokens.input`` is the *uncached*
# portion, so it maps to ``cache_miss_tokens``; ``input_tokens`` is the provider
# total (uncached + cache.read). ``provider_reported_cost`` keeps OpenCode's own
# cost separate from any locally estimated cost.
OPENCODE_STATS_SCHEMA_VERSION = "1.0.0"
OPENCODE_STATS_REQUIRED = ("schema_version", "snapshot_id", "captured_at", "provider", "model")
OPENCODE_STATS_FIELDS = OPENCODE_STATS_REQUIRED + (
    "variant",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "cache_miss_tokens",
    "total_tokens",
    "cache_hit_ratio",
    "provider_reported_cost",
    "estimated_total_cost",
    "pricing_profile",
    "pricing_currency",
    "sessions",
    "subagents",
    "prompts",
    "steps",
    "agent_client",
    "opencode_version",
    "source",
)
_OPENCODE_STATS_NON_NEGATIVE_INT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "cache_miss_tokens",
    "total_tokens",
    "sessions",
    "subagents",
    "prompts",
    "steps",
)
_OPENCODE_STATS_NON_NEGATIVE_NUMBER_FIELDS = ("provider_reported_cost", "estimated_total_cost")

_SPAN_NON_NEGATIVE_INT_FIELDS = (
    "attempt",
    "retry_count",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_miss_tokens",
    "reasoning_tokens",
    "total_tokens",
    "bytes_read",
    "bytes_written",
    "result_count",
)
_SPAN_NON_NEGATIVE_NUMBER_FIELDS = (
    "duration_ms",
    "context_size",
    "estimated_input_cost",
    "estimated_cached_input_cost",
    "estimated_output_cost",
    "estimated_total_cost",
)
_AI_RUN_NON_NEGATIVE_INT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "context_configured",
    "context_used",
    "tool_call_count",
    "retry_count",
)
_AI_RUN_NON_NEGATIVE_NUMBER_FIELDS = ("duration_ms", "prompt_tokens_per_second", "generation_tokens_per_second")

TARGET_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

TOOL_LIMIT_KEYS = (
    "tool_timeout_seconds",
    "max_tool_calls",
    "max_file_read_bytes",
    "max_file_write_bytes",
    "max_shell_output_bytes",
    "max_search_results",
)

DEFAULT_LIMITS: dict[str, Any] = {
    "schema_version": 1,
    "limits_version": "0.1.0",
    "timeouts": {"model_timeout_seconds": 900, "max_runtime_seconds": 1800},
    "anomaly_thresholds": {
        "max_repeated_reads": 3,
        "max_retries": 2,
        "max_attempts": 5,
        "context_utilization_warn": 0.85,
    },
    "tools": {
        "tool_timeout_seconds": 120,
        "max_tool_calls": 50,
        "max_file_read_bytes": 262144,
        "max_file_write_bytes": 1048576,
        "max_shell_output_bytes": 65536,
        "max_search_results": 200,
    },
}

DEFAULT_TOOL_LIMITS: dict[str, Any] = dict(DEFAULT_LIMITS["tools"])


def load_tool_limits(limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return tool limits merged over conservative defaults."""
    policy = limits if limits is not None else load_limits()
    merged = dict(DEFAULT_TOOL_LIMITS)
    configured = policy.get("tools")
    if isinstance(configured, dict):
        merged.update(configured)
    return merged


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _reject_unknown(record: dict[str, Any], allowed: tuple[str, ...], label: str) -> None:
    unknown = sorted(set(record) - set(allowed))
    if unknown:
        raise infra.InfraError(f"{label} has unknown fields: {', '.join(unknown)}")


def _require(record: dict[str, Any], required: tuple[str, ...], label: str) -> None:
    missing = sorted(key for key in required if key not in record or record[key] in (None, ""))
    if missing:
        raise infra.InfraError(f"{label} missing fields: {', '.join(missing)}")


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


def _check_non_negative(value: Any, label: str, integer: bool) -> None:
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
        if any(learning.contains_secret_like(text) for text in values):
            raise infra.InfraError(
                f"{label} field '{key}' looks like it may contain a credential; do not record secrets"
            )


def hash_target(value: str) -> str:
    """Hash a sensitive target (for example a file path) for repeated-op detection.

    Only the digest is persisted, never the raw target. The digest is stable for
    the same normalized string and domain-separated by a fixed prefix.
    """
    if not _present(value):
        raise infra.InfraError("Target must be a non-empty string")
    normalized = value.strip().replace("\\", "/")
    digest = hashlib.sha256(("local-ai-infra:target\x1f" + normalized).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def hash_prompt_prefix(value: str, limit: int = 2048) -> str:
    """Digest the stable prompt prefix for cache-reuse observability.

    Only the digest of the first ``limit`` characters is persisted; the prompt
    itself is never stored. Comparing digests across requests with the same
    logical agent context detects a prefix that keeps changing, which prevents
    provider prefix-cache reuse without exposing prompt content.
    """
    if not _present(value):
        raise infra.InfraError("Prompt prefix must be a non-empty string")
    digest = hashlib.sha256(("local-ai-infra:prefix\x1f" + value[:limit]).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def cache_hit_ratio(cached_input_tokens: Any, cache_miss_tokens: Any) -> float | None:
    """Return cached / (cached + miss) when both counts are known, else ``None``.

    A zero-token request has no ratio (never 0% or 100% by assumption).
    """
    for value in (cached_input_tokens, cache_miss_tokens):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
    total = cached_input_tokens + cache_miss_tokens
    if total <= 0:
        return None
    return round(cached_input_tokens / total, 4)


def _validate_limits(limits: dict[str, Any]) -> None:
    if limits.get("schema_version") != 1:
        raise infra.InfraError("Unsupported limits schema_version")
    timeouts = limits.get("timeouts")
    thresholds = limits.get("anomaly_thresholds")
    if not isinstance(timeouts, dict) or not isinstance(thresholds, dict):
        raise infra.InfraError("limits must define timeouts and anomaly_thresholds objects")
    model_timeout = timeouts.get("model_timeout_seconds")
    if isinstance(model_timeout, bool) or not isinstance(model_timeout, int) or model_timeout <= 0:
        raise infra.InfraError("timeouts.model_timeout_seconds must be a positive integer")
    max_runtime = timeouts.get("max_runtime_seconds")
    if max_runtime is not None:
        if isinstance(max_runtime, bool) or not isinstance(max_runtime, int) or max_runtime <= 0:
            raise infra.InfraError("timeouts.max_runtime_seconds must be a positive integer")
        if max_runtime < model_timeout:
            raise infra.InfraError("timeouts.max_runtime_seconds must not be less than model_timeout_seconds")
    for key in ("max_repeated_reads", "max_retries"):
        value = thresholds.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise infra.InfraError(f"anomaly_thresholds.{key} must be a non-negative integer")
    max_attempts = thresholds.get("max_attempts")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise infra.InfraError("anomaly_thresholds.max_attempts must be a positive integer")
    warn = thresholds.get("context_utilization_warn")
    if isinstance(warn, bool) or not isinstance(warn, (int, float)) or not 0 <= warn <= 1:
        raise infra.InfraError("anomaly_thresholds.context_utilization_warn must be a number in [0, 1]")
    tools = limits.get("tools")
    if tools is not None:
        if not isinstance(tools, dict):
            raise infra.InfraError("limits.tools must be an object")
        unknown = sorted(set(tools) - set(TOOL_LIMIT_KEYS))
        if unknown:
            raise infra.InfraError(f"limits.tools has unknown fields: {', '.join(unknown)}")
        for key in TOOL_LIMIT_KEYS:
            value = tools.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise infra.InfraError(f"limits.tools.{key} must be a positive integer")


def load_limits(path: pathlib.Path | None = None) -> dict[str, Any]:
    """Load and validate the limits policy, falling back to conservative defaults."""
    if path is None:
        path = infra.ROOT / "config/limits.yaml"
    if not path.exists():
        return copy.deepcopy(DEFAULT_LIMITS)
    limits = infra.load_json(path)
    _validate_limits(limits)
    return limits


def validate_span_event(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one runtime span before it is persisted."""
    if not isinstance(record, dict):
        raise infra.InfraError("Span record must be an object")
    _reject_unknown(record, SPAN_FIELDS, "Span record")
    _require(record, SPAN_REQUIRED, "Span record")
    if record["schema_version"] != SPAN_SCHEMA_VERSION:
        raise infra.InfraError("Unsupported span schema_version")
    _reject_secrets(record, "Span record")
    _check_uuid(record.get("trace_id"), "Span record trace_id")
    _check_uuid(record.get("span_id"), "Span record span_id")
    _check_uuid(record.get("parent_span_id"), "Span record parent_span_id")
    _check_uuid(record.get("run_id"), "Span record run_id")
    _check_timestamp(record.get("timestamp_utc"), "Span record timestamp_utc")
    _check_enum(record.get("stage"), SPAN_STAGES, "stage")
    _check_enum(record.get("tool_kind"), TOOL_KINDS, "tool_kind")
    _check_enum(record.get("tool_outcome"), TOOL_OUTCOMES, "tool_outcome")
    _check_enum(record.get("error_category"), RUNTIME_ERROR_CATEGORIES, "error_category")
    for field in _SPAN_NON_NEGATIVE_INT_FIELDS:
        _check_non_negative(record.get(field), field, integer=True)
    for field in _SPAN_NON_NEGATIVE_NUMBER_FIELDS:
        _check_non_negative(record.get(field), field, integer=False)
    utilization = record.get("context_utilization")
    if utilization is not None:
        if isinstance(utilization, bool) or not isinstance(utilization, (int, float)) or not 0 <= utilization <= 1:
            raise infra.InfraError("context_utilization must be a number in [0, 1]")
    ratio = record.get("cache_hit_ratio")
    if ratio is not None:
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
            raise infra.InfraError("cache_hit_ratio must be a number in [0, 1]")
    _check_bool(record.get("repeated"), "repeated")
    exit_code = record.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise infra.InfraError("exit_code must be an integer")
    attempt = record.get("attempt")
    if attempt is not None and attempt < 1:
        raise infra.InfraError("attempt must be at least 1")
    target_hash = record.get("target_hash")
    if target_hash is not None and not TARGET_HASH_PATTERN.match(str(target_hash)):
        raise infra.InfraError("target_hash must be a hashed target (use hash_target), never a raw path")
    prefix_hash = record.get("prompt_prefix_hash")
    if prefix_hash is not None and not TARGET_HASH_PATTERN.match(str(prefix_hash)):
        raise infra.InfraError("prompt_prefix_hash must be a digest (use hash_prompt_prefix), never a raw prompt")
    return record


def validate_ai_run_event(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one run/benchmark/eval event before it is persisted."""
    if not isinstance(record, dict):
        raise infra.InfraError("Run event must be an object")
    _reject_unknown(record, AI_RUN_FIELDS, "Run event")
    _require(record, AI_RUN_REQUIRED, "Run event")
    if record["schema_version"] != "1.0.0":
        raise infra.InfraError("Unsupported run event schema_version")
    _reject_secrets(record, "Run event")
    _check_uuid(record.get("run_id"), "Run event run_id")
    _check_uuid(record.get("event_id"), "Run event event_id")
    _check_timestamp(record.get("timestamp_utc"), "Run event timestamp_utc")
    _check_enum(record.get("status"), AI_RUN_STATUSES, "status")
    _check_enum(record.get("review_status"), REVIEW_STATUSES, "review_status")
    for field in _AI_RUN_NON_NEGATIVE_INT_FIELDS:
        _check_non_negative(record.get(field), field, integer=True)
    for field in _AI_RUN_NON_NEGATIVE_NUMBER_FIELDS:
        _check_non_negative(record.get(field), field, integer=False)
    _check_bool(record.get("structured_output_compliant"), "structured_output_compliant")
    return record


def validate_opencode_stats_event(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one normalized OpenCode stats snapshot row before it is persisted."""
    if not isinstance(record, dict):
        raise infra.InfraError("OpenCode stats record must be an object")
    _reject_unknown(record, OPENCODE_STATS_FIELDS, "OpenCode stats record")
    _require(record, OPENCODE_STATS_REQUIRED, "OpenCode stats record")
    if record["schema_version"] != OPENCODE_STATS_SCHEMA_VERSION:
        raise infra.InfraError("Unsupported OpenCode stats schema_version")
    _reject_secrets(record, "OpenCode stats record")
    _check_timestamp(record.get("captured_at"), "OpenCode stats record captured_at")
    snapshot_id = record.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not TARGET_HASH_PATTERN.match(snapshot_id):
        raise infra.InfraError("snapshot_id must be a content digest (sha256:...)")
    for field in _OPENCODE_STATS_NON_NEGATIVE_INT_FIELDS:
        _check_non_negative(record.get(field), field, integer=True)
    for field in _OPENCODE_STATS_NON_NEGATIVE_NUMBER_FIELDS:
        _check_non_negative(record.get(field), field, integer=False)
    ratio = record.get("cache_hit_ratio")
    if ratio is not None:
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
            raise infra.InfraError("OpenCode stats cache_hit_ratio must be a number in [0, 1]")
    return record


def validate_task_outcome(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one completed-task outcome event before it is persisted."""
    if not isinstance(record, dict):
        raise infra.InfraError("Task outcome record must be an object")
    _reject_unknown(record, TASK_OUTCOME_FIELDS, "Task outcome record")
    _require(record, TASK_OUTCOME_REQUIRED, "Task outcome record")
    if record["schema_version"] != TASK_OUTCOME_SCHEMA_VERSION:
        raise infra.InfraError("Unsupported task outcome schema_version")
    _reject_secrets(record, "Task outcome record")
    for field in ("task_id", "event_id", "run_id"):
        _check_uuid(record.get(field), f"Task outcome record {field}")
    _check_timestamp(record.get("completed_at"), "Task outcome record completed_at")
    if record.get("started_at") is not None:
        _check_timestamp(record.get("started_at"), "Task outcome record started_at")
    _check_enum(record.get("status"), TASK_OUTCOME_STATUSES, "status")
    _check_enum(record.get("error_category"), RUNTIME_ERROR_CATEGORIES, "error_category")
    for field in _TASK_OUTCOME_BOOL_FIELDS:
        _check_bool(record.get(field), field)
    for field in _TASK_OUTCOME_NON_NEGATIVE_INT_FIELDS:
        _check_non_negative(record.get(field), field, integer=True)
    for field in _TASK_OUTCOME_NON_NEGATIVE_NUMBER_FIELDS:
        _check_non_negative(record.get(field), field, integer=False)
    if record.get("escalated") is False and record.get("escalation_count") not in (None, 0):
        raise infra.InfraError("Task outcome escalation_count must be 0 when escalated is false")
    return record


def _dedupe_spans(spans: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop duplicate span_ids (for example from an accidental double append)."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    duplicates = 0
    for span in spans:
        span_id = span.get("span_id")
        if isinstance(span_id, str) and span_id in seen:
            duplicates += 1
            continue
        if isinstance(span_id, str):
            seen.add(span_id)
        unique.append(span)
    return unique, duplicates


def _known_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _known_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sum_field(records: list[dict[str, Any]], field: str, *, integer: bool) -> Any:
    total = 0
    known = False
    for record in records:
        value = record.get(field)
        if _known_int(value) if integer else _known_number(value):
            total += value
            known = True
    return total if known else None


def _average(total: Any, denominator: int) -> float | None:
    if total is None or denominator <= 0:
        return None
    return round(total / denominator, 3)


def _usage_group(spans: list[dict[str, Any]]) -> dict[str, Any]:
    requests = len(spans)
    requests_with_usage = sum(
        1 for span in spans if _known_int(span.get("input_tokens")) or _known_int(span.get("output_tokens"))
    )
    input_tokens = _sum_field(spans, "input_tokens", integer=True)
    cached = _sum_field(spans, "cached_input_tokens", integer=True)
    miss = _sum_field(spans, "cache_miss_tokens", integer=True)
    output = _sum_field(spans, "output_tokens", integer=True)
    total_tokens = _sum_field(spans, "total_tokens", integer=True)
    estimated_cost = _sum_field(spans, "estimated_total_cost", integer=False)
    cost_requests = sum(1 for span in spans if _known_number(span.get("estimated_total_cost")))
    provider_cost = _sum_field(spans, "provider_reported_cost", integer=False)
    provider_cost_requests = sum(1 for span in spans if _known_number(span.get("provider_reported_cost")))
    latencies = [span["duration_ms"] for span in spans if _known_number(span.get("duration_ms"))]
    ratio = None
    if cached is not None and miss is not None and (cached + miss) > 0:
        ratio = round(cached / (cached + miss), 4)
    return {
        "requests": requests,
        "successful_requests": sum(1 for span in spans if not span.get("error_category")),
        "requests_with_usage": requests_with_usage,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_miss_tokens": miss,
        "output_tokens": output,
        "total_tokens": total_tokens,
        "cache_hit_ratio": ratio,
        "avg_input_tokens_per_request": _average(input_tokens, requests_with_usage),
        "avg_cached_tokens_per_request": _average(cached, requests_with_usage),
        "avg_output_tokens_per_request": _average(output, requests_with_usage),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else None,
        "estimated_total_cost": estimated_cost,
        "avg_estimated_cost_per_request": _average(estimated_cost, cost_requests),
        "provider_reported_cost": provider_cost,
        "provider_cost_observations": provider_cost_requests,
    }


def summarize_usage(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate provider token/cache/latency/cost usage from inference spans.

    Only ``inference`` spans count as provider requests. Averages use the number
    of requests that actually reported usage (or cost), so a failed call without
    token metadata never dilutes the result. Unavailable aggregates stay ``None``.
    """
    inference = [span for span in spans if span.get("stage") == "inference"]
    overall = _usage_group(inference)
    grouped: dict[tuple, list[dict[str, Any]]] = {}
    for span in inference:
        grouped.setdefault((span.get("provider"), span.get("model")), []).append(span)
    breakdown: list[dict[str, Any]] = []
    for provider, model in sorted(grouped, key=lambda key: (str(key[0]), str(key[1]))):
        group = _usage_group(grouped[(provider, model)])
        group["provider"] = provider
        group["model"] = model
        breakdown.append(group)
    return {
        "total_requests": overall["requests"],
        "successful_requests": overall["successful_requests"],
        "requests_with_usage": overall["requests_with_usage"],
        "input_tokens": overall["input_tokens"],
        "cached_input_tokens": overall["cached_input_tokens"],
        "cache_miss_tokens": overall["cache_miss_tokens"],
        "output_tokens": overall["output_tokens"],
        "total_tokens": overall["total_tokens"],
        "cache_hit_ratio": overall["cache_hit_ratio"],
        "avg_input_tokens_per_request": overall["avg_input_tokens_per_request"],
        "avg_cached_tokens_per_request": overall["avg_cached_tokens_per_request"],
        "avg_output_tokens_per_request": overall["avg_output_tokens_per_request"],
        "avg_latency_ms": overall["avg_latency_ms"],
        "estimated_total_cost": overall["estimated_total_cost"],
        "avg_estimated_cost_per_request": overall["avg_estimated_cost_per_request"],
        "provider_reported_cost": overall["provider_reported_cost"],
        "by_provider_model": breakdown,
    }


def summarize_opencode_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate OpenCode stats snapshots by provider/model/variant.

    OpenCode reports cumulative aggregates, so each key reports its *latest*
    snapshot rather than summing every import (which would double count).
    Provider-reported cost stays separate from locally estimated cost.
    """
    latest: dict[tuple, dict[str, Any]] = {}
    snapshot_ids: set = set()
    for record in records:
        snapshot_ids.add(record.get("snapshot_id"))
        key = (record.get("provider"), record.get("model"), record.get("variant"))
        captured = record.get("captured_at") or ""
        current = latest.get(key)
        if current is None or captured >= (current.get("captured_at") or ""):
            latest[key] = record
    rows = list(latest.values())
    overall = _usage_group(rows)
    breakdown: list[dict[str, Any]] = []
    for provider, model, variant in sorted(latest, key=lambda key: (str(key[0]), str(key[1]), str(key[2]))):
        group = _usage_group([latest[(provider, model, variant)]])
        group.update({"provider": provider, "model": model, "variant": variant})
        breakdown.append(group)
    return {
        "snapshot_count": len(snapshot_ids),
        "model_rows": len(rows),
        "input_tokens": overall["input_tokens"],
        "cached_input_tokens": overall["cached_input_tokens"],
        "cache_miss_tokens": overall["cache_miss_tokens"],
        "output_tokens": overall["output_tokens"],
        "reasoning_tokens": _sum_field(rows, "reasoning_tokens", integer=True),
        "cache_write_tokens": _sum_field(rows, "cache_write_tokens", integer=True),
        "cache_hit_ratio": overall["cache_hit_ratio"],
        "provider_reported_cost": overall["provider_reported_cost"],
        "by_provider_model_variant": breakdown,
    }


def _outcome_attr(obj: Any, name: str) -> Any:
    return getattr(obj, name, None) if obj is not None else None


def _outcome_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _outcome_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _sum_if_all_known(values: list) -> float | None:
    """Sum values only when every component is known; never invent a partial total."""
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return round(total, 8)


def _outcome_mean(total: float | None, denominator: int, digits: int = 6) -> float | None:
    if total is None or denominator <= 0:
        return None
    return round(total / denominator, digits)


def _result_estimated_cost(result: Any, pricing_config: dict) -> dict:
    if result is None:
        return pricing.empty_cost()
    return pricing.estimate_usage_cost(
        _outcome_attr(result, "provider"),
        _outcome_attr(result, "model"),
        input_tokens=_outcome_attr(result, "input_tokens"),
        cached_input_tokens=_outcome_attr(result, "cached_input_tokens"),
        cache_miss_tokens=_outcome_attr(result, "cache_miss_tokens"),
        output_tokens=_outcome_attr(result, "output_tokens"),
        pricing=pricing_config,
    )


def build_task_outcome(
    student_result: Any,
    *,
    escalation: Any = None,
    task_id: Optional[str] = None,
    task_type: Optional[str] = None,
    tests_passed: Optional[bool] = None,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    initial_variant: Optional[str] = None,
    final_variant: Optional[str] = None,
    provider_reported_cost: Optional[float] = None,
    pricing_config: Optional[dict] = None,
    event_id: Optional[str] = None,
    success_known: bool = True,
) -> dict[str, Any]:
    """Build one task outcome from the runtime result and optional escalation.

    Success is taken from the final runtime result's ``status`` (the teacher
    result when the task escalated and the teacher ran, otherwise the student
    result); ``tests_passed`` is only what the caller already knows and is never
    inferred from model text. Costs reuse ``pricing.estimate_usage_cost`` per
    attempt and are summed only when every attempt cost is known. Unknown values
    stay ``None``.

    ``success_known=False`` keeps ``status`` (execution state) but leaves
    ``success``/``error_category``/``error_code`` ``None``. Callers use it when
    the source only proves execution completion, not semantic task success.
    """
    teacher_result = _outcome_attr(escalation, "teacher_result")
    escalated = bool(_outcome_attr(escalation, "escalated"))
    uses_teacher = escalated and teacher_result is not None
    final_result = teacher_result if uses_teacher else student_result

    resolved_task_id = task_id or _outcome_attr(student_result, "trace_id") or _outcome_attr(escalation, "trace_id")
    if not isinstance(resolved_task_id, str) or not resolved_task_id:
        raise infra.InfraError("Task outcome requires a task_id (trace_id)")

    status = _outcome_attr(final_result, "status")
    if status not in ("succeeded", "failed", "cancelled"):
        status = "unknown"
    if success_known:
        success = None if status == "unknown" else status == "succeeded"
    else:
        success = None

    student_attempts = _outcome_int(_outcome_attr(student_result, "attempts"))
    attempt_count = student_attempts
    if uses_teacher and student_attempts is not None:
        teacher_attempts = _outcome_int(_outcome_attr(teacher_result, "attempts"))
        attempt_count = None if teacher_attempts is None else student_attempts + teacher_attempts

    latencies = [_outcome_number(_outcome_attr(student_result, "duration_ms"))]
    if uses_teacher:
        latencies.append(_outcome_number(_outcome_attr(teacher_result, "duration_ms")))
    total_latency_ms = _sum_if_all_known(latencies)

    resolved_pricing = pricing_config if pricing_config is not None else pricing.load_pricing()
    student_cost = _result_estimated_cost(student_result, resolved_pricing)
    teacher_cost = _result_estimated_cost(teacher_result, resolved_pricing) if uses_teacher else None
    cost_parts = [student_cost["estimated_total_cost"]]
    if uses_teacher:
        cost_parts.append(teacher_cost["estimated_total_cost"])
    estimated_total_cost = _sum_if_all_known(cost_parts)
    currency = student_cost.get("pricing_currency") or (teacher_cost or {}).get("pricing_currency")

    if initial_variant is None:
        initial_variant = _outcome_attr(_outcome_attr(escalation, "context_pack"), "source_variant")

    record = {
        "schema_version": TASK_OUTCOME_SCHEMA_VERSION,
        "task_id": resolved_task_id,
        "event_id": event_id or str(uuid.uuid4()),
        "completed_at": completed_at or infra.utc_now(),
        "started_at": started_at,
        "run_id": _outcome_attr(final_result, "run_id"),
        "task_type": task_type,
        "status": status,
        "success": success,
        "tests_passed": tests_passed,
        "escalated": escalated,
        "escalation_count": 1 if escalated else 0,
        "attempt_count": attempt_count,
        "initial_provider": _outcome_attr(student_result, "provider"),
        "initial_model": _outcome_attr(student_result, "model"),
        "initial_variant": initial_variant,
        "final_provider": _outcome_attr(final_result, "provider"),
        "final_model": _outcome_attr(final_result, "model"),
        "final_variant": final_variant,
        "total_latency_ms": total_latency_ms,
        "provider_reported_cost": provider_reported_cost,
        "estimated_total_cost": estimated_total_cost,
        "pricing_currency": currency,
        "error_category": None if (success or not success_known) else _outcome_attr(final_result, "error_category"),
        "error_code": None if (success or not success_known) else _outcome_attr(final_result, "error_code"),
    }
    return validate_task_outcome(record)


def dedupe_task_outcomes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the latest outcome per ``task_id`` so repeated recording stays idempotent.

    Files stay append-only; like the span and OpenCode aggregators, duplicates
    are collapsed at read time rather than rewritten on disk.
    """
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        task_id = record.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        latest[task_id] = record
    return list(latest.values())


def _rate(numerator: int, denominator: int) -> float | None:
    return (numerator / denominator) if denominator else None


def _task_outcome_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = len(records)
    known_success = [record for record in records if isinstance(record.get("success"), bool)]
    succeeded = sum(1 for record in records if record.get("success") is True)
    escalated = sum(1 for record in records if record.get("escalated") is True)
    attempts = [record["attempt_count"] for record in records if _known_int(record.get("attempt_count"))]
    latencies = [record["total_latency_ms"] for record in records
                 if _known_number(record.get("total_latency_ms"))]
    costs = [record["estimated_total_cost"] for record in records
             if _known_number(record.get("estimated_total_cost"))]
    success_costs = [record["estimated_total_cost"] for record in records
                     if record.get("success") is True and _known_number(record.get("estimated_total_cost"))]
    return {
        "tasks": tasks,
        "succeeded": succeeded,
        "tasks_with_known_success": len(known_success),
        "escalated": escalated,
        "success_rate": _rate(succeeded, len(known_success)),
        "escalation_rate": _rate(escalated, tasks),
        "tasks_with_known_attempts": len(attempts),
        "avg_attempts": _outcome_mean(sum(attempts), len(attempts), 3),
        "tasks_with_known_latency": len(latencies),
        "avg_latency_ms": _outcome_mean(sum(latencies), len(latencies), 3),
        "tasks_with_known_cost": len(costs),
        "avg_cost_per_task": _outcome_mean(sum(costs), len(costs)),
        "successful_tasks_with_known_cost": len(success_costs),
        "avg_cost_per_successful_task": _outcome_mean(sum(success_costs), len(success_costs)),
    }


def summarize_task_outcomes(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one outcome per task into success/escalation/latency/cost metrics.

    Unknown values are excluded from the averages that need them rather than
    treated as zero; rates are ``None`` when their denominator is empty.
    """
    unique = dedupe_task_outcomes(records)
    overall = _task_outcome_group(unique)
    model_keys = sorted(
        {(record.get("final_provider"), record.get("final_model"), record.get("final_variant"))
         for record in unique},
        key=lambda key: tuple(str(part) for part in key),
    )
    by_model: list[dict[str, Any]] = []
    for key in model_keys:
        group = _task_outcome_group([
            record for record in unique
            if (record.get("final_provider"), record.get("final_model"), record.get("final_variant")) == key
        ])
        group.update({"provider": key[0], "model": key[1], "variant": key[2]})
        by_model.append(group)
    task_types = sorted({record.get("task_type") for record in unique}, key=lambda value: str(value))
    by_task_type: list[dict[str, Any]] = []
    for task_type in task_types:
        group = _task_outcome_group([record for record in unique if record.get("task_type") == task_type])
        group["task_type"] = task_type
        by_task_type.append(group)
    return {
        "record_count": len(records),
        "duplicate_count": max(0, len(records) - len(unique)),
        "task_count": len(unique),
        **overall,
        "by_final_provider_model_variant": by_model,
        "by_task_type": by_task_type,
    }


def _escalation_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": record.get("run_id"),
        "trace_id": record.get("trace_id") or record.get("run_id"),
        "task_class": record.get("task_class"),
        "student_model": record.get("student_model"),
        "failure_category": record.get("failure_category"),
        "teacher_provider": record.get("teacher_provider"),
        "teacher_model": record.get("teacher_model"),
        "teacher_reason": record.get("teacher_reason"),
        "teacher_outcome": record.get("teacher_outcome"),
        "human_review_outcome": record.get("human_review_outcome"),
        "duration_ms": record.get("duration_ms"),
        "estimated_cost_usd": record.get("estimated_cost_usd"),
        "input_tokens": record.get("cost_input_tokens"),
        "output_tokens": record.get("cost_output_tokens"),
    }


def summarize_trace(
    trace_id: str,
    spans: list[dict[str, Any]],
    escalation: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate one trace's spans into stage timing, tool, token, and anomaly data."""
    policy = limits or DEFAULT_LIMITS
    thresholds = policy.get("anomaly_thresholds", {})
    unique, duplicates = _dedupe_spans(spans)

    duration_by_stage: dict[str, float] = {}
    read_targets: dict[str, int] = {}
    tool_calls = tool_failures = tool_timeouts = 0
    file_reads = file_writes = shell_commands = 0
    retries = 0
    attempts = 0
    token_totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "cache_miss_tokens": 0,
                    "reasoning_tokens": 0, "total_tokens": 0}
    token_known = {key: False for key in token_totals}
    max_context_size = None
    max_context_utilization = None
    instrumented_ms = 0.0

    for span in unique:
        stage = span.get("stage", "other")
        duration = float(span.get("duration_ms") or 0)
        duration_by_stage[stage] = duration_by_stage.get(stage, 0.0) + duration
        instrumented_ms += duration
        attempt = span.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > attempts:
            attempts = attempt
        retry_count = span.get("retry_count")
        if isinstance(retry_count, int) and not isinstance(retry_count, bool) and retry_count > 0:
            retries += retry_count
        for key in token_totals:
            value = span.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                token_totals[key] += value
                token_known[key] = True
        context_size = span.get("context_size")
        if isinstance(context_size, (int, float)) and not isinstance(context_size, bool):
            max_context_size = max(max_context_size or 0, context_size)
        utilization = span.get("context_utilization")
        if isinstance(utilization, (int, float)) and not isinstance(utilization, bool):
            max_context_utilization = max(max_context_utilization or 0, utilization)
        if stage == "tool":
            tool_calls += 1
            outcome = span.get("tool_outcome")
            if outcome in ("failed", "timeout"):
                tool_failures += 1
            if outcome == "timeout":
                tool_timeouts += 1
            kind = span.get("tool_kind")
            if kind == "read":
                if outcome in (None, "succeeded"):
                    file_reads += 1
                target = span.get("target_hash") or span.get("tool_name")
                if target:
                    read_targets[target] = read_targets.get(target, 0) + 1
            elif kind == "write":
                file_writes += 1
            elif kind == "shell":
                shell_commands += 1

    repeated_reads = sum(count - 1 for count in read_targets.values() if count > 1)

    warnings: list[str] = []
    if repeated_reads > thresholds.get("max_repeated_reads", 3):
        warnings.append(f"repeated_reads {repeated_reads} exceeds {thresholds.get('max_repeated_reads')}")
    if retries > thresholds.get("max_retries", 2):
        warnings.append(f"retries {retries} exceeds {thresholds.get('max_retries')}")
    if attempts > thresholds.get("max_attempts", 5):
        warnings.append(f"attempts {attempts} exceeds {thresholds.get('max_attempts')}")
    if max_context_utilization is not None and max_context_utilization > thresholds.get("context_utilization_warn", 0.85):
        warnings.append(f"context_utilization {max_context_utilization:.2f} exceeds warning threshold")
    max_tool_calls = (policy.get("tools") or {}).get("max_tool_calls")
    if isinstance(max_tool_calls, int) and not isinstance(max_tool_calls, bool) and tool_calls > max_tool_calls:
        warnings.append(f"tool_calls {tool_calls} exceeds {max_tool_calls}")

    return {
        "trace_id": trace_id,
        "span_count": len(unique),
        "duplicate_span_count": duplicates,
        "duration_by_stage": duration_by_stage,
        "instrumented_duration_ms": instrumented_ms,
        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "tool_timeouts": tool_timeouts,
        "file_reads": file_reads,
        "file_writes": file_writes,
        "shell_commands": shell_commands,
        "repeated_reads": repeated_reads,
        "retries": retries,
        "attempts": attempts,
        "input_tokens": token_totals["input_tokens"] if token_known["input_tokens"] else None,
        "output_tokens": token_totals["output_tokens"] if token_known["output_tokens"] else None,
        "cached_input_tokens": token_totals["cached_input_tokens"] if token_known["cached_input_tokens"] else None,
        "cache_miss_tokens": token_totals["cache_miss_tokens"] if token_known["cache_miss_tokens"] else None,
        "reasoning_tokens": token_totals["reasoning_tokens"] if token_known["reasoning_tokens"] else None,
        "total_tokens": token_totals["total_tokens"] if token_known["total_tokens"] else None,
        "max_context_size": max_context_size,
        "max_context_utilization": max_context_utilization,
        "escalation": _escalation_summary(escalation) if escalation else None,
        "warnings": warnings,
    }


def build_trace_report(
    spans: list[dict[str, Any]],
    escalations: list[dict[str, Any]],
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Group spans and escalation rows by trace_id into a per-trace report."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    uncorrelated = 0
    for span in spans:
        trace_id = span.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            uncorrelated += 1
            continue
        grouped.setdefault(trace_id, []).append(span)
    escalations_by_trace: dict[str, dict[str, Any]] = {}
    for record in escalations:
        trace_id = record.get("trace_id") or record.get("run_id")
        if isinstance(trace_id, str) and trace_id:
            escalations_by_trace.setdefault(trace_id, record)
    for trace_id, record in escalations_by_trace.items():
        grouped.setdefault(trace_id, [])
    traces = [
        summarize_trace(trace_id, grouped[trace_id], escalations_by_trace.get(trace_id), limits)
        for trace_id in sorted(grouped)
    ]
    return {"trace_count": len(traces), "uncorrelated_span_count": uncorrelated, "traces": traces}
