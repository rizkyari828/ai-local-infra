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
import hashlib
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
import result_cache  # scripts/common, on sys.path
import orchestration_store  # scripts/common, on sys.path

ROOT = infra.ROOT
INFRA_CATEGORIES = ("PROVIDER_503", "RATE_LIMIT", "TIMEOUT", "STALE_SESSION", "AUTH_FAILURE",
                    "SAFETY_GUARD", "CONFIG_DRIFT")
REASONING = "REASONING_INSUFFICIENT"
IMPLEMENTATION = "IMPLEMENTATION_ERROR"
NEXT_TIER = {"FAST": "BALANCED", "BALANCED": "STRONG", "STRONG": "MAX", "MAX": None}
SUCCESS, FAILURE, SKIPPED, BLOCKED, NOT_RUN, CANCELLED = "SUCCESS", "FAILURE", "SKIPPED", "BLOCKED", "NOT_RUN", "CANCELLED"
UNVERIFIED = "UNVERIFIED"  # process completed but independent task acceptance is unavailable
ACCEPTED = (SUCCESS, "NO_CHANGE_REQUIRED")

# Task acceptance is INDEPENDENT of process completion. A model's prose or exit code
# is never acceptance; structured criteria that pass are the only auto-accepted signal.
ACCEPTANCE_PASSED, ACCEPTANCE_FAILED, ACCEPTANCE_UNVERIFIED = "PASSED", "FAILED", "UNVERIFIED"

WORKER_ISOLATION_NOTE = ("Execute only this node's objective. Do not re-plan the master "
                         "objective, do not spawn nested orchestrators or sub-agents, and return "
                         "a concise structured result.")

_EXPLANATORY_ACCEPTANCE = ("i reviewed", "i cannot", "cannot complete", "unable to",
                           "i was unable", "not able to complete")


def _acceptance(node: dict[str, Any], result: dict[str, Any]) -> tuple[str, Optional[str]]:
    """Classify task acceptance independently of the process exit status.

    PASSED      -> every required criterion has explicit evidence and passes
    FAILED      -> explicit negative/incomplete evidence for a required criterion
    UNVERIFIED  -> the process completed but no independent acceptance is available
    """
    acceptance = result.get("acceptance_results")
    criteria = list(node.get("acceptance_criteria") or [])
    if isinstance(acceptance, dict) and acceptance:
        missing = [c for c in criteria if c not in acceptance]
        if missing:
            return ACCEPTANCE_UNVERIFIED, "acceptance evidence incomplete"
        if all(bool(acceptance[c]) for c in criteria):
            return ACCEPTANCE_PASSED, None
        return ACCEPTANCE_FAILED, "acceptance criterion not satisfied"
    summary = (result.get("summary") or "").lower()
    if any(cue in summary for cue in _EXPLANATORY_ACCEPTANCE):
        return ACCEPTANCE_UNVERIFIED, "worker did not claim task completion"
    return ACCEPTANCE_UNVERIFIED, "no independent acceptance evidence"


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


