#!/usr/bin/env python3
"""Bounded Qwen -> DeepSeek escalation policy and controller (stdlib only).

Providers execute requests; this module only decides when another provider is
allowed. v0.1 supports one hop (maximum depth 1) from the local student to the
configured hosted fallback. It is not a router, planner, scorer, or retry
framework.

Design rules:

- Only model-capability failures escalate by default (`model_reasoning_failure`,
  `validation_failure`). Environment, configuration, provider, tool, timeout,
  cancellation, and task/tool resource-limit failures never escalate.
- Escalation is off, manual, and hosted-blocked by default.
- The escalation keeps the original ``trace_id``; the teacher run gets a new
  ``run_id`` and the existing escalation telemetry row links them through
  ``parent_run_id``.
- The remaining task runtime budget and cancellation are inherited, so the
  hosted model never receives a fresh full timeout.
- Observing a failure never promotes a lesson; the existing review/evidence and
  promotion rules are untouched.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

import infra
import learning
import pricing
import providers
import runtime
import telemetry

# Runtime error category -> learning failure taxonomy. Only model-capability
# categories are eligible for escalation; the rest map to an environment or
# unknown observation and are never escalated.
RUNTIME_TO_TAXONOMY = {
    "model_reasoning_failure": "unknown",
    "validation_failure": "invalid_structured_output",
    "provider_failure": "environment_issue",
    "provider_timeout": "environment_issue",
    "tool_failure": "environment_issue",
    "tool_timeout": "environment_issue",
    "environment_failure": "environment_issue",
    "configuration_failure": "environment_issue",
    "resource_limit": "unknown",
    "context_limit": "context_loss",
    "user_cancelled": "unknown",
    "unknown": "unknown",
}

DEFAULT_ELIGIBLE_CATEGORIES = ("model_reasoning_failure", "validation_failure")

VALID_ACTIONS = ("skip", "recommend", "blocked", "escalate")


@dataclass
class EscalationPolicy:
    """Conservative, configurable decision policy. No execution."""

    enabled: bool = False
    mode: str = "manual"
    hosted_allowed: bool = False
    fallback_provider: str = "deepseek"
    eligible_categories: tuple = DEFAULT_ELIGIBLE_CATEGORIES
    max_depth: int = providers.MAX_ESCALATION_DEPTH

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "EscalationPolicy":
        section = (config or {}).get("escalation") or {}
        eligible = section.get("eligible_categories") or DEFAULT_ELIGIBLE_CATEGORIES
        return cls(
            enabled=bool(section.get("enabled", False)),
            mode=section.get("mode", "manual"),
            hosted_allowed=bool(section.get("hosted_allowed", False)),
            fallback_provider=section.get("fallback_provider", "deepseek"),
            eligible_categories=tuple(eligible),
            max_depth=int(section.get("max_depth", providers.MAX_ESCALATION_DEPTH)),
        )

    def eligible(self, category: Optional[str]) -> bool:
        return category in self.eligible_categories

    def evaluate(self, student_result: Optional[Any], session: Optional[Any]) -> "EscalationDecision":
        category = getattr(student_result, "error_category", None)
        if student_result is None or getattr(student_result, "status", None) == "succeeded":
            return EscalationDecision(False, "skip", "student_succeeded", self.fallback_provider, category)
        eligible = self.eligible(category)
        if not self.enabled:
            return EscalationDecision(eligible, "skip", "escalation_disabled", self.fallback_provider, category)
        if not eligible:
            return EscalationDecision(False, "skip", "category_not_eligible", self.fallback_provider, category)
        if not self.hosted_allowed:
            return EscalationDecision(True, "blocked", "hosted_blocked", self.fallback_provider, category)
        if session is None or getattr(session, "escalation_depth", 0) >= self.max_depth:
            return EscalationDecision(True, "skip", "depth_exceeded", self.fallback_provider, category)
        if getattr(session, "cancel", None) is not None and session.cancel.is_set():
            return EscalationDecision(True, "skip", "cancelled", self.fallback_provider, category)
        if self.mode == "manual":
            return EscalationDecision(True, "recommend", "manual_mode", self.fallback_provider, category)
        return EscalationDecision(True, "escalate", "eligible", self.fallback_provider, category)


@dataclass
class EscalationDecision:
    eligible: bool
    action: str
    reason: str
    fallback_provider: str
    failure_category: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "action": self.action,
            "reason": self.reason,
            "fallback_provider": self.fallback_provider,
            "failure_category": self.failure_category,
        }


@dataclass
class EscalationOutcome:
    """Structured result of evaluating (and possibly running) an escalation."""

    action: str
    reason: str
    eligible: bool
    escalated: bool
    trace_id: str
    student_run_id: str
    fallback_provider: Optional[str] = None
    teacher_run_id: Optional[str] = None
    teacher_result: Optional[Any] = None
    telemetry_written: bool = False

    def as_dict(self) -> dict:
        teacher = self.teacher_result
        return {
            "action": self.action,
            "reason": self.reason,
            "eligible": self.eligible,
            "escalated": self.escalated,
            "trace_id": self.trace_id,
            "student_run_id": self.student_run_id,
            "fallback_provider": self.fallback_provider,
            "teacher_run_id": self.teacher_run_id,
            "teacher_status": getattr(teacher, "status", None),
            "teacher_error_category": getattr(teacher, "error_category", None),
            "teacher_model": getattr(teacher, "model", None),
            "teacher_duration_ms": getattr(teacher, "duration_ms", None),
            "teacher_input_tokens": getattr(teacher, "input_tokens", None),
            "teacher_output_tokens": getattr(teacher, "output_tokens", None),
            "teacher_cached_input_tokens": getattr(teacher, "cached_input_tokens", None),
            "teacher_cache_miss_tokens": getattr(teacher, "cache_miss_tokens", None),
            "telemetry_written": self.telemetry_written,
        }


class EscalationController:
    """Decide and run at most one hosted escalation for a failed task."""

    def __init__(
        self,
        policy: EscalationPolicy,
        fallback_provider: providers.ModelProvider,
        limits: Optional[dict] = None,
        span_output: Optional[Any] = None,
        escalation_output: Optional[Any] = None,
        role: str = "teacher-cheap",
        record_spans: bool = True,
        record_telemetry: bool = True,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if not isinstance(fallback_provider, providers.ModelProvider):
            raise providers.ProviderConfigurationError("invalid_provider", "escalation requires a ModelProvider")
        self.policy = policy
        self.fallback_provider = fallback_provider
        self.limits = limits if limits is not None else telemetry.load_limits()
        self.span_output = infra.ROOT / "telemetry/spans/runs.jsonl" if span_output is None else span_output
        self.escalation_output = (infra.ROOT / "telemetry/escalations/runs.jsonl"
                                  if escalation_output is None else escalation_output)
        self.role = role
        self.record_spans = record_spans
        self.record_telemetry = record_telemetry
        self._clock = clock or time.perf_counter

    @classmethod
    def from_config(
        cls,
        config: Optional[dict] = None,
        fallback_provider: Optional[providers.ModelProvider] = None,
        limits: Optional[dict] = None,
        span_output: Optional[Any] = None,
        escalation_output: Optional[Any] = None,
        role: str = "teacher-cheap",
        record_spans: bool = True,
        record_telemetry: bool = True,
        clock: Optional[Callable[[], float]] = None,
    ) -> "EscalationController":
        runtime_config = config if config is not None else providers.load_runtime_config()
        policy = EscalationPolicy.from_config(runtime_config)
        provider = fallback_provider if fallback_provider is not None else \
            providers.build_provider(runtime_config, policy.fallback_provider)
        return cls(policy, provider, limits=limits, span_output=span_output,
                   escalation_output=escalation_output, role=role,
                   record_spans=record_spans, record_telemetry=record_telemetry, clock=clock)

    def describe(self) -> dict:
        return {
            "policy": {
                "enabled": self.policy.enabled,
                "mode": self.policy.mode,
                "hosted_allowed": self.policy.hosted_allowed,
                "fallback_provider": self.policy.fallback_provider,
                "eligible_categories": list(self.policy.eligible_categories),
                "max_depth": self.policy.max_depth,
            },
            "fallback": self.fallback_provider.describe(),
        }

    def maybe_escalate(
        self,
        session: runtime.TaskSession,
        task: runtime.AgentTask,
        student_result: runtime.RuntimeResult,
        task_class: str = "general",
    ) -> EscalationOutcome:
        decision = self.policy.evaluate(student_result, session)
        if decision.action != "escalate":
            return self._outcome(decision, session, escalated=False)
        if session.remaining(self._clock) <= 0:
            return self._outcome(EscalationDecision(True, "skip", "budget_exhausted",
                                                    decision.fallback_provider, decision.failure_category),
                                 session, escalated=False)
        if not _hosted_request_safe(task.prompt):
            return self._outcome(EscalationDecision(True, "blocked", "hosted_secret_detected",
                                                    decision.fallback_provider, decision.failure_category),
                                 session, escalated=False)

        child = session.child(role=self.role)
        teacher_agent = runtime.AgentRuntime(
            self.fallback_provider,
            limits=self.limits,
            span_output=self.span_output,
            role=self.role,
            record_spans=self.record_spans,
            clock=self._clock,
        )
        teacher_task = runtime.AgentTask(
            prompt=task.prompt,
            trace_id=session.trace_id,
            run_id=child.run_id,
            role=self.role,
            timeout_seconds=task.timeout_seconds,
            options=task.options,
            cancel=session.cancel,
            parent_span_id=student_result.span_id,
        )
        teacher_result = teacher_agent.run(teacher_task, session=child)
        written = self._record_escalation(student_result, teacher_result, session, child, task_class)
        return EscalationOutcome(
            action="escalate",
            reason="escalated",
            eligible=True,
            escalated=True,
            trace_id=session.trace_id,
            student_run_id=session.run_id,
            fallback_provider=self.fallback_provider.name,
            teacher_run_id=child.run_id,
            teacher_result=teacher_result,
            telemetry_written=written,
        )

    def _outcome(self, decision: EscalationDecision, session: runtime.TaskSession, escalated: bool) -> EscalationOutcome:
        return EscalationOutcome(
            action=decision.action,
            reason=decision.reason,
            eligible=decision.eligible,
            escalated=escalated,
            trace_id=session.trace_id,
            student_run_id=session.run_id,
            fallback_provider=decision.fallback_provider,
        )

    def _record_escalation(
        self,
        student_result: runtime.RuntimeResult,
        teacher_result: runtime.RuntimeResult,
        session: runtime.TaskSession,
        child: runtime.TaskSession,
        task_class: str,
    ) -> bool:
        if not self.record_telemetry:
            return False
        cost = pricing.estimate_usage_cost(
            self.fallback_provider.name,
            teacher_result.model or self.fallback_provider.model,
            input_tokens=teacher_result.input_tokens,
            cached_input_tokens=teacher_result.cached_input_tokens,
            cache_miss_tokens=teacher_result.cache_miss_tokens,
            output_tokens=teacher_result.output_tokens,
        )
        record = {
            "schema_version": "1.1.0",
            "run_id": child.run_id,
            "event_id": str(uuid.uuid4()),
            "timestamp_utc": infra.utc_now(),
            "parent_run_id": session.run_id,
            "trace_id": session.trace_id,
            "task_class": task_class or "general",
            "student_role": "student",
            "student_model": student_result.model or "unknown",
            "student_profile": None,
            "student_outcome": "failed",
            "failure_category": RUNTIME_TO_TAXONOMY.get(student_result.error_category, "unknown"),
            "teacher_provider": self.fallback_provider.name,
            "teacher_model": teacher_result.model or self.fallback_provider.model or "unknown",
            "teacher_reason": "failure",
            "teacher_outcome": "unknown",
            "human_review_outcome": "unreviewed",
            "correction_required": None,
            "human_correction_count": None,
            "tests_passed": None,
            "lesson_candidate": False,
            "eval_candidate": False,
            "training_candidate": False,
            "duration_ms": teacher_result.duration_ms,
            "cost_input_tokens": teacher_result.input_tokens,
            "cost_output_tokens": teacher_result.output_tokens,
            "cost_cache_hit_tokens": teacher_result.cached_input_tokens,
            "estimated_cost_usd": cost["estimated_total_cost"],
            "pricing_source": cost["pricing_profile"],
            "recorded_by": "escalation-controller",
            "sanitized_note": None,
        }
        try:
            learning.validate_escalation_record(record)
            infra.append_jsonl(self.escalation_output, record)
        except (infra.InfraError, OSError):
            return False
        return True


def _hosted_request_safe(prompt: Any) -> bool:
    """Reuse the existing secret guard; do not send credential-like text to a host."""
    return isinstance(prompt, str) and bool(prompt.strip()) and not learning.contains_secret_like(prompt)
