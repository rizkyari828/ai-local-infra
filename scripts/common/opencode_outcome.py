#!/usr/bin/env python3
"""Map one OpenCode session into a Task Outcome V1 row (stdlib only).

OpenCode v2.0.x exposes structured session metadata through the CLI
(``opencode api session.get --param sessionID=...``): provider/model/variant,
token counts, OpenCode's own cost, created/updated/idle times, and an execution
``outcome`` whose enum is ``succeeded``/``failed``/``interrupted``.

That outcome describes whether the agent turn finished normally, failed, or was
interrupted. It is *execution* status, not proof that the requested coding task
was solved, so this bridge never sets ``success`` from it: ``success`` stays
``null`` and the execution state is preserved in ``status``. Unknown values stay
``null``. Prompts, titles, messages, and responses are never read or stored.

The task identity is the OpenCode session id, mapped to a deterministic UUIDv5
so it satisfies the existing UUID ``task_id`` contract and dedupes repeated
finalization of the same session. The raw session id is not persisted.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any, Optional

import infra
import telemetry

# OpenCode ``Session.Message.Idle`` outcome enum -> Task Outcome ``status``.
EXECUTION_OUTCOME_STATUS = {
    "succeeded": "succeeded",
    "failed": "failed",
    "interrupted": "cancelled",
}


def _first_str(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _iso_from_ms(value: Any) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    stamp = dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def task_id_for_session(session_id: str) -> str:
    """Derive a stable UUID task id from the OpenCode session id."""
    if not isinstance(session_id, str) or not session_id:
        raise infra.InfraError("OpenCode outcome requires a session id")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "opencode:session:" + session_id))


def select_session(
    sessions: list[dict[str, Any]], directory: str, since_ms: int
) -> Optional[dict[str, Any]]:
    """Return the one session in ``directory`` updated at/after ``since_ms``.

    ``directory`` must already be resolved (``os.path.realpath``). Returns
    ``None`` when no session matches *or when more than one does* (concurrent
    runs), so the caller records nothing rather than attributing an outcome to
    the wrong session.
    """
    if not isinstance(since_ms, int) or since_ms <= 0:
        return None
    matched = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = session.get("id")
        updated = session.get("updated")
        path = session.get("directory")
        if not isinstance(session_id, str) or not session_id:
            continue
        if not isinstance(updated, (int, float)) or isinstance(updated, bool):
            continue
        if updated < since_ms:
            continue
        if not isinstance(path, str) or os.path.realpath(path) != directory:
            continue
        matched.append(session)
    if len(matched) != 1:
        return None
    return matched[0]


class _OpenCodeSessionResult:
    """Minimal runtime-result shape consumed by ``telemetry.build_task_outcome``."""

    def __init__(self, *, trace_id: str, status: str, provider: Optional[str],
                 model: Optional[str], duration_ms: Optional[float],
                 input_tokens: Optional[int], output_tokens: Optional[int],
                 cached_input_tokens: Optional[int], cache_miss_tokens: Optional[int]) -> None:
        self.trace_id = trace_id
        self.run_id = None
        self.status = status
        self.provider = provider
        self.model = model
        self.attempts = None
        self.duration_ms = duration_ms
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_input_tokens = cached_input_tokens
        self.cache_miss_tokens = cache_miss_tokens
        self.error_category = None
        self.error_code = None


def build_outcome(
    session: dict[str, Any],
    *,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build and validate one Task Outcome row from OpenCode session metadata.

    Only the fields listed here are read; ``title`` and any message content are
    ignored. ``success`` is always ``null`` (execution outcome only).
    """
    if not isinstance(session, dict):
        raise infra.InfraError("OpenCode session metadata must be an object")
    resolved_id = _first_str(session_id, session.get("id"))
    if resolved_id is None:
        raise infra.InfraError("OpenCode session metadata has no id")

    model_obj = session.get("model") if isinstance(session.get("model"), dict) else {}
    provider = _first_str(model_obj.get("providerID"), model_obj.get("provider"))
    model = _first_str(model_obj.get("id"), model_obj.get("modelID"))
    variant = _first_str(model_obj.get("variant"))

    tokens = session.get("tokens") if isinstance(session.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    uncached_input = _as_int(tokens.get("input"))
    cached_input = _as_int(cache.get("read"))
    output_tokens = _as_int(tokens.get("output"))
    input_tokens = None
    if uncached_input is not None or cached_input is not None:
        input_tokens = (uncached_input or 0) + (cached_input or 0)

    time_obj = session.get("time") if isinstance(session.get("time"), dict) else {}
    started_ms = time_obj.get("created")
    end_ms = time_obj.get("idle")
    if not isinstance(end_ms, (int, float)) or isinstance(end_ms, bool):
        end_ms = time_obj.get("updated")
    duration_ms = None
    if (isinstance(started_ms, (int, float)) and not isinstance(started_ms, bool)
            and isinstance(end_ms, (int, float)) and not isinstance(end_ms, bool)
            and end_ms >= started_ms):
        duration_ms = float(end_ms - started_ms)

    status = EXECUTION_OUTCOME_STATUS.get(_first_str(session.get("outcome")), "unknown")
    trace_id = task_id_for_session(resolved_id)
    result = _OpenCodeSessionResult(
        trace_id=trace_id,
        status=status,
        provider=provider,
        model=model,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input,
        cache_miss_tokens=uncached_input,
    )
    return telemetry.build_task_outcome(
        result,
        task_id=trace_id,
        started_at=_iso_from_ms(started_ms),
        completed_at=_iso_from_ms(end_ms),
        initial_variant=variant,
        final_variant=variant,
        provider_reported_cost=_as_number(session.get("cost")),
        success_known=False,
    )


__all__ = ["build_outcome", "task_id_for_session", "select_session", "EXECUTION_OUTCOME_STATUS"]


