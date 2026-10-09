"""Efficiency telemetry V0.1 (deterministic, read-only, stdlib only).

Answers, from structured operational data: what actually executed, what was
TOOL_ONLY / NO_CHANGE_REQUIRED, what AI work was logically required, what was reused
from the cache, how many real invocations occurred, on which backend/provider/model,
and whether the run started as a chat handoff or a direct local invocation.

Source of truth: ``orchestration.db`` (Operational Data Contract v3) plus bounded
context metadata and bounded result-cache metadata. This module **derives** summaries;
it creates no second execution log and stores nothing. It measures facts, never
hypothetical savings: TOOL_ONLY counts are reported as counts, and the only "avoided
calls" figure exposed is the count of *valid logical AI cache hits* (those tasks were
already classified as AI work).

Telemetry is metadata-only: never prompts, responses, transcripts, tokens, or secrets.
Token/cost/RTK/Headroom measurements are a **separate** dimension and are never merged
here (their savings may overlap with orchestration reuse).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import orchestration_store  # scripts/common, on sys.path

SCHEMA_VERSION = 1


def _state_dir_for(store: orchestration_store.OrchestrationStore,
                   state_dir: Optional[pathlib.Path]) -> pathlib.Path:
    # orchestration.db lives at <state>/orchestration/orchestration.db.
    return pathlib.Path(state_dir) if state_dir is not None else store.path.parent.parent


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _event_counts(events: list[dict[str, Any]], event_type: str) -> int:
    return sum(1 for event in events if event.get("event_type") == event_type)


def _reason_counts(events: list[dict[str, Any]], event_type: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if event.get("event_type") != event_type:
            continue
        reason = _payload(event).get("reason") or "UNKNOWN"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def context_metrics(run_id: str, *, state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Aggregate bounded ContextManifest metrics written by context retrieval. Reads
    metadata only (counts, sizes, fingerprints) — never pack content."""
    base = pathlib.Path(state_dir or infra.STATE_DIR) / "orchestration" / run_id / "context"
    result: dict[str, Any] = {"available": False, "pack_count": 0, "total_chars": 0,
                              "required_items": 0, "optional_items": 0, "optional_items_dropped": 0,
                              "deduplicated_chars": 0, "graphify_fallback_packs": 0, "budget_classes": {}}
    if not base.is_dir():
        return result
    result["available"] = True
    for path in sorted(base.glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        result["pack_count"] += 1
        result["total_chars"] += int(manifest.get("size") or 0)
        result["required_items"] += int(manifest.get("required_count") or 0)
        result["optional_items"] += int(manifest.get("optional_count") or 0)
        result["optional_items_dropped"] += int(manifest.get("omitted_count") or 0)
        result["deduplicated_chars"] += int(manifest.get("duplicate_chars_avoided") or 0)
        if manifest.get("fallback_used") or manifest.get("graphify_status") not in (None, "CURRENT"):
            result["graphify_fallback_packs"] += 1
        budget = manifest.get("context_budget") or "UNKNOWN"
        result["budget_classes"][budget] = result["budget_classes"].get(budget, 0) + 1
    return result


def _model_usage_from_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, dict[str, Any]] = {}
    for attempt in attempts:
        key = (attempt.get("backend"), attempt.get("surface"), attempt.get("provider"),
               attempt.get("model"), attempt.get("required_tier"), attempt.get("required_effort"),
               attempt.get("native_effort"), attempt.get("normalized_effort"), attempt.get("status"),
               attempt.get("failure_category"))
        entry = groups.get(key)
        if entry is None:
            entry = {"backend": attempt.get("backend"), "surface": attempt.get("surface"),
                     "provider": attempt.get("provider"), "model": attempt.get("model"),
                     "required_tier": attempt.get("required_tier"),
                     "required_effort": attempt.get("required_effort"),
                     "native_effort": attempt.get("native_effort"),
                     "normalized_effort": attempt.get("normalized_effort"),
                     "status": attempt.get("status"), "failure_category": attempt.get("failure_category"),
                     "count": 0}
            groups[key] = entry
        entry["count"] += 1
    return sorted(groups.values(), key=lambda row: (row["provider"] or "", row["model"] or "",
                                                    row["required_tier"] or "", row["status"] or ""))


