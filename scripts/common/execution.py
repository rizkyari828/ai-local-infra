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
        result["provider"] = request["provider"]
        result["model"] = request["model"]
        return result


class CliBackend(Backend):
    """Real local CLI backend. Invoked only with an explicit --confirm gate."""
    mode = "SUPPORTED"

    def __init__(self, name: str, binary: str, timeout: int = 900):
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
        status = SUCCESS if completed.returncode == 0 and not category else FAILURE
        return {"task_id": request["task_id"], "status": status, "failure_category": category,
                "summary": (completed.stdout or "")[-400:] if status == SUCCESS else (completed.stderr or "")[-400:],
                "acceptance_results": {}, "files_changed": [], "decisions": [], "new_risk_signals": []}


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
    if "401" in text or "403" in text or "unauthorized" in text or "auth" in text:
        return "AUTH_FAILURE"
    if "not supported" in text or "unsupported model" in text:
        return "ACCOUNT_MODEL_UNSUPPORTED"
    if "session" in text and "stale" in text:
        return "STALE_SESSION"
    return "UNKNOWN_FAILURE"


def backend_for(name: str, *, timeout: int = 900) -> Backend:
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


def resolve_plan(plan: dict[str, Any], backend: Backend, surface: Optional[str] = None) -> list[dict[str, Any]]:
    resolutions = []
    for node in plan["nodes"]:
        tier = node["routing"]["tier"]
        resolution = routing.resolve_model(tier, routing.load_policy())
        resolved = backend.resolve(node, resolution["provider"], resolution["model"],
                                   node["routing"]["effort"])
        capability = None
        if surface:
            capability = discovery.capability(resolution["provider"], resolution["model"], surface)["status"]
        resolved.update({"task_id": node["task_id"], "tier": tier,
                         "capability_status": capability,
                         "runnable": bool(backend.available() and resolution["model"]) or backend.name == "fake"})
        resolutions.append(resolved)
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


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def execute_plan(plan: dict[str, Any], *, backend: Backend, run_id: Optional[str] = None,
                 state_dir: Optional[pathlib.Path] = None, repo_root: Optional[pathlib.Path] = None,
                 confirm: bool = False, dry_run: bool = False, allow_dirty: bool = False,
                 surface: Optional[str] = None, record_evidence: bool = True,
                 infra_retries: int = 1, same_tier_retries: int = 1, resume: bool = False) -> dict[str, Any]:
    if not plan.get("valid"):
        raise ExecutionError("refusing to execute an invalid plan")
    if not confirm and not dry_run:
        raise ExecutionError("execution requires --confirm (or use --dry-run)")
    repo_root = pathlib.Path(repo_root or ROOT)
    state = repo_state(repo_root)
    bindings = resolve_plan(plan, backend, surface)
    bounds = call_bounds(plan, infra_retries=infra_retries, same_tier_retries=same_tier_retries)

    report: dict[str, Any] = {"run_id": run_id, "backend": backend.name, "dry_run": dry_run,
                              "bounds": bounds, "resolutions": bindings, "nodes": {},
                              "head": state["head"], "dirty": state["dirty"]}
    if any(not binding["runnable"] for binding in bindings):
        report["error"] = "NO_COMPATIBLE_EXECUTION_BACKEND for one or more nodes"
        report["nodes"] = {b["task_id"]: {"status": "NO_COMPATIBLE_EXECUTION_BACKEND",
                                          "required_tier": b["tier"]} for b in bindings if not b["runnable"]}
        return report
    if dry_run:
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
    resolutions = {b["task_id"]: b for b in bindings}

    for task_id in plan["order"]:
        node = next(item for item in plan["nodes"] if item["task_id"] == task_id)
        if resume and results.get(task_id, {}).get("status") == SUCCESS:
            continue
        deps = node["depends_on"]
        if any(results.get(dep, {}).get("status") not in (SUCCESS, SKIPPED) for dep in deps):
            results[task_id] = {"status": BLOCKED, "task_id": task_id}
            continue
        if node["conditional"] and not _conditional_active(node, deps, results):
            results[task_id] = {"status": SKIPPED, "task_id": task_id}
            continue
        try:
            results[task_id] = _run_node(node, deps, results, backend, resolutions[task_id],
                                         state_dir=state_dir, record_evidence=record_evidence,
                                         infra_retries=infra_retries, same_tier_retries=same_tier_retries)
        except KeyboardInterrupt:
            results[task_id] = {"status": CANCELLED, "task_id": task_id}
            for future in plan["order"][plan["order"].index(task_id) + 1:]:
                results.setdefault(future, {"status": NOT_RUN, "task_id": future})
            break
        _save_state(run_dir, {"run_id": run_id, "head": state["head"], "repo": state["root"],
                              "nodes": results})
    report["nodes"] = results
    report["final"] = _final_summary(plan, results)
    _save_state(run_dir, {"run_id": run_id, "head": state["head"], "repo": state["root"], "nodes": results})
    return report


