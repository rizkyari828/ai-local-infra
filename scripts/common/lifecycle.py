"""Automatic orchestration lifecycle hooks V0.1 (stdlib only).

A supported orchestrated execution should record the whole lifecycle — Run, Task,
Attempt, bounded outcome evidence, operational events, cache provenance, and context
metadata — with no manual ``learn start`` / ``learn finish`` bookkeeping. This module
is that boundary: a thin, best-effort wrapper that persists an execution report after
``execution.execute_plan`` already recorded per-node evidence and attempts.

Scope: only executions launched through this orchestration system. It never
intercepts arbitrary OpenCode/Codex chat, scrapes a UI, monitors terminals, installs a
daemon, or hooks provider traffic. Persistence is best-effort: a storage failure never
turns a successful run into a failure, and it never fabricates completion.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import execution  # scripts/common, on sys.path
import orchestrator  # scripts/common, on sys.path
import orchestration_store  # scripts/common, on sys.path
import context_retrieval  # scripts/common, on sys.path


def _machine_id() -> str:
    return os.environ.get("LAI_MACHINE") or "unknown"


def record_run(plan: dict[str, Any], report: dict[str, Any], *, state_dir: Optional[pathlib.Path] = None,
               machine_id: Optional[str] = None, entry_mode: Optional[str] = None,
               controller_ai_calls: Optional[int] = None,
               store: Optional[orchestration_store.OrchestrationStore] = None) -> Optional[str]:
    """Persist the full operational lifecycle + context metadata for one run.

    Returns the run id, or None when persistence was skipped (dry-run) or failed.
    Idempotent through ``persist_run``: re-recording a run duplicates nothing.
    """
    if report.get("dry_run"):
        return report.get("run_id")
    try:
        run_id = orchestration_store.persist_run(
            plan, report, machine_id=machine_id or _machine_id(), state_dir=state_dir,
            entry_mode=entry_mode or report.get("entry_mode"),
            controller_ai_calls=controller_ai_calls, store=store)
        state_root = pathlib.Path(state_dir) if state_dir is not None else infra.STATE_DIR
        for task_id, manifest in (report.get("context_manifests") or {}).items():
            context_retrieval.write_manifest(state_root, run_id, task_id, manifest)
        return run_id
    except (orchestration_store.StoreError, context_retrieval.ContextError, OSError):
        return None


def run_direct(objective: str, *, backend: execution.Backend,
               state_dir: Optional[pathlib.Path] = None, repo_root: Optional[pathlib.Path] = None,
               constraints: Optional[dict[str, Any]] = None, run_id: Optional[str] = None,
               confirm: bool = False, dry_run: bool = False, allow_dirty: bool = False,
               surface: Optional[str] = None, record_evidence: bool = True, cache: bool = True,
               force_refresh: bool = False, max_ai_calls: Optional[int] = None,
               entry_mode: str = "DIRECT_CLI", controller_ai_calls: Optional[int] = None,
               backends: Optional[dict[str, execution.Backend]] = None,
               health: Optional[Any] = None,
               store: Optional[orchestration_store.OrchestrationStore] = None) -> tuple[dict, dict]:
    """Direct local entry path: objective -> deterministic plan -> execute -> record.

    The same engine and safety guardrails as the CLI run path; this only removes the
    need for a chat controller and for manual lifecycle bookkeeping. It preserves
    READ_ONLY/MUTATING intent, hard AI-call budget, TOOL_ONLY security, cache policy,
    context retrieval, availability-aware resolution, failover, outcome learning, and
    operational data.
    """
    constraints = constraints or {}
    plan = orchestrator.decompose(objective, constraints=constraints)
    if not plan["valid"]:
        raise execution.ExecutionError("invalid plan; run 'plan' to inspect diagnostics")
    report = execution.execute_plan(
        plan, backend=backend, run_id=run_id, state_dir=state_dir, repo_root=repo_root,
        confirm=confirm, dry_run=dry_run, allow_dirty=allow_dirty, surface=surface,
        record_evidence=record_evidence, cache=cache, force_refresh=force_refresh,
        max_ai_calls=max_ai_calls, constraints=constraints, backends=backends, health=health,
        entry_mode=entry_mode, controller_ai_calls=controller_ai_calls)
    if not dry_run:
        record_run(plan, report, state_dir=state_dir, entry_mode=entry_mode,
                   controller_ai_calls=controller_ai_calls, store=store)
    return plan, report