def get_run_efficiency(run_id: str, *, store: Optional[orchestration_store.OrchestrationStore] = None,
                       state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Deterministic per-run efficiency summary derived from operational state."""
    owns = store is None
    store = store or orchestration_store.OrchestrationStore(state_dir=state_dir)
    try:
        run = store.get_run(run_id)
        if run is None:
            raise orchestration_store.StoreError(f"unknown run_id: {run_id}")
        tasks = store.run_tasks(run_id)
        attempts = store.run_attempts(run_id)
        events = store.run_events(run_id)
        root = _state_dir_for(store, state_dir)
    finally:
        if owns:
            store.close()

    total = len(tasks)
    # NO_CHANGE_REQUIRED is a distinct deterministic outcome, reported separately from
    # the generic TOOL_ONLY bucket so the two counts stay disjoint and auditable.
    no_change = sum(1 for t in tasks if t["status"] == "NO_CHANGE_REQUIRED")
    tool_only = sum(1 for t in tasks if t["executor_type"] == "TOOL_ONLY"
                    and t["status"] != "NO_CHANGE_REQUIRED")
    logical_ai = sum(1 for t in tasks if t["executor_type"] == "AI")
    cache_reused_ai = sum(1 for t in tasks if t["executor_type"] == "AI"
                          and t.get("execution_source") == "CACHE")
    fresh_ai = logical_ai - cache_reused_ai
    conditional_ai = sum(1 for t in tasks if t["executor_type"] == "AI" and t.get("conditional"))
    tool_operations: dict[str, int] = {}
    for task in tasks:
        if task["executor_type"] == "TOOL_ONLY" and task.get("tool_operation"):
            op = task["tool_operation"]
            tool_operations[op] = tool_operations.get(op, 0) + 1

    real = len(attempts)
    successful = sum(1 for a in attempts if a["status"] == "SUCCESS")
    failed = real - successful
    retries = sum(1 for a in attempts if a.get("is_retry"))
    fallbacks = sum(1 for a in attempts if a.get("is_fallback"))
    planner_calls = 0  # V0.1 makes no live planner AI calls (decision semantics only)

    miss_reasons = _reason_counts(events, "CACHE_MISS")
    hits = _event_counts(events, "CACHE_HIT")
    hard_max = run.get("hard_max_ai_calls")
    used = run.get("used_ai_calls") or 0
    controller = run.get("controller_ai_calls")
    local_ai = real + planner_calls
    entry_mode = run.get("entry_mode")

    failures: dict[str, int] = {}
    for attempt in attempts:
        if attempt.get("failure_category"):
            failures[attempt["failure_category"]] = failures.get(attempt["failure_category"], 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "project_id": run.get("project_id"),
        "machine_id": run.get("machine_id"),
        "status": run.get("status"),
        "intent": run.get("intent"),
        "objective_summary": run.get("objective_summary"),
        "entry_mode": entry_mode,
        "planner_mode": run.get("planner_mode"),
        "tasks": {"total": total, "tool_only": tool_only, "no_change": no_change,
                  "logical_ai": logical_ai, "cache_reused_ai": cache_reused_ai,
                  "fresh_ai": fresh_ai, "conditional_ai": conditional_ai},
        "ai": {"real_invocation_attempts": real, "successful_invocations": successful,
               "failed_invocations": failed, "planner_calls": planner_calls,
               "retry_calls": retries, "fallback_calls": fallbacks,
               "calls_avoided_by_cache": cache_reused_ai},
        "budget": {"hard_max_ai_calls": hard_max, "planned_ai_calls": run.get("planned_ai_calls"),
                   "minimum_required_ai_calls": run.get("minimum_required_ai_calls"),
                   "used_ai_calls": used,
                   "remaining_ai_call_budget": None if hard_max is None else max(0, hard_max - used)},
        "cache": {"hits": hits, "misses": _event_counts(events, "CACHE_MISS"),
                  "rejected": _event_counts(events, "CACHE_REJECTED"),
                  "written": _event_counts(events, "CACHE_ENTRY_WRITTEN"),
                  "invalid": miss_reasons.get("CACHE_INVALID", 0),
                  "bypassed_by_user": miss_reasons.get("USER_FORCED_REFRESH", 0),
                  "incremental_nodes_reused": hits},
        "failures": {"by_category": failures,
                     "failover_selected": _event_counts(events, "FAILOVER_SELECTED"),
                     "retry_selected": _event_counts(events, "RETRY_SELECTED")},
        "model_usage": _model_usage_from_attempts(attempts),
        "tool_operations": tool_operations,
        "zero_ai": {"local_ai_calls": local_ai, "controller_ai_calls": controller,
                    "zero_additional_ai": local_ai == 0,
                    "true_zero_ai": local_ai == 0 and controller == 0},
        "context": context_metrics(run_id, state_dir=root),
    }


def get_recent_efficiency(*, store: Optional[orchestration_store.OrchestrationStore] = None,
                          state_dir: Optional[pathlib.Path] = None, limit: int = 20,
                          machine_id: Optional[str] = None,
                          project_id: Optional[str] = None) -> dict[str, Any]:
    """Recent runs with a compact efficiency record each, filtered by machine/project.
    Cross-machine UIs can distinguish machines while still recognizing one project."""
    owns = store is None
    store = store or orchestration_store.OrchestrationStore(state_dir=state_dir)
    try:
        rows = store.recent_runs(limit=limit)
        if machine_id is not None:
            rows = [r for r in rows if r["machine_id"] == machine_id]
        if project_id is not None:
            rows = [r for r in rows if r["project_id"] == project_id]
        summaries = [get_run_efficiency(r["run_id"], store=store, state_dir=state_dir) for r in rows]
        root = _state_dir_for(store, state_dir)
    finally:
        if owns:
            store.close()
    totals = {"runs": len(summaries),
              "logical_ai_tasks": sum(s["tasks"]["logical_ai"] for s in summaries),
              "cache_reused_ai_tasks": sum(s["tasks"]["cache_reused_ai"] for s in summaries),
              "real_ai_invocation_attempts": sum(s["ai"]["real_invocation_attempts"] for s in summaries),
              "tool_only_tasks": sum(s["tasks"]["tool_only"] for s in summaries),
              "true_zero_ai_runs": sum(1 for s in summaries if s["zero_ai"]["true_zero_ai"])}
    return {"schema_version": SCHEMA_VERSION, "machine_id": machine_id, "project_id": project_id,
            "state_dir": str(root), "totals": totals,
            "runs": [{"run_id": s["run_id"], "created_at": r.get("created_at"),
                      "machine_id": s["machine_id"], "entry_mode": s["entry_mode"],
                      "status": s["status"], "tasks": s["tasks"], "ai": s["ai"],
                      "zero_ai": s["zero_ai"]} for s, r in zip(summaries, rows)]}


def get_model_usage(*, store: Optional[orchestration_store.OrchestrationStore] = None,
                    state_dir: Optional[pathlib.Path] = None, run_id: Optional[str] = None,
                    machine_id: Optional[str] = None, project_id: Optional[str] = None) -> dict[str, Any]:
    """Grouped real-invocation facts by backend/provider/model/tier/effort/status."""
    owns = store is None
    store = store or orchestration_store.OrchestrationStore(state_dir=state_dir)
    try:
        usage = store.model_usage(run_id, machine_id=machine_id, project_id=project_id)
    finally:
        if owns:
            store.close()
    groups = []
    for row in usage:
        entry = dict(row)
        entry["count"] = entry.pop("n")
        groups.append(entry)
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "machine_id": machine_id,
            "project_id": project_id, "groups": groups,
            "total_invocations": sum(row["count"] for row in groups)}


def get_cache_efficiency(*, store: Optional[orchestration_store.OrchestrationStore] = None,
                         state_dir: Optional[pathlib.Path] = None, run_id: Optional[str] = None,
                         machine_id: Optional[str] = None,
                         project_id: Optional[str] = None) -> dict[str, Any]:
    """Bounded cache telemetry from operational events (bounded reason codes)."""
    owns = store is None
    store = store or orchestration_store.OrchestrationStore(state_dir=state_dir)
    try:
        if run_id is not None:
            run_ids = [run_id]
        else:
            recent = store.recent_runs(limit=200)
            run_ids = [r["run_id"] for r in recent
                       if (machine_id is None or r["machine_id"] == machine_id)
                       and (project_id is None or r["project_id"] == project_id)]
        totals = {"hits": 0, "misses": 0, "rejected": 0, "written": 0, "invalid": 0, "bypassed_by_user": 0}
        for rid in run_ids:
            events = store.run_events(rid)
            totals["hits"] += _event_counts(events, "CACHE_HIT")
            totals["misses"] += _event_counts(events, "CACHE_MISS")
            totals["rejected"] += _event_counts(events, "CACHE_REJECTED")
            totals["written"] += _event_counts(events, "CACHE_ENTRY_WRITTEN")
            totals["invalid"] += _reason_counts(events, "CACHE_MISS").get("CACHE_INVALID", 0)
            totals["bypassed_by_user"] += _reason_counts(events, "CACHE_MISS").get("USER_FORCED_REFRESH", 0)
        totals["incremental_nodes_reused"] = totals["hits"]
        totals["calls_avoided_by_valid_hit"] = totals["hits"]
    finally:
        if owns:
            store.close()
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "machine_id": machine_id,
            "project_id": project_id, "runs_considered": len(run_ids), **totals}


def get_context_efficiency(run_id: str, *, state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Bounded context-retrieval metrics for one run (metadata only)."""
    metrics = context_metrics(run_id, state_dir=state_dir)
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, **metrics}