def _conditional_active(node: dict[str, Any], deps: list[str], results: dict[str, dict[str, Any]]) -> bool:
    if not node["conditional"]:
        return True
    for dep in deps:
        outcome = results.get(dep, {})
        if outcome.get("new_risk_signals") or outcome.get("failure_category") in (REASONING, IMPLEMENTATION):
            return True
    return False


def _run_node(node, deps, results, backend, resolution, *, state_dir, record_evidence,
              infra_retries, same_tier_retries) -> dict[str, Any]:
    dependency_summaries = [{"task_id": dep, "summary": results.get(dep, {}).get("summary", "")}
                            for dep in deps]
    tier = node["routing"]["tier"]
    initial_tier = tier
    attempt = 0
    infra_used = same_used = escalations = 0
    execution_id = uuid.uuid4().hex
    decision_id = evidence.start(task_family=node["task_family"], task_class=node["task_class"],
                                 surface=None, provider=resolution["provider"], model=resolution["model"],
                                 native_variant=resolution["native_variant"], initial_tier=initial_tier,
                                 initial_effort=node["routing"]["effort"], state_dir=state_dir) \
        if record_evidence else execution_id
    while True:
        attempt += 1
        request = _request(node, resolution, dependency_summaries, execution_id, tier, backend)
        result = backend.execute(request)
        category = result.get("failure_category")
        status = result.get("status", FAILURE)
        acceptance = result.get("acceptance_results") or {}
        validated = status == SUCCESS and _acceptance_ok(node, acceptance)
        if validated:
            return _finalize(node, result, SUCCESS, None, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence)
        if category in INFRA_CATEGORIES:
            if infra_used < infra_retries:
                infra_used += 1
                continue
            return _finalize(node, result, FAILURE, category, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence)
        if category == REASONING:
            next_tier = NEXT_TIER.get(tier)
            if next_tier and escalations < 2:
                tier = next_tier
                escalations += 1
                continue
            return _finalize(node, result, FAILURE, category, initial_tier, tier, attempt, escalations,
                             decision_id, resolution, state_dir, record_evidence)
        if category == IMPLEMENTATION and same_used < same_tier_retries:
            same_used += 1
            continue
        return _finalize(node, result, FAILURE, category or "UNKNOWN_FAILURE", initial_tier, tier,
                         attempt, escalations, decision_id, resolution, state_dir, record_evidence)


def _acceptance_ok(node: dict[str, Any], acceptance: dict[str, Any]) -> bool:
    criteria = node["acceptance_criteria"]
    if not criteria:
        return True
    return all(acceptance.get(criterion, False) for criterion in criteria)


def _request(node, resolution, dependency_summaries, execution_id, tier, backend) -> dict[str, Any]:
    return {
        "execution_id": execution_id, "task_id": node["task_id"], "task_family": node["task_family"],
        "task_class": node["task_class"], "tier": tier, "provider": resolution["provider"],
        "model": resolution["model"], "native_variant": resolution["native_variant"],
        "objective_summary": node["objective_summary"], "acceptance_criteria": node["acceptance_criteria"],
        "dependency_summaries": dependency_summaries, "knowledge_refs": node.get("knowledge_refs", []),
        "context_manifest": node["context"]["manifest"], "working_dir": str(ROOT),
        "prompt": _prompt(node, dependency_summaries),
    }


def _prompt(node, dependency_summaries) -> str:
    deps = "; ".join(f"{d['task_id']}: {d['summary']}" for d in dependency_summaries) or "none"
    return (f"{WORKER_ISOLATION_NOTE}\n\nObjective: {node['objective_summary']}\n"
            f"Acceptance: {', '.join(node['acceptance_criteria'])}\nDependencies: {deps}")


def _finalize(node, result, status, category, initial_tier, final_tier, attempts, escalations,
              decision_id, resolution, state_dir, record_evidence) -> dict[str, Any]:
    if record_evidence and decision_id:
        completion = "acceptance_passed" if status == SUCCESS else None
        try:
            evidence.finish(decision_id, outcome=status, completion_signal=completion,
                            final_tier=final_tier, final_effort=node["routing"]["effort"],
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
    lines = [f"RUN {report.get('run_id')}  backend={report.get('backend')}"
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
    return "\n".join(lines)
