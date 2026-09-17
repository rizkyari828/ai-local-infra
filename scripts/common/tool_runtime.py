#!/usr/bin/env python3
"""Bounded, observable execution of tools (stdlib only).

``ToolRuntime`` resolves a registered tool, enforces the workspace and limits,
executes under a timeout/cancellation context, normalizes the result, and emits
one validated ``tool`` span automatically. Prompts, source, file contents, and
raw command output are never persisted; only bounded metadata is recorded.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import infra
import telemetry
import tools
from tools import ToolContext, ToolError, ToolFailure, Workspace

TOOL_RUNTIME_VERSION = "0.1.0"


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass
class ToolResult:
    """Normalized tool result, kept separate from provider/model results."""

    tool_name: str
    success: bool
    duration_ms: float
    trace_id: Optional[str] = None
    run_id: Optional[str] = None
    output: Optional[str] = None
    error_category: Optional[str] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    retryable: bool = False
    metadata: dict = field(default_factory=dict)
    span_id: Optional[str] = None
    span_written: bool = False
    tool_runtime_version: str = TOOL_RUNTIME_VERSION

    def as_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "output": self.output,
            "error_category": self.error_category,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "retryable": self.retryable,
            "metadata": self.metadata,
            "span_id": self.span_id,
            "span_written": self.span_written,
            "tool_runtime_version": self.tool_runtime_version,
        }


class ToolRuntime:
    """Execute tools against one explicit workspace with automatic telemetry."""

    def __init__(
        self,
        workspace: Any,
        registry: Optional[tools.ToolRegistry] = None,
        limits: Optional[dict] = None,
        span_output: Optional[Any] = None,
        role: str = "student",
        record_spans: bool = True,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        self.registry = registry if registry is not None else tools.build_default_registry()
        self.limits = limits if limits is not None else telemetry.load_limits()
        self.tool_limits = telemetry.load_tool_limits(self.limits)
        self.span_output = infra.ROOT / "telemetry/spans/runs.jsonl" if span_output is None else span_output
        self.role = role
        self.record_spans = record_spans
        self._clock = clock or time.perf_counter

    def describe(self) -> dict:
        return {
            "workspace": str(self.workspace.root),
            "tools": self.registry.describe(),
            "limits": dict(self.tool_limits),
        }

    def call(
        self,
        tool_name: str,
        params: Optional[dict] = None,
        *,
        trace_id: str,
        run_id: Optional[str] = None,
        cancel: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        attempt: int = 1,
    ) -> ToolResult:
        started = self._clock()
        tool = self.registry.get(tool_name) if isinstance(tool_name, str) else None
        if tool is None:
            error = tools.ToolConfigurationError("unknown_tool", "tool is not registered")
            return self._failure(tool_name if isinstance(tool_name, str) else "", started,
                                 trace_id, run_id, attempt, error)
        try:
            validated = tool.validate(params or {})
        except ToolError as exc:
            return self._failure(tool.name, started, trace_id, run_id, attempt, exc)
        except Exception:
            error = ToolFailure("invalid_params", "tool parameters could not be parsed",
                                category="validation_failure")
            return self._failure(tool.name, started, trace_id, run_id, attempt, error)

        configured_timeout = float(self.tool_limits["tool_timeout_seconds"])
        timeout = configured_timeout
        if timeout_seconds is not None:
            try:
                timeout = min(float(timeout_seconds), configured_timeout)
            except (TypeError, ValueError):
                timeout = configured_timeout
        context = ToolContext(
            workspace=self.workspace,
            timeout_seconds=timeout,
            limits=self.tool_limits,
            cancel=cancel,
            trace_id=trace_id,
            run_id=run_id,
        )
        try:
            if cancel is not None and cancel.is_set():
                raise tools.ToolCancelled("cancelled", "tool call was cancelled")
            result = tool.execute(validated, context)
        except ToolError as exc:
            return self._failure(tool.name, started, trace_id, run_id, attempt, exc)
        except Exception:
            error = tools.ToolError("unexpected_error", category="unknown")
            return self._failure(tool.name, started, trace_id, run_id, attempt, error)

        duration_ms = round((self._clock() - started) * 1000, 3)
        span_id, written = self._record_span(tool.name, tool.kind, "succeeded", duration_ms,
                                             trace_id, run_id, attempt, result.metadata, None)
        return ToolResult(
            tool_name=tool.name,
            success=True,
            duration_ms=duration_ms,
            trace_id=trace_id,
            run_id=run_id,
            output=result.output,
            metadata=result.metadata,
            span_id=span_id,
            span_written=written,
        )

    def reject(
        self,
        tool_name: str,
        error: ToolError,
        *,
        trace_id: str,
        run_id: Optional[str] = None,
        attempt: int = 1,
    ) -> ToolResult:
        """Record a runtime-level refusal (for example a tool-call budget) as a tool span."""
        return self._failure(tool_name, self._clock(), trace_id, run_id, attempt, error)

    def _failure(
        self,
        tool_name: str,
        started: float,
        trace_id: str,
        run_id: Optional[str],
        attempt: int,
        error: ToolError,
    ) -> ToolResult:
        duration_ms = round((self._clock() - started) * 1000, 3)
        tool = self.registry.get(tool_name)
        kind = tool.kind if tool is not None else None
        outcome = {"tool_timeout": "timeout", "user_cancelled": "cancelled"}.get(error.category, "failed")
        metadata = dict(error.metadata)
        span_id, written = self._record_span(tool_name, kind, outcome, duration_ms,
                                             trace_id, run_id, attempt, metadata, error)
        return ToolResult(
            tool_name=tool_name,
            success=False,
            duration_ms=duration_ms,
            trace_id=trace_id,
            run_id=run_id,
            output=error.output,
            error_category=error.category,
            error_code=error.code,
            error_detail=error.detail,
            retryable=error.retryable,
            metadata=metadata,
            span_id=span_id,
            span_written=written,
        )

    def _record_span(
        self,
        tool_name: str,
        kind: Optional[str],
        outcome: str,
        duration_ms: float,
        trace_id: str,
        run_id: Optional[str],
        attempt: int,
        metadata: dict,
        error: Optional[ToolError],
    ) -> tuple:
        target_hash = metadata.get("target_hash")
        if not isinstance(target_hash, str) or not telemetry.TARGET_HASH_PATTERN.match(target_hash):
            target_hash = None
        if kind is None:
            tool_kind = None
        else:
            tool_kind = kind if kind in telemetry.TOOL_KINDS else "other"
        exit_code = metadata.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            exit_code = None
        span_id = str(uuid.uuid4())
        record = {
            "schema_version": telemetry.SPAN_SCHEMA_VERSION,
            "trace_id": trace_id,
            "span_id": span_id,
            "timestamp_utc": infra.utc_now(),
            "parent_span_id": None,
            "run_id": run_id,
            "stage": "tool",
            "duration_ms": duration_ms,
            "model": None,
            "provider": None,
            "role": self.role,
            "attempt": attempt if attempt >= 1 else 1,
            "retry_count": 0,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "context_size": None,
            "context_utilization": None,
            "tool_name": tool_name or None,
            "tool_kind": tool_kind,
            "tool_outcome": outcome,
            "target_hash": target_hash,
            "repeated": None,
            "bytes_read": _int_or_none(metadata.get("bytes_read")),
            "bytes_written": _int_or_none(metadata.get("bytes_written")),
            "exit_code": exit_code,
            "result_count": _int_or_none(metadata.get("result_count")),
            "error_category": error.category if error is not None else None,
            "error_code": error.code if error is not None else None,
            "limits_version": self.limits.get("limits_version"),
            "sanitized_note": None,
        }
        written = False
        if self.record_spans:
            try:
                telemetry.validate_span_event(record)
                if self.span_output is not None:
                    infra.append_jsonl(self.span_output, record)
                written = True
            except (infra.InfraError, OSError):
                written = False
        return span_id, written