def _git_raw(root: pathlib.Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    except OSError:
        return ""
    return result.stdout or ""


def _worktree_fingerprint(root: pathlib.Path) -> str:
    """Content+state fingerprint of the working tree. Unlike a changed-path diff, this
    also detects further edits to files that were already dirty before the run, so a
    read-only violation on an already-dirty file is still caught.

    Known residual limit: ignored files and untracked content outside `git status`
    output are not covered; this is post-hoc detection, not a sandbox."""
    digest = hashlib.sha256()
    for part in ("status --porcelain -z", "diff --no-color", "diff --cached --no-color"):
        digest.update(part.encode())
        digest.update(_git_raw(root, *part.split()).encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()


def workspace_root(root: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Resolve ONE explicit workspace root used for Git, context, tools and cwd."""
    resolved = pathlib.Path(root or ROOT).expanduser()
    if not resolved.exists() or not resolved.is_dir():
        raise ExecutionError(f"WORKSPACE_INVALID: workspace root is not a directory: {resolved}")
    return resolved.resolve()


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
                 tool_executor: Optional[deterministic.ToolExecutor] = None,
                 cache: bool = False, force_refresh: bool = False,
                 project_id: Optional[str] = None,
                 cache_store: Optional["result_cache.ResultCache"] = None,
                 explicit_model: bool = False,
                 allow_stronger_reuse: bool = True,
                 entry_mode: str = "DIRECT_CLI",
                 controller_ai_calls: Optional[int] = None) -> dict[str, Any]:
    if not plan.get("valid"):
        raise ExecutionError("refusing to execute an invalid plan")
    if not confirm and not dry_run:
        raise ExecutionError("execution requires --confirm (or use --dry-run)")
    repo_root = workspace_root(repo_root)
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
                              "entry_mode": entry_mode, "controller_ai_calls": controller_ai_calls,
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
    plan_fingerprint = _plan_fingerprint(plan)
    state["plan_fingerprint"] = plan_fingerprint
    state["intent"] = plan.get("intent")
    previous = _load_state(run_dir) if resume else None
    if resume and previous:
        if previous.get("head") != state["head"] or previous.get("repo") != state["root"]:
            report["error"] = "REFUSE_RESUME: repository HEAD/identity changed; refusing to reuse stale results"
            return report
        if previous.get("plan_fingerprint") != plan_fingerprint:
            report["error"] = ("REFUSE_RESUME: plan/task contract identity changed; refusing to reuse "
                               "stale results")
            return report
        uncertain = _uncertain_mutation(plan, previous)
        if uncertain:
            report["error"] = (f"REFUSE_RESUME_UNCERTAIN_MUTATION: {uncertain} may have mutated before "
                               "acceptance was recorded; human review required")
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
    # Provenance of this execution's results: simulated results are isolated from real
    # production reuse/evidence and can never satisfy a real cache lookup.
    run_provenance = (result_cache.PROVENANCE_SIMULATED
                      if getattr(backend, "mode", "SUPPORTED") == "SIMULATED"
                      else result_cache.PROVENANCE_REAL)

    attempts: list[dict[str, Any]] = []
    context_manifests: dict[str, dict[str, Any]] = {}
    cache_meta: dict[str, Any] = {"enabled": bool(cache), "hits": 0, "misses": 0, "writes": 0,
                                  "rejected": 0, "decisions": []}
    store, owns_store, repo_identity = _open_cache(cache, cache_store, state_dir, project_id, repo_root)
    if store is None:
        cache_meta["enabled"] = False
    try:
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
                if store is not None and results[task_id]["status"] in (SUCCESS, "NO_CHANGE_REQUIRED"):
                    _annotate_result(node, results[task_id])
                if results[task_id]["status"] in ("AI_REQUIRED", FAILURE):
                    report["error"] = results[task_id].get("failure_category") or "TOOL_EXECUTION_FAILED"
                    _mark_not_run(plan, task_id, results)
                    break
                _save_state(run_dir, _state_blob(run_id, state, results, budget))
                continue
            dependency_summaries = _dependency_facts(node, results)
            context_result = _build_node_context(node, dependency_summaries, repo_root)
            identity = None
            if store is not None:
                dependencies_complete = all(fact.get("result_fingerprint") for fact in dependency_summaries)
                freshness = result_cache.freshness_policy(node, constraints=constraints)
                identity = _cache_identity(node, context_result, dependency_summaries, repo_identity,
                                           freshness, state["head"])
                resolution = resolutions.get(task_id) or {}
                decision = result_cache.decide(
                    store=store, project_id=repo_identity, node=node, identity=identity,
                    required_tier=resolution.get("tier"), required_effort=node["routing"]["effort"],
                    freshness=freshness, force_refresh=force_refresh, explicit_model=explicit_model,
                    allow_stronger_reuse=allow_stronger_reuse, dependencies_complete=dependencies_complete,
                    provenance=run_provenance)
                cache_meta["decisions"].append(_bounded_decision(decision))
                if decision["decision"] == result_cache.REUSE:
                    cache_meta["hits"] += 1
                    results[task_id] = _reused_result(node, decision)
                    _save_state(run_dir, _state_blob(run_id, state, results, budget))
                    context_manifests.setdefault(task_id, (context_result or {}).get("manifest"))
                    continue
                cache_meta["misses"] += 1
            try:
                node_result = _run_node(node, deps, results, backend, resolutions[task_id],
                                        state_dir=state_dir, record_evidence=record_evidence,
                                        infra_retries=infra_retries, same_tier_retries=same_tier_retries,
                                        surface=backend.name, budget=budget,
                                        enforce_read_only=enforce_read_only, repo_root=repo_root,
                                        candidates=resolutions[task_id].get("candidates"),
                                        backends=backend_registry, health=health,
                                        constraints=constraints, attempts=attempts,
                                        context_manifests=context_manifests,
                                        dependency_summaries=dependency_summaries,
                                        prebuilt_context=context_result)
            except KeyboardInterrupt:
                results[task_id] = {"status": CANCELLED, "task_id": task_id}
                _mark_not_run(plan, task_id, results)
                break
            results[task_id] = node_result
            if store is not None and identity is not None:
                node_result["input_fingerprint"] = identity["input_fingerprint"]
            if node_result["status"] == SUCCESS:
                fp, bounded, rtype = _annotate_result(node, node_result)
                if store is not None and identity is not None and fp is not None:
                    _write_cache(store, node, identity, freshness, node_result, bounded, rtype, fp,
                                 attempts, run_id, repo_identity, cache_meta, run_provenance)
            if node_result["status"] in ("CALL_BUDGET_EXHAUSTED", "READ_ONLY_VIOLATION"):
                report["error"] = node_result["status"]
                _mark_not_run(plan, task_id, results)
                break
            _save_state(run_dir, _state_blob(run_id, state, results, budget))
    finally:
        if owns_store and store is not None:
            store.close()
    report["nodes"] = results
    report["attempts"] = attempts
    report["context_manifests"] = context_manifests
    report["calls_used"] = budget["used"]
    report["remaining_calls"] = (budget["max"] - budget["used"]) if budget["max"] is not None else None
    report["cache"] = cache_meta
    report["logical_ai_nodes"] = sum(1 for node in plan["nodes"] if node.get("executor") != "TOOL_ONLY")
    report["cache_reused"] = cache_meta["hits"]
    report["real_ai_calls"] = budget["used"]
    report["final"] = _final_summary(plan, results)
    report["accounting"] = _accounting(plan, report, cache_meta["hits"])
    _save_state(run_dir, _state_blob(run_id, state, results, budget))
    return report


def _accounting(plan: dict[str, Any], report: dict[str, Any], cache_hits: int) -> dict[str, Any]:
    """Truthful invocation accounting. `worker_launches` increments at the actual
    launch boundary; it does NOT claim to bound model turns internal to an external
    coding-agent CLI, which the engine cannot observe."""
    attempts = report.get("attempts") or []
    planner = (plan.get("planner_mode") or {}).get("mode")
    return {
        "logical_ai_tasks": sum(1 for node in plan["nodes"] if node.get("executor") != "TOOL_ONLY"),
        "worker_launches": report.get("calls_used", 0),
        "provider_inference_requests": None,  # not observable through an agent CLI
        "cache_reuses": cache_hits,
        "planner_calls": 0 if planner in (None, "NONE", "DETERMINISTIC") else 1,
        "retries": sum(1 for a in attempts if a.get("is_retry")),
        "fallbacks": sum(1 for a in attempts if a.get("is_fallback")),
        "tool_only_tasks": sum(1 for node in plan["nodes"] if node.get("executor") == "TOOL_ONLY"),
        "hard_budget_semantics": "bounds worker CLI launches made by this engine",
    }


def _mark_not_run(plan: dict[str, Any], current: str, results: dict[str, dict[str, Any]]) -> None:
    index = plan["order"].index(current)
    for future in plan["order"][index + 1:]:
        results.setdefault(future, {"status": NOT_RUN, "task_id": future})


# --------------------------------------------------------------------------- #
# Result cache integration (Layer 3) — deterministic reuse, zero AI calls on hit
# --------------------------------------------------------------------------- #


def _open_cache(cache, cache_store, state_dir, project_id, repo_root):
    """Best-effort cache setup. Any failure disables caching; it never fails a run."""
    if not cache:
        return None, False, None
    owns = False
    store = cache_store
    try:
        if store is None:
            store = result_cache.ResultCache(state_dir=state_dir)
            owns = True
        identity = project_id or orchestration_store.repository_identity(repo_root)["repository_id"]
    except Exception:  # cache is best-effort local state, never a run blocker
        if owns and store is not None:
            store.close()
        return None, False, None
    return store, owns, identity


def _dependency_facts(node: dict[str, Any], results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for dep in node["depends_on"]:
        outcome = results.get(dep, {})
        fact: dict[str, Any] = {"task_id": dep, "summary": outcome.get("summary", ""),
                                "status": outcome.get("status")}
        # Meaningful dependency identity: the accepted result fingerprint, never
        # execution activity (reused vs executed must not change descendant identity).
        # A deterministically-inactive (SKIPPED) conditional node contributes a stable
        # placeholder so it neither blocks nor pollutes descendant reuse.
        fingerprint = outcome.get("result_fingerprint")
        if not fingerprint and outcome.get("status") == SKIPPED:
            fingerprint = result_cache.result_fingerprint("inactive", {"status": SKIPPED})
        if fingerprint:
            fact["result_fingerprint"] = fingerprint
        facts.append(fact)
    return facts


def _build_node_context(node: dict[str, Any], dependency_summaries: list[dict[str, Any]],
                        repo_root: pathlib.Path) -> Optional[dict[str, Any]]:
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
            repository={"head": None})
        return context_retrieval.build_pack(ctx_request, root=repo_root)
    except (context_retrieval.ContextError, OSError):
        return None


def _cache_identity(node: dict[str, Any], context_result: Optional[dict[str, Any]],
                    dependency_summaries: list[dict[str, Any]], project_id: str,
                    freshness: str, repository_head: Optional[str]) -> dict[str, Any]:
    manifest = (context_result or {}).get("manifest")
    semantic = result_cache.task_semantic_fingerprint(node)
    context_fp = result_cache.context_identity(manifest)
    dep_fps = [fact["result_fingerprint"] for fact in dependency_summaries
               if fact.get("result_fingerprint")]
    fingerprint = result_cache.input_fingerprint(
        project_id=project_id, task_semantic_fingerprint=semantic, context_fingerprint=context_fp,
        dependency_fingerprints=dep_fps, freshness_policy=freshness, repository_head=repository_head)
    return {"task_semantic_fingerprint": semantic, "context_manifest_fingerprint": context_fp,
            "input_fingerprint": fingerprint, "dependency_fingerprints": dep_fps}


def _bounded_decision(decision: dict[str, Any]) -> dict[str, Any]:
    entry = decision.get("entry") or {}
    return {"task_id": decision.get("task_id"), "decision": decision.get("decision"),
            "reason": decision.get("reason"), "input_fingerprint": decision.get("input_fingerprint"),
            "task_semantic_fingerprint": decision.get("task_semantic_fingerprint"),
            "context_manifest_fingerprint": decision.get("context_manifest_fingerprint"),
            "freshness_policy": decision.get("freshness_policy"),
            "cache_entry_id": entry.get("cache_entry_id"),
            "reused_from_run_id": entry.get("original_run_id"),
            "reused_from_task_id": entry.get("original_task_id"),
            "result_fingerprint": entry.get("result_fingerprint")}


def _reused_result(node: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    entry = decision["entry"]
    result = entry.get("result") or {}
    return {"task_id": node["task_id"], "status": SUCCESS, "failure_category": None,
            "acceptance": "PASSED",
            "summary": result.get("summary", ""), "decisions": result.get("decisions", []),
            "files_changed": result.get("files_changed", []),
            "acceptance_results": result.get("acceptance_results", {}),
            "new_risk_signals": result.get("new_risk_signals", []), "recommended_followups": [],
            "execution_source": "CACHE", "cache_status": "HIT",
            "cache_entry_id": entry.get("cache_entry_id"),
            "reused_from_run_id": entry.get("original_run_id"),
            "reused_from_task_id": entry.get("original_task_id"),
            "input_fingerprint": entry.get("input_fingerprint"),
            "result_fingerprint": entry.get("result_fingerprint"),
            "reuse_reason": decision.get("reason")}


def _annotate_result(node: dict[str, Any], result: dict[str, Any]):
    """Attach a deterministic accepted-result fingerprint for downstream dependency
    identity (incremental DAG reuse), independent of cache write eligibility."""
    bounded = result_cache.bounded_result(node, result)
    if bounded is None:
        return None, None, None
    rtype = result_cache.result_type(node)
    fingerprint = result_cache.result_fingerprint(rtype, bounded)
    result["result_fingerprint"] = fingerprint
    result.setdefault("execution_source", "TOOL_ONLY" if node.get("executor") == "TOOL_ONLY" else "EXECUTED")
    return fingerprint, bounded, rtype


def _success_attempt(attempts: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    matches = [attempt for attempt in (attempts or [])
               if attempt.get("task_id") == task_id and attempt.get("status") == "SUCCESS"]
    return matches[-1] if matches else {}


def _write_cache(store, node, identity, freshness, result, bounded, rtype, result_fp,
                 attempts, run_id, project_id, cache_meta, run_provenance) -> None:
    eligible, reason = result_cache.eligible_for_cache(node, result, freshness=freshness)
    if not eligible:
        cache_meta["rejected"] += 1
        cache_meta["decisions"].append({"task_id": node["task_id"], "decision": "WRITE_REJECTED",
                                        "reason": reason})
        return
    provenance = _success_attempt(attempts, node["task_id"])
    try:
        entry_id = store.write(
            project_id=project_id, task_semantic_fingerprint=identity["task_semantic_fingerprint"],
            input_fingerprint=identity["input_fingerprint"],
            context_manifest_fingerprint=identity.get("context_manifest_fingerprint"),
            result_fingerprint=result_fp, result_type=rtype, result=bounded, freshness_policy=freshness,
            original_run_id=run_id, original_task_id=node["task_id"],
            original_backend=provenance.get("backend"), original_provider=provenance.get("provider"),
            original_model=provenance.get("model"),
            original_required_tier=(node.get("routing") or {}).get("tier"),
            original_required_effort=(node.get("routing") or {}).get("effort"),
            original_native_effort=provenance.get("native_effort"),
            original_normalized_effort=provenance.get("normalized_effort"),
            result_provenance=(result_cache.PROVENANCE_SIMULATED if provenance.get("simulated")
                               else run_provenance))
    except result_cache.CacheError:
        cache_meta["rejected"] += 1
        cache_meta["decisions"].append({"task_id": node["task_id"], "decision": "WRITE_REJECTED",
                                        "reason": result_cache.REASON_RESULT_NOT_REUSABLE})
        return
    cache_meta["writes"] += 1
    cache_meta["decisions"].append({"task_id": node["task_id"], "decision": "CACHE_WRITTEN",
                                    "reason": result_cache.REASON_UNCHANGED_INPUTS,
                                    "cache_entry_id": entry_id, "result_fingerprint": result_fp,
                                    "input_fingerprint": identity["input_fingerprint"]})


def _state_blob(run_id: str, state: dict[str, Any], results: dict[str, dict[str, Any]],
                budget: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": run_id, "head": state["head"], "repo": state["root"], "nodes": results,
            "calls_used": budget["used"], "hard_max_ai_calls": budget["max"],
            "intent": state.get("intent"), "plan_fingerprint": state.get("plan_fingerprint")}


def _plan_fingerprint(plan: dict[str, Any]) -> str:
    """Identity of the plan + task contracts, so the same repository HEAD cannot resume
    an entirely different objective/task contract."""
    material = {
        "intent": plan.get("intent"),
        "objective_summary": plan.get("objective_summary"),
        "nodes": [{
            "task_id": node.get("task_id"),
            "executor": node.get("executor"),
            "contract": node.get("task_contract") or {"objective": node.get("objective_summary")},
            "acceptance": list(node.get("acceptance_criteria") or []),
            "tool": node.get("tool"),
            "routing": (node.get("routing") or {}),
        } for node in plan.get("nodes", [])],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str)
                          .encode("utf-8", "replace")).hexdigest()[:32]


def _uncertain_mutation(plan: dict[str, Any], previous: dict[str, Any]) -> Optional[str]:
    """A mutating node with no durable terminal record may have mutated before a crash;
    its side effect is unknown, so a resume must not silently replay it."""
    recorded = previous.get("nodes") or {}
    for node in plan.get("nodes", []):
        if node.get("intent") != "MUTATING":
            continue
        status = (recorded.get(node["task_id"]) or {}).get("status")
        if status is None or status in (UNVERIFIED, CANCELLED, "RUNNING"):
            return node["task_id"]
    return None


def _save_state(run_dir: pathlib.Path, state: dict[str, Any]) -> None:
    """Atomic local write: a crash never leaves a partially written state.json."""
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = _state_path(run_dir)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(_redact_state(state), ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def _redact_state(state: dict[str, Any]) -> dict[str, Any]:
    """Apply the secret policy to persisted textual tails (never raw worker output)."""
    redacted = dict(state)
    nodes: dict[str, Any] = {}
    for task_id, result in (state.get("nodes") or {}).items():
        if isinstance(result, dict):
            entry = dict(result)
            if isinstance(entry.get("summary"), str):
                entry["summary"] = _redact_tail(entry["summary"])
            if isinstance(entry.get("decisions"), list):
                entry["decisions"] = [_redact_tail(item) if isinstance(item, str) else item
                                      for item in entry["decisions"]]
            nodes[task_id] = entry
        else:
            nodes[task_id] = result
    redacted["nodes"] = nodes
    return redacted


def _redact_tail(text: str) -> str:
    if not knowledge.learning.contains_secret_like(text):
        return text
    lines = []
    for line in text.splitlines():
        lines.append("[REDACTED]" if knowledge.learning.contains_secret_like(line) else line)
    return "\n".join(lines)


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
              constraints=None, attempts=None, context_manifests=None,
              dependency_summaries=None, prebuilt_context=None) -> dict[str, Any]:
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
    if dependency_summaries is None:
        dependency_summaries = [{"task_id": dep, "summary": results.get(dep, {}).get("summary", "")}
                                for dep in deps]
    attempt = 0
    infra_used = same_used = escalations = 0
    execution_id = uuid.uuid4().hex
    # Node-level read-only scope: a READ_ONLY node keeps its restriction even inside a
    # MIXED graph, and already-dirty files are compared by content, not just path.
    read_only_scope = bool(enforce_read_only or node.get("intent") == "READ_ONLY")
    before_paths = _worktree_paths(repo_root) if read_only_scope else None
    before_fp = _worktree_fingerprint(repo_root) if read_only_scope else None
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
        # The context is validated BEFORE the launch boundary: a rejected/missing
        # context consumes no AI-call budget and counts as zero invocations.
        if prebuilt_context is not None:
            context_result = prebuilt_context
        else:
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
        context_pack = None
        if context_result is not None:
            if context_manifests is not None:
                context_manifests[node["task_id"]] = context_result["manifest"]
            if context_result["status"] != context_retrieval.STATUS_READY:
                return _finalize(node, {"summary": f"context {context_result['status']}"}, FAILURE,
                                 context_result["status"], initial_tier, tier, attempt, escalations,
                                 decision_id, resolution, state_dir, record_evidence)
            context_pack = context_result["pack"]
        res = {"provider": candidate["provider"], "model": candidate["model"],
               "native_variant": candidate["native_variant"]}
        request = _request(node, res, dependency_summaries, execution_id, tier, current_backend,
                           working_dir=repo_root, context_pack=context_pack,
                           context_manifest=(context_result or {}).get("manifest"),
                           read_only=node.get("intent") == "READ_ONLY")
        if context_manifests is not None:
            manifest = context_manifests.setdefault(node["task_id"], context_result["manifest"]
                                                    if context_result is not None else {})
            manifest["delivered_input_fingerprint"] = request["delivered_input_fingerprint"]
        budget["used"] += 1  # actual worker/model launch boundary
        try:
            result = current_backend.execute(request)
        except KeyboardInterrupt:
            raise  # cancellation semantics are handled by the caller
        except Exception as exc:  # adapter bug/crash: never a false SUCCESS, never an orphan
            result = {"task_id": node["task_id"], "status": FAILURE, "failure_category": "UNKNOWN_FAILURE",
                      "summary": f"adapter error: {type(exc).__name__}"}
        candidate = {**candidate, "simulated": getattr(current_backend, "mode", "SUPPORTED") == "SIMULATED"}
        if read_only_scope:
            changed = sorted(_worktree_paths(repo_root) - (before_paths or set()))
            content_changed = _worktree_fingerprint(repo_root) != before_fp
            if changed or content_changed:
                detail = ", ".join(changed[:5]) or "content changed in an already-dirty file"
                return _finalize(node, {"summary": "read-only violation: " + detail},
                                 "READ_ONLY_VIOLATION", "READ_ONLY_VIOLATION", initial_tier, tier,
                                 attempt, escalations, decision_id, resolution, state_dir, record_evidence,
                                 candidate=candidate)
        category = result.get("failure_category")
        process_ok = result.get("status") == SUCCESS and bool(result.get("completion_signal"))
        acceptance_state, acceptance_reason = (ACCEPTANCE_UNVERIFIED, "process did not complete")
        if process_ok:
            acceptance_state, acceptance_reason = _acceptance(node, result)
        accepted = process_ok and acceptance_state == ACCEPTANCE_PASSED
        if attempts is not None:
            attempt_status = ("SUCCESS" if accepted
                              else UNVERIFIED if acceptance_state == ACCEPTANCE_UNVERIFIED and process_ok
                              else "FAILED")
            attempt_category = None if accepted else (
                category or ("ACCEPTANCE_UNVERIFIED" if attempt_status == UNVERIFIED else "ACCEPTANCE_FAILED"))
            attempts.append({
                "task_id": node["task_id"], "attempt_no": attempt,
                "backend": current_backend.name, "provider": candidate["provider"],
                "model": candidate["model"], "native_effort": candidate["native_variant"],
                "normalized_effort": resolution.get("normalized_effort") if isinstance(resolution, dict) else None,
                "required_tier": initial_tier, "required_effort": node["routing"]["effort"],
                "status": attempt_status, "failure_category": attempt_category,
                "is_retry": attempt > 1 and current_backend.name == first_backend,
                "is_fallback": current_backend.name != first_backend,
                "completion_signal": result.get("completion_signal"),
                "simulated": getattr(current_backend, "mode", "SUPPORTED") == "SIMULATED",
            })
        if accepted:
            return _finalize(node, result, SUCCESS, None, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence, candidate=candidate)
        if process_ok and acceptance_state == ACCEPTANCE_FAILED:
            return _finalize(node, result, FAILURE, "ACCEPTANCE_FAILED", initial_tier, tier, attempt,
                             escalations, decision_id, resolution, state_dir, record_evidence,
                             candidate=candidate)
        if process_ok and acceptance_state == ACCEPTANCE_UNVERIFIED:
            # The process completed but no independent acceptance exists: this is not a
            # task failure and never a task success. It cannot feed cache or learning.
            return _finalize(node, result, UNVERIFIED, "ACCEPTANCE_UNVERIFIED", initial_tier, tier,
                             attempt, escalations, decision_id, resolution, state_dir, record_evidence,
                             candidate=candidate)
        if category == "PERMISSION_REQUIRED":
            return _finalize(node, result, FAILURE, category, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence, candidate=candidate)
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
                             decision_id, resolution, state_dir, record_evidence, candidate=candidate)
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
                             decision_id, resolution, state_dir, record_evidence, candidate=candidate)
        if category == IMPLEMENTATION and same_used < same_tier_retries:
            same_used += 1
            continue
        return _finalize(node, result, FAILURE, category or "UNKNOWN_FAILURE", initial_tier, tier,
                         attempt, escalations, decision_id, resolution, state_dir, record_evidence,
                         candidate=candidate)


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
        # Finalization must reflect upstream acceptance, never manufacture success.
        unresolved = [dep for dep in deps
                      if results.get(dep, {}).get("status") not in ACCEPTED + (SKIPPED,)]
        ok = ok and not unresolved
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
            "status": status, "failure_category": category, "summary": operation, "result": bounded,
            "acceptance": ("PASSED" if status in (SUCCESS, "NO_CHANGE_REQUIRED")
                           else "FAILED" if status == FAILURE else "UNVERIFIED")}


def _is_success(node: dict[str, Any], result: dict[str, Any]) -> bool:
    """Backwards-compatible helper: SUCCESS only when the process completed AND every
    required criterion passed. Exit code (or prose) alone is never task success.

    The execution path uses :func:`_acceptance` directly (it distinguishes FAILED from
    UNVERIFIED); this remains for callers that only need the boolean."""
    if result.get("status") != SUCCESS or not result.get("completion_signal"):
        return False
    return _acceptance(node, result)[0] == ACCEPTANCE_PASSED


def _request(node, resolution, dependency_summaries, execution_id, tier, backend,
             *, working_dir, context_pack=None, context_manifest=None, read_only: bool = False) -> dict[str, Any]:
    contract = node.get("task_contract") or {}
    manifest = context_manifest if context_manifest is not None else (node.get("context") or {}).get("manifest", [])
    return {
        "execution_id": execution_id, "task_id": node["task_id"], "task_family": node["task_family"],
        "task_class": node["task_class"], "tier": tier, "provider": resolution["provider"],
        "model": resolution["model"], "native_variant": resolution["native_variant"],
        "objective_summary": node["objective_summary"], "acceptance_criteria": node["acceptance_criteria"],
        "dependency_summaries": dependency_summaries, "knowledge_refs": node.get("knowledge_refs", []),
        "context_manifest": manifest, "context_pack": context_pack, "working_dir": str(working_dir),
        "read_only": read_only, "task_contract": contract,
        "delivered_input_fingerprint": delivered_input_fingerprint(node, dependency_summaries, manifest),
        "prompt": _prompt(node, dependency_summaries, read_only=read_only, context_pack=context_pack),
    }


def delivered_input_fingerprint(node: dict[str, Any], dependency_summaries: list[dict[str, Any]],
                                manifest: Optional[dict[str, Any]]) -> str:
    """Deterministic identity of the FINAL safe semantic worker input: task contract,
    validated context identity, known dependency facts and the acceptance contract.
    Never a raw prompt. Same identity as the representation actually delivered."""
    contract = node.get("task_contract") or {}
    material = {
        "contract_version": contract.get("contract_version"),
        "objective": contract.get("objective") or node.get("objective_summary"),
        "master_fingerprint": contract.get("master_fingerprint"),
        "operation": contract.get("operation"),
        "targets": contract.get("targets"),
        "acceptance": list(node.get("acceptance_criteria") or []),
        "acceptance_contract": result_cache.ACCEPTANCE_CONTRACT_VERSION,
        "context": result_cache.context_identity(manifest),
        "dependencies": sorted(fact["result_fingerprint"] for fact in (dependency_summaries or [])
                               if fact.get("result_fingerprint")),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str)
                          .encode("utf-8", "replace")).hexdigest()[:32]


def _render_context_pack(context_pack: Optional[dict[str, Any]]) -> str:
    if not context_pack:
        return ""
    lines = []
    for item in context_pack.get("items") or []:
        label = item.get("source_kind", "CONTEXT")
        identifier = item.get("source_id") or ""
        lines.append(f"[{label} {identifier}] {item.get('content', '')}")
    return "\n".join(lines)


def _prompt(node, dependency_summaries, read_only: bool = False,
            context_pack: Optional[dict[str, Any]] = None) -> str:
    contract = node.get("task_contract") or {}
    deps = "; ".join(f"{d['task_id']}: {d['summary']}" for d in dependency_summaries) or "none"
    guard = "\nRead-only task: do not modify, create, or delete any files." if read_only else ""
    target_line = ", ".join(contract.get("targets") or []) or "none stated"
    context_text = _render_context_pack(context_pack)
    context_block = f"\nDelivered context:\n{context_text}" if context_text else ""
    return (f"{WORKER_ISOLATION_NOTE}{guard}\n\nObjective: {node['objective_summary']}\n"
            f"Operation: {contract.get('operation', 'execute')}\nTargets: {target_line}\n"
            f"Acceptance: {', '.join(node['acceptance_criteria'])}\nDependencies: {deps}"
            f"{context_block}")


def _finalize(node, result, status, category, initial_tier, final_tier, attempts, escalations,
              decision_id, resolution, state_dir, record_evidence, *, candidate=None) -> dict[str, Any]:
    acceptance_state = "PASSED" if status == SUCCESS else (
        "UNVERIFIED" if status == UNVERIFIED else ("FAILED" if status == FAILURE else None))
    if record_evidence and decision_id:
        outcome = SUCCESS if status == SUCCESS else ("UNKNOWN" if status in ("UNKNOWN", UNVERIFIED) else FAILURE)
        completion = result.get("completion_signal") if status == SUCCESS else None
        final_effort = (resolution or {}).get("normalized_effort") or node["routing"]["effort"]
        # Attribute accepted evidence to the ACTUAL accepted/attempted candidate, not to
        # the originally proposed one after failover.
        attribution = candidate or {}
        try:
            evidence.finish(decision_id, outcome=outcome, completion_signal=completion,
                            final_tier=final_tier, final_effort=final_effort,
                            failure_category=category, attempt_count=attempts, state_dir=state_dir,
                            surface=attribution.get("backend"),
                            provider=attribution.get("provider"), model=attribution.get("model"),
                            native_variant=attribution.get("native_variant"),
                            normalized_effort=(resolution or {}).get("normalized_effort"),
                            simulated=bool(attribution.get("simulated")))
        except evidence.EvidenceError:
            pass
    return {"task_id": node["task_id"], "status": status, "failure_category": category,
            "acceptance": acceptance_state,
            "acceptance_contract": result_cache.ACCEPTANCE_CONTRACT_VERSION,
            "summary": result.get("summary", ""), "decisions": result.get("decisions", []),
            "files_changed": result.get("files_changed", []),
            "acceptance_results": result.get("acceptance_results", {}),
            "new_risk_signals": result.get("new_risk_signals", []),
            "recommended_followups": result.get("recommended_followups", []),
            "initial_tier": initial_tier, "final_tier": final_tier, "attempts": attempts,
            "escalated": final_tier != initial_tier, "escalations": escalations}


def _final_summary(plan: dict[str, Any], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    blockers = [r["task_id"] for r in results.values() if r.get("status") in (FAILURE, UNVERIFIED)]
    return {"total": len(plan["nodes"]),
            "succeeded": sum(1 for r in results.values() if r.get("status") == SUCCESS),
            "failed": sum(1 for r in results.values() if r.get("status") == FAILURE),
            "unverified": sum(1 for r in results.values() if r.get("status") == UNVERIFIED),
            "blocked": sum(1 for r in results.values() if r.get("status") == BLOCKED),
            "skipped": sum(1 for r in results.values() if r.get("status") == SKIPPED),
            "cancelled": sum(1 for r in results.values() if r.get("status") == CANCELLED),
            "unresolved_blockers": blockers}


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


def render_execution(report: dict[str, Any]) -> str:
    lines = [f"RUN {report.get('run_id')}  backend={report.get('backend')}  intent={report.get('intent')}"
             + ("  (dry-run)" if report.get("dry_run") else "")]
    for binding in report.get("resolutions", []):
        lines.append(f"{binding['task_id']}  {binding['tier']}  ->  {binding.get('provider')}/{binding.get('model')}"
                     f"  variant {binding.get('native_variant')}  {binding.get('mode')}"
                     + ("" if binding.get("runnable") else "  [NO_COMPATIBLE_EXECUTION_BACKEND]"))
    for task_id, result in report.get("nodes", {}).items():
        suffix = ""
        if result.get("execution_source") == "CACHE":
            suffix = f"  [CACHE reuse from {result.get('reused_from_run_id')}/{result.get('reused_from_task_id')}]"
        lines.append(f"{task_id}: {result.get('status')}"
                     + (f" ({result['failure_category']})" if result.get("failure_category") else "") + suffix)
    cache = report.get("cache")
    if cache and cache.get("enabled"):
        lines.append(f"RESULT CACHE: logical_ai {report.get('logical_ai_nodes')}, reused {cache['hits']}, "
                     f"executed {report.get('real_ai_calls')} real AI calls, writes {cache['writes']}, "
                     f"misses {cache['misses']}, rejected {cache['rejected']}")
    if report.get("error"):
        lines.append(f"ERROR: {report['error']}")
    if report.get("final"):
        final = report["final"]
        lines.append(f"FINAL: {final['succeeded']}/{final['total']} succeeded; "
                     f"unverified {final.get('unverified', 0)}; "
                     f"blockers {final['unresolved_blockers'] or 'none'}")
    bounds = report.get("bounds", {})
    lines.append(f"CALL BOUNDS: planned {bounds.get('planned_ai_nodes')}, conditional "
                 f"{bounds.get('conditional_nodes')}, max retries/escalations "
                 f"{bounds.get('max_retry_and_escalation_calls')}, parallel 0")
    if report.get("max_ai_calls") is not None:
        lines.append(f"HARD AI-CALL BUDGET: max {report['max_ai_calls']}, used {report.get('calls_used')}, "
                     f"remaining {report.get('remaining_calls')}")
    return "\n".join(lines)
