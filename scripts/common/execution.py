"""Multi-model task execution V0.1 (sequential, plan-gated, standard library only).

Executes a validated task graph node-by-node in dependency order (never in
parallel), through a provider-agnostic backend adapter. Context per node is the
minimum sufficient manifest, worker results are compact, and only meaningful
executed nodes produce one bounded outcome-evidence row. Planning and dry-run are
local; real backends are used only with an explicit ``--confirm`` gate.

No UI automation, no nested orchestrators, no auto commit/push/reset, no parallel
workers. Provider-specific invocation lives only inside a bounded adapter.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import uuid
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import routing  # scripts/common, on sys.path
import discovery  # scripts/common, on sys.path
import failover  # scripts/common, on sys.path
import deterministic  # scripts/common, on sys.path
import context_retrieval  # scripts/common, on sys.path
import derived_knowledge as knowledge  # scripts/common, on sys.path
import evidence  # scripts/common, on sys.path

ROOT = infra.ROOT
INFRA_CATEGORIES = ("PROVIDER_503", "RATE_LIMIT", "TIMEOUT", "STALE_SESSION", "AUTH_FAILURE",
                    "SAFETY_GUARD", "CONFIG_DRIFT")
REASONING = "REASONING_INSUFFICIENT"
IMPLEMENTATION = "IMPLEMENTATION_ERROR"
NEXT_TIER = {"FAST": "BALANCED", "BALANCED": "STRONG", "STRONG": "MAX", "MAX": None}
SUCCESS, FAILURE, SKIPPED, BLOCKED, NOT_RUN, CANCELLED = "SUCCESS", "FAILURE", "SKIPPED", "BLOCKED", "NOT_RUN", "CANCELLED"

WORKER_ISOLATION_NOTE = ("Execute only this node's objective. Do not re-plan the master "
                         "objective, do not spawn nested orchestrators or sub-agents, and return "
                         "a concise structured result.")


class ExecutionError(RuntimeError):
    pass


class Backend:
    name = "abstract"
    mode = "UNSUPPORTED"

    def capabilities(self) -> dict[str, Any]:
        return {"name": self.name, "mode": self.mode, "non_interactive": False,
                "can_select_model": False, "can_select_effort": False, "outputs": "none"}

    def available(self) -> bool:
        return False

    def resolve(self, node: dict[str, Any], provider: Optional[str], model: Optional[str],
                effort: str) -> dict[str, Any]:
        return {"provider": provider, "model": model, "native_variant": effort,
                "mode": self.mode, "available": self.available()}

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class FakeBackend(Backend):
    """Deterministic, offline backend for tests and safe acceptance (no AI call)."""
    name = "fake"
    mode = "SIMULATED"

    def __init__(self, script: Optional[dict[str, list[dict[str, Any]]]] = None,
                 default: Optional[dict[str, Any]] = None, cancel_on: Optional[str] = None):
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self.default = default
        self.cancel_on = cancel_on
        self.calls: list[dict[str, Any]] = []

    def available(self) -> bool:
        return True

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"execution_id": request["execution_id"], "task_id": request["task_id"],
                           "tier": request["tier"], "provider": request["provider"],
                           "model": request["model"], "native_variant": request["native_variant"],
                           "dependency_tasks": [d["task_id"] for d in request["dependency_summaries"]],
                           "prompt": request["prompt"]})
        if self.cancel_on == request["task_id"]:
            raise KeyboardInterrupt()
        queue = self.script.get(request["task_id"])
        outcome = queue.pop(0) if queue else (self.default or {
            "status": SUCCESS, "summary": f"{request['task_id']} completed",
            "acceptance_results": {c: True for c in request["acceptance_criteria"]},
            "files_changed": [], "decisions": [], "new_risk_signals": []})
        result = dict(outcome)
        result.setdefault("task_id", request["task_id"])
        result.setdefault("status", SUCCESS)
        if result.get("status") == SUCCESS:
            result.setdefault("completion_signal", "agent_reported")
        result["provider"] = request["provider"]
        result["model"] = request["model"]
        return result


class CliBackend(Backend):
    """Real local CLI backend. Invoked only with an explicit --confirm gate."""
    mode = "SUPPORTED"

    def __init__(self, name: str, binary: str, timeout: int = 300):
        self.name = name
        self.binary = binary
        self.timeout = timeout

    def capabilities(self) -> dict[str, Any]:
        return {"name": self.name, "mode": self.mode, "non_interactive": True,
                "can_select_model": True, "can_select_effort": True, "outputs": "json/text"}

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def resolve(self, node, provider, model, effort):
        native = (node["routing"]["effort"] or "").lower()
        return {"provider": provider, "model": model, "native_variant": native,
                "mode": self.mode, "available": self.available()}

    def _command(self, request: dict[str, Any]) -> list[str]:
        prompt = request["prompt"]
        if self.name == "opencode":
            command = [self.binary, "run", "--format", "json", "--dir", request["working_dir"]]
            if request["model"]:
                model_arg = f"{request['provider']}/{request['model']}" if request["provider"] else request["model"]
                command += ["--model", model_arg]
            if request["native_variant"]:
                command += ["--variant", request["native_variant"]]
            return command + [prompt]
        if self.name == "codex":
            command = [self.binary, "exec"]
            if request.get("read_only"):
                command += ["-s", "read-only"]  # verified flag: codex exec --sandbox read-only
            if request["model"]:
                command += ["-m", request["model"]]
            if request["native_variant"]:
                command += ["-c", f"model_reasoning_effort={request['native_variant']}"]
            return command + [prompt]
        raise ExecutionError(f"unknown CLI backend {self.name}")

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            completed = subprocess.run(self._command(request), cwd=request["working_dir"],
                                       capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"task_id": request["task_id"], "status": FAILURE, "failure_category": "TIMEOUT"}
        except OSError as exc:
            return {"task_id": request["task_id"], "status": FAILURE, "failure_category": "UNKNOWN_FAILURE",
                    "summary": f"backend error: {type(exc).__name__}"}
        category = classify_failure(completed.returncode, completed.stderr)
        if completed.returncode == 0 and not category:
            output = (completed.stdout or "").strip()
            if not output:
                return {"task_id": request["task_id"], "status": "UNKNOWN",
                        "summary": "backend produced no output"}
            return {"task_id": request["task_id"], "status": SUCCESS,
                    "completion_signal": "agent_reported", "summary": output[-400:],
                    "acceptance_results": {}, "files_changed": [], "decisions": [], "new_risk_signals": []}
        return {"task_id": request["task_id"], "status": FAILURE, "failure_category": category or "UNKNOWN_FAILURE",
                "summary": (completed.stderr or "")[-400:], "acceptance_results": {},
                "files_changed": [], "decisions": [], "new_risk_signals": []}


def classify_failure(returncode: int, stderr: str = "") -> Optional[str]:
    text = (stderr or "").lower()
    if returncode == 0:
        return None
    if "503" in text or "service unavailable" in text:
        return "PROVIDER_503"
    if "rate limit" in text or "429" in text:
        return "RATE_LIMIT"
    if "timeout" in text or "timed out" in text:
        return "TIMEOUT"
    if "permission" in text or "approval" in text or "approve" in text:
        return "PERMISSION_REQUIRED"
    if "401" in text or "403" in text or "unauthorized" in text or "auth" in text:
        return "AUTH_FAILURE"
    if "not supported" in text or "unsupported model" in text:
        return "ACCOUNT_MODEL_UNSUPPORTED"
    if "session" in text and "stale" in text:
        return "STALE_SESSION"
    return "UNKNOWN_FAILURE"


def backend_for(name: str, *, timeout: int = 300) -> Backend:
    name = (name or "auto").lower()
    if name == "fake":
        return FakeBackend()
    if name in ("opencode", "codex"):
        return CliBackend(name, name, timeout=timeout)
    if name == "auto":
        return CliBackend("opencode", "opencode", timeout=timeout) if shutil.which("opencode") \
            else CliBackend("codex", "codex", timeout=timeout)
    raise ExecutionError(f"unknown backend: {name}")


# --------------------------------------------------------------------------- #
# Resolution + planning of calls
# --------------------------------------------------------------------------- #


def resolve_plan(plan: dict[str, Any], backend: Backend, surface: Optional[str] = None, *,
                 health: Optional[failover.Health] = None,
                 constraints: Optional[dict[str, Any]] = None,
                 backends: Optional[dict[str, Backend]] = None) -> list[dict[str, Any]]:
    resolutions = []
    for node in plan["nodes"]:
        if node.get("executor") == "TOOL_ONLY":
            resolutions.append({"task_id": node["task_id"], "executor": "TOOL_ONLY", "tier": None,
                                "runnable": True, "candidates": None})
            continue
        tier = node["routing"]["tier"]
        effort = node["routing"]["effort"]
        if health is None:
            resolution = routing.resolve_model(tier, routing.load_policy())
            resolved = backend.resolve(node, resolution["provider"], resolution["model"], effort)
            capability = None
            if surface:
                capability = discovery.capability(resolution["provider"], resolution["model"], surface)["status"]
            resolved.update({"task_id": node["task_id"], "tier": tier, "capability_status": capability,
                             "runnable": bool(backend.available() and resolution["model"]) or backend.name == "fake"})
            resolutions.append(resolved)
            continue
        # Runtime-availability-aware, capability-preserving candidate resolution.
        specs = failover.candidates_for_tier(tier)
        candidates = failover.resolve(tier, effort, specs, health, constraints=constraints)
        selected = failover.select(candidates)
        runnable = bool(selected and (backends is None or selected["backend"] in backends))
        resolutions.append({
            "task_id": node["task_id"], "tier": tier, "effort": effort, "candidates": candidates,
            "backend": selected["backend"] if selected else None,
            "provider": selected["provider"] if selected else None,
            "model": selected.get("model") if selected else None,
            "native_variant": selected.get("native_variant") if selected else None,
            "capability_status": selected["status"] if selected else None,
            "runnable": runnable,
            "reason": None if runnable else "NO_CAPABILITY_PRESERVING_FALLBACK",
        })
    return resolutions


def call_bounds(plan: dict[str, Any], *, infra_retries: int = 1, same_tier_retries: int = 1,
                escalation_depth: int = 2) -> dict[str, int]:
    nodes = plan["nodes"]
    conditional = sum(1 for node in nodes if node["conditional"])
    per_node = 1 + infra_retries + same_tier_retries + escalation_depth
    return {"planned_ai_nodes": len(nodes), "conditional_nodes": conditional,
            "max_retry_and_escalation_calls": per_node, "max_total_calls": len(nodes) * per_node,
            "parallel_workers": 0}


# --------------------------------------------------------------------------- #
# Repository safety
# --------------------------------------------------------------------------- #


def _git(root: pathlib.Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def repo_state(root: pathlib.Path) -> dict[str, Any]:
    return {"root": str(root), "head": _git(root, "rev-parse", "HEAD"),
            "dirty": bool(_git(root, "status", "--porcelain"))}


def _worktree_paths(root: pathlib.Path) -> set[str]:
    """Tracked or untracked working-tree changes (paths only)."""
    status = _git(root, "status", "--porcelain", "--untracked-files=normal")
    if not status:
        return set()
    return {line[3:].strip() for line in status.splitlines() if line.strip()}


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def execute_plan(plan: dict[str, Any], *, backend: Backend, run_id: Optional[str] = None,
                 state_dir: Optional[pathlib.Path] = None, repo_root: Optional[pathlib.Path] = None,
                 confirm: bool = False, dry_run: bool = False, allow_dirty: bool = False,
                 surface: Optional[str] = None, record_evidence: bool = True,
                 infra_retries: int = 1, same_tier_retries: int = 1, resume: bool = False,
                 read_only: bool = False, max_ai_calls: Optional[int] = None,
                 backends: Optional[dict[str, Backend]] = None,
                 health: Optional[failover.Health] = None,
                 constraints: Optional[dict[str, Any]] = None,
                 tool_executor: Optional[deterministic.ToolExecutor] = None) -> dict[str, Any]:
    if not plan.get("valid"):
        raise ExecutionError("refusing to execute an invalid plan")
    if not confirm and not dry_run:
        raise ExecutionError("execution requires --confirm (or use --dry-run)")
    repo_root = pathlib.Path(repo_root or ROOT)
    tool_executor = tool_executor or deterministic.ToolExecutor(root=repo_root)
    state = repo_state(repo_root)
    enforce_read_only = read_only or plan.get("intent") == "READ_ONLY"
    backend_registry: dict[str, Backend] = dict(backends or {})
    if backend is not None:
        backend_registry.setdefault(backend.name, backend)
    bindings = resolve_plan(plan, backend, surface, health=health, constraints=constraints,
                            backends=backend_registry)
    bounds = call_bounds(plan, infra_retries=infra_retries, same_tier_retries=same_tier_retries)

    report: dict[str, Any] = {"run_id": run_id, "backend": backend.name, "dry_run": dry_run,
                              "intent": plan.get("intent"), "bounds": bounds, "resolutions": bindings,
                              "nodes": {}, "head": state["head"], "dirty": state["dirty"],
                              "max_ai_calls": max_ai_calls, "calls_used": 0, "remaining_calls": None}
    if enforce_read_only:
        conflicts = [f"READ_ONLY_MUTATION_CONFLICT: {node['task_id']} is a mutating node under READ_ONLY intent"
                     for node in plan["nodes"] if node.get("intent") == "MUTATING"]
        if conflicts:
            report["error"] = conflicts[0]
            report["diagnostics"] = conflicts
            return report
    if any(not binding["runnable"] for binding in bindings):
        reason = next((binding.get("reason") for binding in bindings if not binding["runnable"]), None)
        report["error"] = reason or "NO_COMPATIBLE_EXECUTION_BACKEND"
        report["nodes"] = {b["task_id"]: {"status": report["error"], "required_tier": b["tier"]}
                           for b in bindings if not b["runnable"]}
        return report
    if dry_run:
        report["remaining_calls"] = max_ai_calls
        return report
    if state["dirty"] and not allow_dirty:
        report["error"] = "working tree is dirty; commit/stash or pass --allow-dirty"
        return report

    run_id = run_id or uuid.uuid4().hex[:12]
    report["run_id"] = run_id
    run_dir = pathlib.Path(state_dir or infra.STATE_DIR) / "orchestration" / run_id
    previous = _load_state(run_dir) if resume else None
    if resume and previous:
        if previous.get("head") != state["head"] or previous.get("repo") != state["root"]:
            report["error"] = "REFUSE_RESUME: repository HEAD/identity changed; refusing to reuse stale results"
            return report
    results: dict[str, dict[str, Any]] = dict(previous["nodes"]) if (resume and previous) else {}
    # Hard budget: resume never resets the counter; take the smaller of persisted/requested ceiling.
    persisted_max = (previous or {}).get("hard_max_ai_calls")
    if resume and persisted_max is not None:
        budget = {"max": persisted_max if max_ai_calls is None else min(persisted_max, max_ai_calls),
                  "used": previous.get("calls_used", 0)}
    else:
        budget = {"max": max_ai_calls, "used": 0}
    report["max_ai_calls"] = budget["max"]
    resolutions = {b["task_id"]: b for b in bindings}

    attempts: list[dict[str, Any]] = []
    context_manifests: dict[str, dict[str, Any]] = {}
    for task_id in plan["order"]:
        node = next(item for item in plan["nodes"] if item["task_id"] == task_id)
        if resume and results.get(task_id, {}).get("status") in (SUCCESS, "NO_CHANGE_REQUIRED"):
            continue
        deps = node["depends_on"]
        if any(results.get(dep, {}).get("status") not in (SUCCESS, SKIPPED, "NO_CHANGE_REQUIRED") for dep in deps):
            results[task_id] = {"status": BLOCKED, "task_id": task_id}
            continue
        if node["conditional"] and not _conditional_active(node, deps, results):
            results[task_id] = {"status": SKIPPED, "task_id": task_id}
            continue
        if node.get("executor") == "TOOL_ONLY":
            results[task_id] = _run_tool_node(node, deps, results, tool_executor,
                                              state_dir=state_dir, record_evidence=record_evidence)
            if results[task_id]["status"] in ("AI_REQUIRED", FAILURE):
                report["error"] = results[task_id].get("failure_category") or "TOOL_EXECUTION_FAILED"
                _mark_not_run(plan, task_id, results)
                break
            _save_state(run_dir, _state_blob(run_id, state, results, budget))
            continue
        try:
            results[task_id] = _run_node(node, deps, results, backend, resolutions[task_id],
                                         state_dir=state_dir, record_evidence=record_evidence,
                                         infra_retries=infra_retries, same_tier_retries=same_tier_retries,
                                         surface=backend.name, budget=budget,
                                         enforce_read_only=enforce_read_only, repo_root=repo_root,
                                         candidates=resolutions[task_id].get("candidates"),
                                         backends=backend_registry, health=health,
                                         constraints=constraints, attempts=attempts,
                                         context_manifests=context_manifests)
        except KeyboardInterrupt:
            results[task_id] = {"status": CANCELLED, "task_id": task_id}
            _mark_not_run(plan, task_id, results)
            break
        if results[task_id]["status"] in ("CALL_BUDGET_EXHAUSTED", "READ_ONLY_VIOLATION"):
            report["error"] = results[task_id]["status"]
            _mark_not_run(plan, task_id, results)
            break
        _save_state(run_dir, _state_blob(run_id, state, results, budget))
    report["nodes"] = results
    report["attempts"] = attempts
    report["context_manifests"] = context_manifests
    report["calls_used"] = budget["used"]
    report["remaining_calls"] = (budget["max"] - budget["used"]) if budget["max"] is not None else None
    report["final"] = _final_summary(plan, results)
    _save_state(run_dir, _state_blob(run_id, state, results, budget))
    return report


def _mark_not_run(plan: dict[str, Any], current: str, results: dict[str, dict[str, Any]]) -> None:
    index = plan["order"].index(current)
    for future in plan["order"][index + 1:]:
        results.setdefault(future, {"status": NOT_RUN, "task_id": future})


def _state_blob(run_id: str, state: dict[str, Any], results: dict[str, dict[str, Any]],
                budget: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": run_id, "head": state["head"], "repo": state["root"], "nodes": results,
            "calls_used": budget["used"], "hard_max_ai_calls": budget["max"]}


def _conditional_active(node: dict[str, Any], deps: list[str], results: dict[str, dict[str, Any]]) -> bool:
    if not node["conditional"]:
        return True
    for dep in deps:
        outcome = results.get(dep, {})
        if outcome.get("new_risk_signals") or outcome.get("failure_category") in (REASONING, IMPLEMENTATION):
            return True
    return False


def _run_node(node, deps, results, backend, resolution, *, state_dir, record_evidence,
              infra_retries, same_tier_retries, surface, budget, enforce_read_only,
              repo_root, candidates=None, backends=None, health=None,
              constraints=None, attempts=None, context_manifests=None) -> dict[str, Any]:
    tier = node["routing"]["tier"]
    initial_tier = tier
    if enforce_read_only and node.get("intent") == "MUTATING":
        return {"task_id": node["task_id"], "status": "READ_ONLY_VIOLATION",
                "failure_category": "READ_ONLY_VIOLATION",
                "summary": "mutating node refused under READ_ONLY intent",
                "initial_tier": initial_tier, "final_tier": tier, "attempts": 0, "escalated": False}
    if budget["max"] is not None and budget["used"] >= budget["max"]:
        return {"task_id": node["task_id"], "status": "CALL_BUDGET_EXHAUSTED",
                "failure_category": "CALL_BUDGET_EXHAUSTED",
                "summary": "hard AI-call budget exhausted before launch",
                "initial_tier": initial_tier, "final_tier": tier, "attempts": 0, "escalated": False}
    registry: dict[str, Backend] = dict(backends or {})
    if backend is not None:
        registry.setdefault(backend.name, backend)
    candidate = {"backend": (resolution or {}).get("backend") or backend.name,
                 "provider": (resolution or {}).get("provider"),
                 "model": (resolution or {}).get("model"),
                 "native_variant": (resolution or {}).get("native_variant"),
                 "tier": tier, "effort": node["routing"]["effort"]}
    current_backend = registry.get(candidate["backend"], backend)
    first_backend = candidate["backend"]
    dependency_summaries = [{"task_id": dep, "summary": results.get(dep, {}).get("summary", "")}
                            for dep in deps]
    attempt = 0
    infra_used = same_used = escalations = 0
    execution_id = uuid.uuid4().hex
    before = _worktree_paths(repo_root) if enforce_read_only else None
    native = candidate["native_variant"]
    normalized = None
    if native:
        try:
            normalized = discovery.normalize_effort(candidate["provider"], native, discovery.load_config())
        except discovery.DiscoveryError:
            normalized = None
    if isinstance(resolution, dict):
        resolution = {**resolution, "normalized_effort": normalized, "native_effort": native}
    decision_id = evidence.start(task_family=node["task_family"], task_class=node["task_class"],
                                 surface=candidate["backend"], provider=candidate["provider"],
                                 model=candidate["model"], native_variant=native,
                                 normalized_effort=normalized,
                                 initial_tier=initial_tier,
                                 initial_effort=node["routing"]["effort"], state_dir=state_dir) \
        if record_evidence else execution_id
    while True:
        if budget["max"] is not None and budget["used"] >= budget["max"]:
            return _finalize(node, {"summary": "hard AI-call budget exhausted"}, FAILURE,
                             "CALL_BUDGET_EXHAUSTED", initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence)
        attempt += 1
        budget["used"] += 1
        res = {"provider": candidate["provider"], "model": candidate["model"],
               "native_variant": candidate["native_variant"]}
        request = _request(node, res, dependency_summaries, execution_id, tier, current_backend,
                           read_only=node.get("intent") == "READ_ONLY")
        try:
            ctx_request = context_retrieval.ContextRequest.build(
                task_id=node["task_id"], task_family=node.get("task_family"),
                task_class=node.get("task_class"), executor_type=node.get("executor", "AI"),
                context_budget=(node.get("context") or {}).get("budget", "SMALL"),
                objective_summary=node["objective_summary"],
                acceptance_criteria=node.get("acceptance_criteria", []),
                dependency_facts=dependency_summaries,
                source_refs=node.get("context_refs") or [], symbol_refs=node.get("symbol_refs") or [],
                config_refs=node.get("config_refs") or [], knowledge_refs=node.get("knowledge_refs") or [],
                repository={"head": (resolution or {}).get("head")})
            context_result = context_retrieval.build_pack(ctx_request, root=repo_root)
        except (context_retrieval.ContextError, OSError):
            context_result = None
        if context_result is not None:
            if context_manifests is not None:
                context_manifests[node["task_id"]] = context_result["manifest"]
            if context_result["status"] != context_retrieval.STATUS_READY:
                return _finalize(node, {"summary": f"context {context_result['status']}"}, FAILURE,
                                 context_result["status"], initial_tier, tier, attempt, escalations,
                                 decision_id, resolution, state_dir, record_evidence)
            request["context_pack"] = context_result["pack"]
            request["context_manifest"] = context_result["manifest"]
        result = current_backend.execute(request)
        if enforce_read_only:
            changed = sorted(_worktree_paths(repo_root) - (before or set()))
            if changed:
                return _finalize(node, {"summary": "read-only violation: " + ", ".join(changed[:5])},
                                 "READ_ONLY_VIOLATION", "READ_ONLY_VIOLATION", initial_tier, tier,
                                 attempt, escalations, decision_id, resolution, state_dir, record_evidence)
        category = result.get("failure_category")
        success = _is_success(node, result)
        if attempts is not None:
            attempts.append({
                "task_id": node["task_id"], "attempt_no": attempt,
                "backend": current_backend.name, "provider": candidate["provider"],
                "model": candidate["model"], "native_effort": candidate["native_variant"],
                "normalized_effort": resolution.get("normalized_effort") if isinstance(resolution, dict) else None,
                "required_tier": initial_tier, "required_effort": node["routing"]["effort"],
                "status": "SUCCESS" if success else "FAILED",
                "failure_category": None if success else (category or "UNKNOWN_FAILURE"),
                "is_retry": attempt > 1 and current_backend.name == first_backend,
                "is_fallback": current_backend.name != first_backend,
                "completion_signal": result.get("completion_signal"),
            })
        if success:
            return _finalize(node, result, SUCCESS, None, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence)
        if category == "PERMISSION_REQUIRED":
            return _finalize(node, result, FAILURE, category, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence)
        if category in INFRA_CATEGORIES:
            if infra_used < infra_retries:
                infra_used += 1
                continue
            # Capability-preserving backend failover (never downgrades the tier floor).
            if candidates is not None and health is not None:
                next_candidate = failover.failover(candidates, candidate, health, category)
                if next_candidate and next_candidate["backend"] in registry:
                    candidate = next_candidate
                    current_backend = registry[next_candidate["backend"]]
                    infra_used = 0
                    continue
            return _finalize(node, result, FAILURE, category, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence)
        if category == REASONING:
            next_tier = NEXT_TIER.get(tier)
            if next_tier and escalations < 2:
                tier = next_tier
                escalations += 1
                if health is not None:
                    stronger = failover.resolve(next_tier, node["routing"]["effort"],
                                                failover.candidates_for_tier(next_tier), health,
                                                constraints=constraints)
                    chosen = failover.select(stronger)
                    if chosen and chosen["backend"] in registry:
                        candidate = chosen
                        current_backend = registry[chosen["backend"]]
                continue
            return _finalize(node, result, FAILURE, category, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence)
        if category == IMPLEMENTATION and same_used < same_tier_retries:
            same_used += 1
            continue
        return _finalize(node, result, FAILURE, category or "UNKNOWN_FAILURE", initial_tier, tier,
                         attempt, escalations, decision_id, resolution, state_dir, record_evidence)


def _run_tool_node(node, deps, results, tool_executor, *, state_dir, record_evidence) -> dict[str, Any]:
    """Deterministic execution: 0 AI calls, no model evidence. A success is only
    claimed when a deterministic check passes; otherwise the node escalates to AI."""
    operation = (node.get("tool") or {}).get("operation")
    args = (node.get("tool") or {}).get("args") or {}
    task = {"operation": operation, "args": args, "mode": node.get("intent")}
    try:
        result = tool_executor.execute(task)
        ok = tool_executor.validate_result(task, result)
    except Exception as exc:  # policy/exec failure — never model weakness
        return {"task_id": node["task_id"], "executor": "TOOL_ONLY", "operation": operation,
                "status": FAILURE, "failure_category": "TOOL_EXECUTION_FAILED",
                "summary": f"{operation}: {type(exc).__name__}"}
    if operation == "finalize":
        status = SUCCESS if ok else FAILURE
        category = None if ok else "DETERMINISTIC_CHECK_FAILED"
    elif operation == "config_value_matches" and result.get("matches"):
        status, category = "NO_CHANGE_REQUIRED", None
    elif ok:
        status, category = SUCCESS, None
    else:
        status, category = "AI_REQUIRED", "DETERMINISTIC_CHECK_FAILED"
    if record_evidence:
        try:
            evidence.outcome(decision_id=uuid.uuid4().hex,
                             outcome="SUCCESS" if status in (SUCCESS, "NO_CHANGE_REQUIRED") else "FAILURE",
                             task_family=node.get("task_family"), task_class=node.get("task_class"),
                             source="tool_only", surface="tool_only", failure_category=category,
                             state_dir=state_dir)
        except evidence.EvidenceError:
            pass
    bounded = {key: result.get(key) for key in ("branch", "head", "clean", "matches", "parsed",
                                                "exists", "ok", "actual") if key in result}
    return {"task_id": node["task_id"], "executor": "TOOL_ONLY", "operation": operation,
            "status": status, "failure_category": category, "summary": operation, "result": bounded}


def _is_success(node: dict[str, Any], result: dict[str, Any]) -> bool:
    """SUCCESS requires a bounded completion signal; exit code alone is never enough.
    When the backend supplies acceptance_results, all criteria must pass."""
    if result.get("status") != SUCCESS or not result.get("completion_signal"):
        return False
    acceptance = result.get("acceptance_results")
    if acceptance:
        return all(acceptance.get(criterion, False) for criterion in node["acceptance_criteria"])
    return True


def _request(node, resolution, dependency_summaries, execution_id, tier, backend,
             read_only: bool = False) -> dict[str, Any]:
    return {
        "execution_id": execution_id, "task_id": node["task_id"], "task_family": node["task_family"],
        "task_class": node["task_class"], "tier": tier, "provider": resolution["provider"],
        "model": resolution["model"], "native_variant": resolution["native_variant"],
        "objective_summary": node["objective_summary"], "acceptance_criteria": node["acceptance_criteria"],
        "dependency_summaries": dependency_summaries, "knowledge_refs": node.get("knowledge_refs", []),
        "context_manifest": node["context"]["manifest"], "working_dir": str(ROOT),
        "read_only": read_only,
        "prompt": _prompt(node, dependency_summaries, read_only=read_only),
    }


def _prompt(node, dependency_summaries, read_only: bool = False) -> str:
    deps = "; ".join(f"{d['task_id']}: {d['summary']}" for d in dependency_summaries) or "none"
    guard = "\nRead-only task: do not modify, create, or delete any files." if read_only else ""
    return (f"{WORKER_ISOLATION_NOTE}{guard}\n\nObjective: {node['objective_summary']}\n"
            f"Acceptance: {', '.join(node['acceptance_criteria'])}\nDependencies: {deps}")


def _finalize(node, result, status, category, initial_tier, final_tier, attempts, escalations,
              decision_id, resolution, state_dir, record_evidence) -> dict[str, Any]:
    if record_evidence and decision_id:
        outcome = SUCCESS if status == SUCCESS else ("UNKNOWN" if status == "UNKNOWN" else FAILURE)
        completion = result.get("completion_signal") if status == SUCCESS else None
        final_effort = (resolution or {}).get("normalized_effort") or node["routing"]["effort"]
        try:
            evidence.finish(decision_id, outcome=outcome, completion_signal=completion,
                            final_tier=final_tier, final_effort=final_effort,
                            failure_category=category, attempt_count=attempts, state_dir=state_dir)
        except evidence.EvidenceError:
            pass
    return {"task_id": node["task_id"], "status": status, "failure_category": category,
            "summary": result.get("summary", ""), "decisions": result.get("decisions", []),
            "files_changed": result.get("files_changed", []),
            "acceptance_results": result.get("acceptance_results", {}),
            "new_risk_signals": result.get("new_risk_signals", []),
            "recommended_followups": result.get("recommended_followups", []),
            "initial_tier": initial_tier, "final_tier": final_tier, "attempts": attempts,
            "escalated": final_tier != initial_tier, "escalations": escalations}


def _final_summary(plan: dict[str, Any], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"total": len(plan["nodes"]),
            "succeeded": sum(1 for r in results.values() if r.get("status") == SUCCESS),
            "failed": sum(1 for r in results.values() if r.get("status") == FAILURE),
            "blocked": sum(1 for r in results.values() if r.get("status") == BLOCKED),
            "skipped": sum(1 for r in results.values() if r.get("status") == SKIPPED),
            "cancelled": sum(1 for r in results.values() if r.get("status") == CANCELLED),
            "unresolved_blockers": [r["task_id"] for r in results.values() if r.get("status") == FAILURE]}


def _state_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "state.json"


def _load_state(run_dir: pathlib.Path) -> Optional[dict[str, Any]]:
    path = _state_path(run_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_state(run_dir: pathlib.Path, state: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _state_path(run_dir).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def render_execution(report: dict[str, Any]) -> str:
    lines = [f"RUN {report.get('run_id')}  backend={report.get('backend')}  intent={report.get('intent')}"
             + ("  (dry-run)" if report.get("dry_run") else "")]
    for binding in report.get("resolutions", []):
        lines.append(f"{binding['task_id']}  {binding['tier']}  ->  {binding.get('provider')}/{binding.get('model')}"
                     f"  variant {binding.get('native_variant')}  {binding.get('mode')}"
                     + ("" if binding.get("runnable") else "  [NO_COMPATIBLE_EXECUTION_BACKEND]"))
    for task_id, result in report.get("nodes", {}).items():
        lines.append(f"{task_id}: {result.get('status')}"
                     + (f" ({result['failure_category']})" if result.get("failure_category") else ""))
    if report.get("error"):
        lines.append(f"ERROR: {report['error']}")
    if report.get("final"):
        final = report["final"]
        lines.append(f"FINAL: {final['succeeded']}/{final['total']} succeeded; "
                     f"blockers {final['unresolved_blockers'] or 'none'}")
    bounds = report.get("bounds", {})
    lines.append(f"CALL BOUNDS: planned {bounds.get('planned_ai_nodes')}, conditional "
                 f"{bounds.get('conditional_nodes')}, max retries/escalations "
                 f"{bounds.get('max_retry_and_escalation_calls')}, parallel 0")
    if report.get("max_ai_calls") is not None:
        lines.append(f"HARD AI-CALL BUDGET: max {report['max_ai_calls']}, used {report.get('calls_used')}, "
                     f"remaining {report.get('remaining_calls')}")
    return "\n".join(lines)
