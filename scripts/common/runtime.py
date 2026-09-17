#!/usr/bin/env python3
"""Smallest practical Agent Runtime (stdlib only).

The runtime receives a task, creates trace/run context, validates the request,
enforces limits, executes the resolved provider, records a runtime span
automatically, classifies failure, and returns a normalized result.

It is intentionally not a multi-agent framework: there is no planning loop,
routing policy, or hosted provider. One task keeps a single ``trace_id`` across
every attempt, and the result carries it, so a future DeepSeek/Codex escalation
can inherit the same trace without changing this execution flow.

Automatic telemetry: every attempt emits one completed ``inference`` span using
the existing span schema and secret guard. Callers never call ``record-span``
for normal provider execution. Prompts and response content are never persisted.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import infra
import providers
import telemetry
import tools
from providers import (
    ModelProvider,
    ProviderCancelled,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderTimeout,
)
from tool_runtime import ToolResult, ToolRuntime

RUNTIME_VERSION = "0.1.0"


@dataclass
class AgentTask:
    """One unit of work for the runtime."""

    prompt: str
    trace_id: Optional[str] = None
    run_id: Optional[str] = None
    model: Optional[str] = None
    role: str = "student"
    task_type: Optional[str] = None
    timeout_seconds: Optional[float] = None
    runtime_budget_seconds: Optional[float] = None
    max_attempts: Optional[int] = None
    options: dict = field(default_factory=dict)
    keep_alive: Optional[str] = None
    cancel: Optional[threading.Event] = None


@dataclass
class RuntimeResult:
    """Normalized runtime outcome, sufficient for future routing decisions."""

    run_id: str
    trace_id: str
    span_id: Optional[str]
    status: str
    provider: str
    model: Optional[str]
    content: Optional[str]
    duration_ms: float
    attempts: int
    error_category: Optional[str]
    error_code: Optional[str]
    retryable: bool
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    context_size: Optional[int] = None
    context_utilization: Optional[float] = None
    finish_reason: Optional[str] = None
    limits_version: Optional[str] = None
    span_written: bool = False
    runtime_version: str = RUNTIME_VERSION

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "content": self.content,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "error_category": self.error_category,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "context_size": self.context_size,
            "context_utilization": self.context_utilization,
            "finish_reason": self.finish_reason,
            "limits_version": self.limits_version,
            "span_written": self.span_written,
            "runtime_version": self.runtime_version,
        }


@dataclass
class TaskSession:
    """Shared run/trace/budget/cancellation context for one task.

    Inference and tool calls within one session keep a single ``trace_id`` and a
    single runtime budget, and share one tool-call counter so the tool budget is
    enforced across the whole task rather than per tool.
    """

    run_id: str
    trace_id: str
    started: float
    budget: float
    cancel: Optional[threading.Event] = None
    role: str = "student"
    tool_calls: int = 0

    def remaining(self, clock: Callable[[], float]) -> float:
        return max(0.0, self.budget - (clock() - self.started))

    def describe(self) -> dict:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "budget_seconds": self.budget,
            "role": self.role,
            "tool_calls": self.tool_calls,
        }


class AgentRuntime:
    """Execute local-model tasks through one resolved provider."""

    def __init__(
        self,
        provider: ModelProvider,
        limits: Optional[dict] = None,
        span_output: Optional[Any] = None,
        role: str = "student",
        record_spans: bool = True,
        clock: Optional[Callable[[], float]] = None,
        tools: Optional[ToolRuntime] = None,
    ) -> None:
        if not isinstance(provider, ModelProvider):
            raise providers.ProviderConfigurationError("invalid_provider", "runtime requires a ModelProvider")
        self.provider = provider
        self.limits = limits if limits is not None else telemetry.load_limits()
        self.span_output = infra.ROOT / "telemetry/spans/runs.jsonl" if span_output is None else span_output
        self.role = role
        self.record_spans = record_spans
        self._clock = clock or time.perf_counter
        self.tools = tools

    @classmethod
    def from_config(
        cls,
        config: Optional[dict] = None,
        provider_name: Optional[str] = None,
        limits: Optional[dict] = None,
        span_output: Optional[Any] = None,
        role: str = "student",
        tools: Optional[ToolRuntime] = None,
    ) -> "AgentRuntime":
        runtime_config = config if config is not None else providers.load_runtime_config()
        provider = providers.build_provider(runtime_config, provider_name)
        return cls(provider=provider, limits=limits, span_output=span_output, role=role, tools=tools)

    def health_check(self) -> dict:
        """Read-only readiness check; never downloads a model."""
        return self.provider.health_check().as_dict()

    def execute(self, prompt: str, **task_fields: Any) -> RuntimeResult:
        return self.run(AgentTask(prompt=prompt, **task_fields))

    def open_session(self, task: AgentTask) -> TaskSession:
        """Create the shared run/trace/budget/cancellation context for one task."""
        run_id = task.run_id or str(uuid.uuid4())
        trace_id = task.trace_id or run_id
        per_attempt_timeout = self._per_attempt_timeout(task)
        max_attempts = self._allowed_attempts(task)
        budget = self._runtime_budget(task, per_attempt_timeout, max_attempts)
        return TaskSession(
            run_id=run_id,
            trace_id=trace_id,
            started=self._clock(),
            budget=budget,
            cancel=task.cancel,
            role=task.role or self.role,
        )

    def call_tool(
        self,
        session: TaskSession,
        tool_name: str,
        params: Optional[dict] = None,
        timeout_seconds: Optional[float] = None,
    ) -> ToolResult:
        """Execute a registered tool inside an existing session.

        The call shares the session's trace_id/run_id, runtime budget, and
        cancellation event, and counts against the session tool-call budget.
        """
        if self.tools is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                duration_ms=0.0,
                trace_id=session.trace_id,
                run_id=session.run_id,
                error_category="configuration_failure",
                error_code="tools_not_configured",
            )
        attempt = 1
        if session.tool_calls >= int(self.tools.tool_limits["max_tool_calls"]):
            error = tools.ToolResourceLimit("max_tool_calls_exceeded", "tool-call budget exhausted")
            return self.tools.reject(tool_name, error, trace_id=session.trace_id,
                                     run_id=session.run_id, attempt=attempt)
        if session.cancel is not None and session.cancel.is_set():
            error = tools.ToolCancelled("cancelled", "session was cancelled")
            return self.tools.reject(tool_name, error, trace_id=session.trace_id,
                                     run_id=session.run_id, attempt=attempt)
        remaining = session.remaining(self._clock)
        if remaining <= 0:
            error = tools.ToolResourceLimit("max_runtime_exceeded", "runtime budget exhausted")
            return self.tools.reject(tool_name, error, trace_id=session.trace_id,
                                     run_id=session.run_id, attempt=attempt)
        session.tool_calls += 1
        try:
            timeout = remaining if timeout_seconds is None else min(float(timeout_seconds), remaining)
        except (TypeError, ValueError):
            timeout = remaining
        return self.tools.call(tool_name, params, trace_id=session.trace_id, run_id=session.run_id,
                               cancel=session.cancel, timeout_seconds=timeout, attempt=attempt)

    def run(self, task: AgentTask, session: Optional[TaskSession] = None) -> RuntimeResult:
        session = session or self.open_session(task)
        run_id, trace_id, started, budget = session.run_id, session.trace_id, session.started, session.budget
        model = task.model or self.provider.model

        if not isinstance(task.prompt, str) or not task.prompt.strip():
            return self._fail_early(run_id, trace_id, started, 0, "validation_failure", "empty_prompt", False, model)
        if session.cancel is not None and session.cancel.is_set():
            return self._fail_early(run_id, trace_id, started, 0, "user_cancelled", "cancelled", False, model)

        max_attempts = self._allowed_attempts(task)
        per_attempt_timeout = self._per_attempt_timeout(task)

        attempt = 0
        while attempt < max_attempts:
            if self._clock() - started >= budget:
                return self._fail_early(run_id, trace_id, started, attempt, "resource_limit",
                                        "max_runtime_exceeded", False, model)
            attempt += 1
            remaining = budget - (self._clock() - started)
            timeout = max(0.001, min(per_attempt_timeout, remaining))
            internal_cancel = threading.Event()
            request = ProviderRequest(
                prompt=task.prompt,
                model=task.model,
                timeout_seconds=timeout,
                options=task.options,
                keep_alive=task.keep_alive,
                cancel=internal_cancel,
            )
            attempt_started = self._clock()
            try:
                response = self._supervise(session.cancel, internal_cancel, timeout,
                                           lambda: self.provider.execute(request))
            except ProviderError as exc:
                duration_ms = round((self._clock() - attempt_started) * 1000, 3)
                span_id, written = self._record_span(trace_id, run_id, attempt, duration_ms,
                                                     error=exc, model=model)
                if exc.retryable and attempt < max_attempts and (self._clock() - started) < budget:
                    continue
                return self._result(run_id, trace_id, span_id, attempt, exc, model, started, written)
            except Exception as exc:  # never let an unexpected provider bug escape unclassified
                duration_ms = round((self._clock() - attempt_started) * 1000, 3)
                error = ProviderError(type(exc).__name__, category="unknown", retryable=False)
                span_id, written = self._record_span(trace_id, run_id, attempt, duration_ms,
                                                     error=error, model=model)
                return self._result(run_id, trace_id, span_id, attempt, error, model, started, written)
            duration_ms = round((self._clock() - attempt_started) * 1000, 3)
            span_id, written = self._record_span(trace_id, run_id, attempt, duration_ms,
                                                 response=response, model=model)
            return RuntimeResult(
                run_id=run_id,
                trace_id=trace_id,
                span_id=span_id,
                status="succeeded",
                provider=self.provider.name,
                model=response.model or model,
                content=response.content,
                duration_ms=round((self._clock() - started) * 1000, 3),
                attempts=attempt,
                error_category=None,
                error_code=None,
                retryable=False,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                reasoning_tokens=response.reasoning_tokens,
                context_size=response.context_size,
                context_utilization=response.context_utilization,
                finish_reason=response.finish_reason,
                limits_version=self.limits.get("limits_version"),
                span_written=written,
            )

        return self._fail_early(run_id, trace_id, started, attempt, "resource_limit",
                                "attempts_exhausted", False, model)

    def _supervise(
        self,
        caller_cancel: Optional[threading.Event],
        internal_cancel: threading.Event,
        timeout: float,
        invoke: Callable[[], ProviderResponse],
    ) -> ProviderResponse:
        """Run a blocking provider call under a deadline with cooperative cancellation."""
        outcome: dict = {}

        def worker() -> None:
            try:
                outcome["response"] = invoke()
            except BaseException as exc:  # provider errors and bugs both reach the runtime
                outcome["error"] = exc

        thread = threading.Thread(target=worker, name="agent-runtime-provider", daemon=True)
        thread.start()
        started = self._clock()
        while thread.is_alive():
            if caller_cancel is not None and caller_cancel.is_set():
                internal_cancel.set()
                raise ProviderCancelled("cancelled")
            if self._clock() - started >= timeout:
                internal_cancel.set()
                raise ProviderTimeout("timeout", "provider exceeded the configured timeout")
            thread.join(0.02)
        thread.join()
        if "error" in outcome:
            raise outcome["error"]
        if "response" not in outcome:
            raise providers.ProviderResponseError("invalid_response", "provider returned no result")
        return outcome["response"]

    def _allowed_attempts(self, task: AgentTask) -> int:
        configured = self._thresholds().get("max_attempts", 1)
        if isinstance(configured, bool) or not isinstance(configured, int) or configured < 1:
            configured = 1
        requested = task.max_attempts if isinstance(task.max_attempts, int) and not isinstance(task.max_attempts, bool) else 1
        if requested < 1:
            requested = 1
        return min(requested, configured)

    def _per_attempt_timeout(self, task: AgentTask) -> float:
        configured = self._timeouts().get("model_timeout_seconds", 900)
        if isinstance(configured, bool) or not isinstance(configured, (int, float)) or configured <= 0:
            configured = 900
        if task.timeout_seconds is not None:
            try:
                requested = float(task.timeout_seconds)
            except (TypeError, ValueError):
                requested = 0.0
            if requested > 0:
                return min(requested, float(configured))
        return float(configured)

    def _runtime_budget(self, task: AgentTask, per_attempt_timeout: float, max_attempts: int) -> float:
        configured = self._timeouts().get("max_runtime_seconds")
        if isinstance(configured, bool) or not isinstance(configured, (int, float)) or configured <= 0:
            configured = per_attempt_timeout * max_attempts
        budget = float(configured)
        if task.runtime_budget_seconds is not None:
            try:
                requested = float(task.runtime_budget_seconds)
            except (TypeError, ValueError):
                requested = 0.0
            if requested > 0:
                budget = min(budget, requested)
        return budget

    def _timeouts(self) -> dict:
        return self.limits.get("timeouts") or {}

    def _thresholds(self) -> dict:
        return self.limits.get("anomaly_thresholds") or {}

    def _record_span(
        self,
        trace_id: str,
        run_id: str,
        attempt: int,
        duration_ms: float,
        response: Optional[ProviderResponse] = None,
        error: Optional[ProviderError] = None,
        model: Optional[str] = None,
    ) -> tuple:
        span_id = providers.new_span_id()
        record = {
            "schema_version": telemetry.SPAN_SCHEMA_VERSION,
            "trace_id": trace_id,
            "span_id": span_id,
            "timestamp_utc": infra.utc_now(),
            "parent_span_id": None,
            "run_id": run_id,
            "stage": "inference",
            "duration_ms": duration_ms,
            "model": (response.model if response is not None and response.model else model),
            "provider": self.provider.name,
            "role": self.role,
            "attempt": attempt if attempt >= 1 else 1,
            "retry_count": max(0, attempt - 1),
            "input_tokens": response.input_tokens if response is not None else None,
            "output_tokens": response.output_tokens if response is not None else None,
            "cached_input_tokens": None,
            "reasoning_tokens": response.reasoning_tokens if response is not None else None,
            "context_size": response.context_size if response is not None else None,
            "context_utilization": response.context_utilization if response is not None else None,
            "tool_name": None,
            "tool_kind": None,
            "tool_outcome": None,
            "target_hash": None,
            "repeated": None,
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

    def _fail_early(
        self,
        run_id: str,
        trace_id: str,
        started: float,
        attempt: int,
        category: str,
        code: str,
        retryable: bool,
        model: Optional[str],
    ) -> RuntimeResult:
        error = ProviderError(code, category=category, retryable=retryable)
        duration_ms = round((self._clock() - started) * 1000, 3)
        span_id, written = self._record_span(trace_id, run_id, attempt, duration_ms, error=error, model=model)
        return self._result(run_id, trace_id, span_id, attempt, error, model, started, written)

    def _result(
        self,
        run_id: str,
        trace_id: str,
        span_id: str,
        attempt: int,
        error: ProviderError,
        model: Optional[str],
        started: float,
        span_written: bool,
    ) -> RuntimeResult:
        status = "cancelled" if error.category == "user_cancelled" else "failed"
        return RuntimeResult(
            run_id=run_id,
            trace_id=trace_id,
            span_id=span_id,
            status=status,
            provider=self.provider.name,
            model=model,
            content=None,
            duration_ms=round((self._clock() - started) * 1000, 3),
            attempts=attempt,
            error_category=error.category,
            error_code=error.code,
            retryable=error.retryable,
            limits_version=self.limits.get("limits_version"),
            span_written=span_written,
        )
