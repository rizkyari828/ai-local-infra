"""Task graph orchestrator V0.1 (deterministic, plan-only, standard library only).

Turns ONE bounded objective into a small task graph (DAG) with per-node symbolic
routing and context budgets. It PLANS ONLY: it calls no model, makes no network
request, spawns no workers, and persists nothing. The raw objective is processed in
memory and never stored.

Reuses existing, provider-agnostic layers: routing (classification + symbolic
tier/effort), derived knowledge (lessons / historical evidence / learned defaults),
and discovery (model capability resolution). No provider/model name is hardcoded in
decomposition semantics.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

import routing  # scripts/common, on sys.path
import derived_knowledge as knowledge  # scripts/common, on sys.path
import discovery  # scripts/common, on sys.path
import deterministic as deterministic_tools  # scripts/common, on sys.path

TIER_ORDER = list(routing.DEFAULT_TIER_ORDER)  # FAST, BALANCED, STRONG, MAX
CONTEXT_BUDGETS = ("TINY", "SMALL", "MEDIUM", "LARGE")
CONTEXT_CATEGORIES = ("OBJECTIVE_SUMMARY", "DEPENDENCY_OUTPUT", "SHARED_KNOWLEDGE",
                      "RELEVANT_FILES", "RELEVANT_CONFIG", "RELEVANT_TESTS", "REPOSITORY_STATE")
EXCLUDED_CATEGORIES = ("FULL_CHAT", "FULL_REPOSITORY", "ALL_PREVIOUS_OUTPUTS")
MAX_PLAN_NODES = 8  # task-size discipline; exceeding this warns (EXCESSIVE_TASK_SPLITTING)

DEFAULT_BUDGET = {"MECHANICAL": "SMALL", "NORMAL": "SMALL",
                  "CORRECTNESS_SENSITIVE": "MEDIUM", "CRITICAL": "MEDIUM"}
BASE_CONTEXT = {
    "MECHANICAL": ["OBJECTIVE_SUMMARY", "DEPENDENCY_OUTPUT", "RELEVANT_FILES"],
    "NORMAL": ["OBJECTIVE_SUMMARY", "DEPENDENCY_OUTPUT", "SHARED_KNOWLEDGE", "RELEVANT_FILES",
               "REPOSITORY_STATE"],
    "CORRECTNESS_SENSITIVE": ["OBJECTIVE_SUMMARY", "DEPENDENCY_OUTPUT", "SHARED_KNOWLEDGE",
                              "RELEVANT_FILES", "RELEVANT_CONFIG", "RELEVANT_TESTS", "REPOSITORY_STATE"],
    "CRITICAL": ["OBJECTIVE_SUMMARY", "DEPENDENCY_OUTPUT", "SHARED_KNOWLEDGE", "RELEVANT_FILES",
                 "RELEVANT_CONFIG", "RELEVANT_TESTS", "REPOSITORY_STATE"],
}
NON_INTELLIGENCE_CATEGORIES = tuple(c for c in knowledge.FAILURE_CATEGORIES
                                    if c != "REASONING_INSUFFICIENT")

# Complete task contract (Execution Contract Stabilization V0.1). One explicit,
# bounded contract per executable node preserves the task-specific meaning the worker
# needs; it never forwards the raw master prompt and never stores raw chat.
CONTRACT_VERSION = "task-contract-v1"

# Deterministic clause boundaries for assigning task-specific objective slices.
_CLAUSE_RE = re.compile(r"[;,]|\band\b", re.IGNORECASE)


def _objective_fingerprint(objective: str) -> str:
    """Stable identity of the whole master objective (never stored as raw text)."""
    canonical = " ".join((objective or "").split())
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:32]


def _clauses(objective: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_RE.split(objective or "") if part.strip()]


class _Slicer:
    """Assigns each work stage the bounded master clause that matches it (once)."""

    _CUES = {
        "implement": None,  # mutation verbs are checked structurally
        "architecture": ("audit", "architecture", "cross-machine", "multi-machine", "review",
                         "analy", "analyze", "inspect", "trace", "examine", "configured",
                         "configuration", "resolver", "precedence", "migration", "concurrency"),
        "security": ("security", "authentication", "authorization", "auth"),
        "docs": ("doc", "docs", "documentation", "readme"),
        "tests": ("test", "tests", "verify", "verif", "validate"),
        "final": ("report", "final", "summar"),
    }

    def __init__(self, objective: str) -> None:
        self.clauses = _clauses(objective)
        self.used: set[int] = set()

    def for_kind(self, kind: str, fallback: str) -> str:
        cues = self._CUES.get(kind)
        for index, clause in enumerate(self.clauses):
            if index in self.used:
                continue
            low = clause.lower()
            match = has_mutation(clause) if kind == "implement" else any(cue in low for cue in (cues or ()))
            if match:
                self.used.add(index)
                return clause
        return fallback

    def remaining(self) -> list[str]:
        """Master clauses no work stage claimed. They must not silently disappear."""
        return [clause for index, clause in enumerate(self.clauses) if index not in self.used]


# Deterministic-coverage vocabulary: tokens a deterministic operation legitimately
# accounts for, so an objective is only treated as fully deterministic when nothing
# requirement-bearing is left unexplained. Conservative; unknown tokens block it.
_OP_EVIDENCE = {
    "repository_state": ("repository", "repo", "branch", "head", "commit", "sha", "hash",
                         "clean", "dirty", "state", "status", "working", "tree", "worktree"),
    "git_head_matches": ("head", "commit", "sha", "hash", "equals", "matches", "is"),
    "config_value_matches": ("config", "value", "key", "equals", "matches"),
    "file_exists": ("exist", "exists", "file"),
    "config_parse": ("config", "parse", "yaml", "yml", "json", "toml"),
    "run_tests": ("test", "tests", "suite", "unit_tests"),
}
_COVERAGE_FILLER = frozenset((
    "report", "check", "verify", "inspect", "review", "show", "list", "read", "current", "and",
    "or", "the", "a", "an", "is", "are", "of", "in", "on", "to", "for", "with", "please",
    "deterministic", "final", "result", "results", "whether", "if", "does", "do", "run", "also",
    "then", "state", "given", "that", "this", "as", "by", "at", "equals", "matches",
))
_TOKEN_RE = re.compile(r"[a-z0-9_./-]+")


def _tokens(text: str) -> list[str]:
    """Requirement-bearing tokens; surrounding punctuation (e.g. a trailing period) is
    stripped while internal '.', '/', '-' used by file paths are preserved."""
    return [token.strip(".-/") for token in _TOKEN_RE.findall((text or "").lower()) if token.strip(".-/")]


def _coverage_tokens(tasks: list[dict[str, Any]]) -> set[str]:
    covered: set[str] = set(_COVERAGE_FILLER)
    for task in tasks:
        covered.update(_OP_EVIDENCE.get(task["operation"], ()))
        for value in (task.get("args") or {}).values():
            covered.update(_tokens(str(value)))
    return covered


def _deterministic_covers(objective: str, tasks: list[dict[str, Any]]) -> bool:
    """A pure deterministic plan is only claimed when EVERY requirement-bearing token in
    the objective is accounted for by the recognized deterministic operations. Anything
    left (e.g. arithmetic, prose generation) means the objective is NOT fully
    deterministic and must not be reported as a completed zero-AI task."""
    if not tasks:
        return False
    covered = _coverage_tokens(tasks)
    return all(token in covered for token in _tokens(objective))


def _requirement_covered(clause: str, nodes: list[dict[str, Any]]) -> bool:
    """A master clause is covered when an AI stage carries it verbatim, or every
    requirement-bearing token is accounted for by a deterministic node's operation."""
    normalized = " ".join((clause or "").lower().split())
    if not normalized:
        return True
    for node in nodes:
        if node.get("executor") == "TOOL_ONLY":
            continue
        if normalized in " ".join((node.get("objective_summary") or "").lower().split()):
            return True
    tool_tasks = [{"operation": (node.get("tool") or {}).get("operation"), "args": (node.get("tool") or {}).get("args")}
                  for node in nodes if node.get("executor") == "TOOL_ONLY" and (node.get("tool") or {}).get("operation")]
    covered = _coverage_tokens([t for t in tool_tasks if t["operation"] in _OP_EVIDENCE])
    return all(token in covered for token in _tokens(normalized))


# Execution intent (never inferred from loose substrings such as "config"/"configured").
INTENT_READ_ONLY = "READ_ONLY"
INTENT_MUTATING = "MUTATING"
INTENT_MIXED = "MIXED"
INTENTS = (INTENT_READ_ONLY, INTENT_MUTATING, INTENT_MIXED)

_READONLY_CUES = ("inspect", "show", "verify", "review", "check", "list", "report", "analyze",
                  "analyse", "audit", "read", "describe", "summarize", "summarise", "identify",
                  "name", "state", "explain", "trace", "confirm", "examine")
_MUTATION_VERBS = ("modify", "change", "implement", "fix", "update", "add", "remove", "delete",
                   "refactor", "rename", "patch", "write", "edit", "configure", "migrate",
                   "replace", "create", "apply", "install", "enable", "disable", "set", "bump",
                   "upgrade", "downgrade", "align", "introduce", "restore")
# Negated mutation phrases ("do not modify", "without changing") never imply mutation.
_NEGATION_RE = re.compile(
    r"\b(?:do not|don't|dont|without|never|no)\s+(?:\w+\s+){0,2}?"
    r"(?:modify|change|edit|write|create|delete|remove|update|touch|alter)\b", re.IGNORECASE)


def _strip_negations(text: str) -> str:
    return _NEGATION_RE.sub(" ", text or "")


def has_mutation(text: str) -> bool:
    text = _strip_negations(text).lower()
    return any(re.search(r"\b" + re.escape(verb) + r"\b", text) for verb in _MUTATION_VERBS)


def has_readonly_cue(text: str) -> bool:
    text = (text or "").lower()
    return any(re.search(r"\b" + re.escape(cue) + r"\b", text) for cue in _READONLY_CUES)


def execution_intent(objective: str, *, read_only: bool = False) -> str:
    """READ_ONLY / MUTATING / MIXED. `read_only=True` forces READ_ONLY (hard policy)."""
    if read_only:
        return INTENT_READ_ONLY
    mutation = has_mutation(objective)
    readonly = has_readonly_cue(objective)
    if mutation and readonly:
        return INTENT_MIXED
    return INTENT_MUTATING if mutation else INTENT_READ_ONLY


def node_intent(title: str) -> str:
    return INTENT_MUTATING if has_mutation(title) else INTENT_READ_ONLY


# Planner modes: NONE (no planning needed), DETERMINISTIC (rule-based), AI (planner call).
PLANNER_NONE = "NONE"
PLANNER_DETERMINISTIC = "DETERMINISTIC"
PLANNER_AI = "AI"

_AI_TERMS = ("architecture", "security", "review", "design", "debug", "root cause", "concurrency",
             "persistence", "migration", "decide", "integrat", "refactor", "implement", "fix",
             "optimi", "diagnos", "ambigu", "unknown", "unclear", "underspecified",
             "explain", "describe", "summariz", "summaris", "walk through")
_PLANNER_AI_TERMS = ("security-sensitive", "ambiguous", "multiple architectures", "unknown architecture",
                     "unclear", "underspecified", "root cause", "major architecture")


def _needs_ai(objective: str) -> bool:
    text = (objective or "").lower()
    return has_mutation(objective) or any(term in text for term in _AI_TERMS)


def _planner_required(objective: str) -> bool:
    text = (objective or "").lower()
    return any(term in text for term in _PLANNER_AI_TERMS)


_SYNTHESIS_TERMS = ("synthesize", "synthesis", "architecture decision", "decide", "recommend",
                    "judge", "weigh", "trade-off", "tradeoff")


def _requires_synthesis(objective: str) -> bool:
    text = (objective or "").lower()
    return any(term in text for term in _SYNTHESIS_TERMS)


def planner_decision(objective: str, *, constraints: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Choose the cheapest sufficient planner: none, deterministic, or AI (required).

    Deterministic objectives bypass planning entirely. A single obvious AI task routes
    directly with no planner. Ambiguous/security objectives report PLANNER_AI.
    """
    deterministic_tasks = deterministic_tools.classify_objective(objective)
    planner = classify_planner(objective)
    if deterministic_tasks and not _needs_ai(objective):
        return {"mode": PLANNER_NONE, "required_tier": None, "required_effort": None,
                "rationale": "deterministic objective; TOOL_ONLY execution, no planner"}
    if _needs_ai(objective) and _planner_required(objective):
        return {"mode": PLANNER_AI, "required_tier": "MAX", "required_effort": "max",
                "rationale": "ambiguous/security decomposition warrants an AI planner (PLANNER_REQUIRED)"}
    if _needs_ai(objective):
        return {"mode": PLANNER_NONE, "required_tier": planner["required_tier"],
                "required_effort": planner["required_effort"],
                "rationale": "single obvious AI task; route directly without a planner"}
    return {"mode": PLANNER_DETERMINISTIC, "required_tier": planner["required_tier"],
            "required_effort": planner["required_effort"], "rationale": "rule-based decomposition"}


def _deterministic_plan(objective: str, constraints: dict[str, Any], decision: dict[str, Any]) -> Optional[dict[str, Any]]:
    tasks = deterministic_tools.classify_objective(objective)
    if not tasks:
        return None
    if not _deterministic_covers(objective, tasks):
        # The objective carries requirements the deterministic operations do not fully
        # account for (e.g. "check HEAD and calculate 2 plus 2"): never report a partial
        # zero-AI completion. Fall through to mixed/intact AI handling.
        return None
    nodes: list[dict[str, Any]] = []
    for index, task in enumerate(tasks, start=1):
        node = _node(f"D{index}", f"Deterministic {task['operation']}", "MECHANICAL", [],
                     acceptance=[f"{task['operation']} check satisfied"], family="deterministic_tool")
        node["executor"] = "TOOL_ONLY"
        node["tool"] = {"operation": task["operation"], "args": task["args"]}
        node["routing"] = None
        nodes.append(node)
    final = _node("DF", "Deterministic final verification", "MECHANICAL",
                  [node["task_id"] for node in nodes],
                  acceptance=["all deterministic checks satisfied"], family="deterministic_tool")
    final["executor"] = "TOOL_ONLY"
    final["tool"] = {"operation": "finalize", "args": {}}
    final["routing"] = None
    nodes.append(final)
    plan = {"schema_version": 1, "objective_summary": _bounded_summary(objective),
            "planner": {"required_tier": None, "required_effort": None},
            "planner_mode": decision, "nodes": nodes, "order": [node["task_id"] for node in nodes],
            "valid": True, "diagnostics": [],
            "intent": execution_intent(objective, read_only=bool(constraints.get("read_only")))}
    plan["routing_mix"] = {tier: 0 for tier in TIER_ORDER}
    plan["executor_mix"] = {"TOOL_ONLY": len(nodes), "AI": 0}
    plan["quota_view"] = {"nodes": len(nodes), "expensive_nodes": 0, "cheap_nodes": 0,
                          "conditional_expensive_nodes": 0, "full_context_nodes": 0}
    _attach_budget(plan, constraints)
    _attach_contracts(plan, objective)
    return plan


def _attach_budget(plan: dict[str, Any], constraints: dict[str, Any]) -> None:
    nodes = plan["nodes"]
    tool_only = sum(1 for node in nodes if node.get("executor") == "TOOL_ONLY")
    direct_ai = sum(1 for node in nodes if node.get("executor") != "TOOL_ONLY")
    conditional_ai = sum(1 for node in nodes if node.get("conditional") and node.get("executor") != "TOOL_ONLY")
    planner_calls = 1 if plan.get("planner_mode", {}).get("mode") == PLANNER_AI else 0
    minimum_required = direct_ai + planner_calls
    hard_max = constraints.get("max_ai_calls")
    plan["planning_budget"] = {
        "tool_only_nodes": tool_only, "direct_ai_nodes": direct_ai, "planner_ai_calls": planner_calls,
        "conditional_ai_nodes": conditional_ai, "possible_retry_calls": direct_ai,
        "minimum_required_ai_calls": minimum_required, "planned_ai_calls": direct_ai + planner_calls,
        "hard_max_ai_calls": hard_max,
    }
    if hard_max is not None and minimum_required > hard_max:
        plan.setdefault("diagnostics", []).append(
            f"INSUFFICIENT_AI_CALL_BUDGET: minimum required {minimum_required} exceeds hard max {hard_max}")
        plan["valid"] = False


class OrchestratorError(RuntimeError):
    pass


def classify_planner(objective: str) -> dict[str, str]:
    text = (objective or "").lower()
    if any(word in text for word in ("security", "authentication", "authorization", "auth architecture")):
        return {"required_tier": "MAX", "required_effort": "max"}
    if any(word in text for word in ("architecture", "cross-machine", "multi-machine", "resolver",
                                     "precedence", "migration", "concurrency", "graphify")):
        return {"required_tier": "STRONG", "required_effort": "high"}
    return {"required_tier": "BALANCED", "required_effort": "medium"}


def _signals(objective: str) -> dict[str, bool]:
    text = (objective or "").lower()
    return {
        "architecture": any(w in text for w in ("architecture", "cross-machine", "multi-machine",
                                                "config", "resolver", "graphify", "precedence",
                                                "schema", "migration", "concurrency")),
        "security": any(w in text for w in ("security", "authentication", "authorization",
                                            "seauth", "auth architecture")),
        "implement": has_mutation(objective),
        "docs": any(w in text for w in ("doc", "docs", "documentation", "readme")),
        "tests": any(w in text for w in ("test", "tests", "verify", "validate")),
    }


def _node(task_id: str, title: str, task_class: str, depends_on: list[str], *,
          conditional: bool = False, risk_signals: Optional[list[str]] = None,
          acceptance: Optional[list[str]] = None, produces: Optional[list[str]] = None,
          consumes: Optional[list[str]] = None, family: Optional[str] = None) -> dict[str, Any]:
    return {
        "task_id": task_id, "title": title, "task_family": family or knowledge.task_family_for(title),
        "task_class": task_class, "objective_summary": title, "depends_on": list(depends_on),
        "conditional": conditional, "status": "planned", "intent": node_intent(title),
        "executor": "AI",
        "risk_signals": list(risk_signals or []),
        "acceptance_criteria": list(acceptance or []),
        "produces_artifacts": list(produces or []), "consumes_results_from": list(consumes or depends_on),
    }


def decompose(objective: str, *, constraints: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Build a small, valid task graph from one objective. Deterministic; no model."""
    constraints = constraints or {}
    decision = planner_decision(objective, constraints=constraints)
    if decision["mode"] == PLANNER_NONE and not _needs_ai(objective):
        deterministic_plan = _deterministic_plan(objective, constraints, decision)
        if deterministic_plan is not None:
            return deterministic_plan
    sig = _signals(objective)
    slicer = _Slicer(objective)
    multi = (sum(1 for key in ("implement", "architecture", "security", "docs", "tests") if sig[key]) >= 2
             or decision["mode"] == PLANNER_AI or _requires_synthesis(objective))
    nodes: list[dict[str, Any]] = []
    nodes.append(_node("T1", "Inspect repository and gather current state", "MECHANICAL", [],
                       acceptance=["repository state summarized", "relevant files identified"],
                       produces=["repo_state_summary"], family="repository_inspection"))
    # Deterministic-first mixed decomposition: recognize explicit deterministic sub-work.
    deterministic_tasks = deterministic_tools.classify_objective(objective)
    if not constraints.get("deterministic_tests"):
        deterministic_tasks = [task for task in deterministic_tasks if task["operation"] != "run_tests"]
    repo_state = next((task for task in deterministic_tasks if task["operation"] == "repository_state"), None)
    deterministic_tasks = [task for task in deterministic_tasks if task is not repo_state]
    if repo_state is not None:
        nodes[0]["executor"] = "TOOL_ONLY"
        nodes[0]["tool"] = {"operation": "repository_state", "args": {}}
        nodes[0]["routing"] = None
        nodes[0]["acceptance_criteria"] = ["repository state determined deterministically"]

    # Simple intact AI objective: keep ONE bounded task that still carries the actual
    # requirement instead of forcing a generic inspect -> implement -> report split.
    if not multi and not deterministic_tasks and repo_state is None:
        return _single_task_plan(objective, sig, decision, constraints)

    last_analysis = "T1"
    if sig["architecture"]:
        nodes.append(_node("T2", "Analyze architecture / cross-machine behavior", "CORRECTNESS_SENSITIVE", ["T1"],
                           risk_signals=["cross-machine", "architecture"],
                           acceptance=["architecture decision documented", "affected files identified"],
                           produces=["architecture_decision"], family="multi_machine_acceptance"))
        last_analysis = "T2"
    if sig["security"]:
        nodes.append(_node("T2S", "Security-sensitive correctness review", "CRITICAL", [last_analysis],
                           risk_signals=["security"],
                           acceptance=["security risks identified", "no unresolved critical finding"],
                           produces=["security_review"], family="security_sensitive_design"))
        last_analysis = "T2S"

    last_change = last_analysis
    if sig["implement"]:
        nodes.append(_node("T3", "Implement the bounded change", "NORMAL", [last_analysis],
                           acceptance=["requested change present", "no unintended diff"],
                           produces=["change_summary"], family="configuration_resolution"))
        last_change = "T3"
        if sig["architecture"] or sig["security"]:
            nodes.append(_node("T3E", "Resolve architecture ambiguity (conditional)", "CORRECTNESS_SENSITIVE",
                               ["T3"], conditional=True, risk_signals=["conditional-escalation"],
                               acceptance=["ambiguity resolved or escalated"], produces=["resolution_summary"],
                               family="configuration_resolution"))

    if sig["docs"]:
        nodes.append(_node("T4", "Update documentation", "MECHANICAL", [last_analysis, last_change],
                           acceptance=["target docs updated"], produces=["docs_summary"],
                           family="report_recovery_validation"))
    if sig["tests"]:
        nodes.append(_node("T5", "Run focused tests and validation", "NORMAL", [last_change],
                           acceptance=["focused tests pass"], produces=["test_summary"],
                           family="multi_machine_acceptance"))

    for index, task in enumerate(deterministic_tasks, start=1):
        det = _node(f"D{index}", f"Deterministic {task['operation']}", "MECHANICAL", [],
                    acceptance=[f"{task['operation']} check satisfied"], family="deterministic_tool")
        det["executor"] = "TOOL_ONLY"
        det["tool"] = {"operation": task["operation"], "args": task["args"]}
        det["routing"] = None
        nodes.append(det)

    if len(nodes) > 1:
        depended_on = {dep for node in nodes for dep in node["depends_on"]}
        leaves = sorted(node["task_id"] for node in nodes if node["task_id"] not in depended_on)
        final = _node("T6", "Final verification and integration report", "NORMAL", leaves,
                      acceptance=["upstream acceptance criteria checked", "unresolved blockers reported"],
                      produces=["final_report"], family="report_recovery_validation")
        has_ai = any(node.get("executor") == "AI" for node in nodes)
        if (repo_state is not None or deterministic_tasks) and not (_requires_synthesis(objective) and has_ai):
            final["executor"] = "TOOL_ONLY"
            final["tool"] = {"operation": "finalize", "args": {}}
            final["routing"] = None
            final["acceptance_criteria"] = ["all deterministic acceptance satisfied"]
        nodes.append(final)

    covered = _assign_task_slices(nodes, slicer)
    if not covered or not all(_requirement_covered(clause, nodes) for clause in _clauses(objective)):
        # Coverage cannot be proven without silently dropping a requirement: keep the
        # whole objective intact in one bounded AI task instead of a lossy decomposition.
        return _single_task_plan(objective, sig, decision, constraints)
    plan = _finish_plan(objective, nodes, decision, constraints)
    _attach_contracts(plan, objective)
    return plan


_SLICE_KIND = {"T2": "architecture", "T2S": "security", "T3": "implement", "T3E": "architecture",
               "T4": "docs", "T5": "tests", "T6": "final"}
_SLICE_OPERATION = {"T1": "inspect", "T2": "analyze", "T2S": "review", "T3": "implement",
                    "T3E": "resolve", "T4": "document", "T5": "verify", "T6": "finalize",
                    "D": "check"}


def _assign_task_slices(nodes: list[dict[str, Any]], slicer: "_Slicer") -> bool:
    """Give each AI stage the bounded master clause that matches its work, so distinct
    objectives with different requirements cannot collapse into one generic task.

    Returns True when every master clause is covered: clauses a stage claimed, plus any
    remaining clause absorbed by a primary AI stage. Returns False when a requirement
    cannot be represented (no AI stage to absorb it), so the caller keeps the objective
    intact instead of silently dropping it."""
    for node in nodes:
        if node.get("executor") == "TOOL_ONLY":
            continue
        kind = _SLICE_KIND.get(node["task_id"])
        if kind is None:
            continue
        node["objective_summary"] = slicer.for_kind(kind, node["objective_summary"])
    leftover = slicer.remaining()
    ai_nodes = [node for node in nodes if node.get("executor") != "TOOL_ONLY"]
    if not leftover:
        return True
    target = next((node for node in ai_nodes if node["task_id"] == "T3"), None)
    if target is None and ai_nodes:
        target = ai_nodes[-1]
    if target is None:
        return False
    target["objective_summary"] = target["objective_summary"] + "; " + "; ".join(leftover)
    return True


def _single_task_plan(objective: str, sig: dict[str, bool], decision: dict[str, Any],
                      constraints: dict[str, Any]) -> dict[str, Any]:
    objective_summary = _bounded_summary(objective, 200)
    task_class = "CRITICAL" if sig["security"] else (
        "CORRECTNESS_SENSITIVE" if sig["architecture"] else "NORMAL")
    node = _node("T1", objective_summary, task_class, [],
                 acceptance=["the stated task's requested outcome is reached"],
                 produces=["task_result"], family=knowledge.task_family_for(objective_summary))
    node["objective_summary"] = objective_summary
    plan = _finish_plan(objective, [node], decision, constraints)
    _attach_contracts(plan, objective)
    return plan


def _finish_plan(objective: str, nodes: list[dict[str, Any]], decision: dict[str, Any],
                 constraints: dict[str, Any]) -> dict[str, Any]:
    plan = {"schema_version": 1, "objective_summary": _bounded_summary(objective),
            "planner": classify_planner(objective), "planner_mode": decision, "nodes": nodes,
            "intent": execution_intent(objective, read_only=bool(constraints.get("read_only")))}
    _apply_routing_and_context(plan, constraints)
    valid, diagnostics, order = validate_graph(plan["nodes"])
    conflicts = _intent_conflicts(plan["nodes"], plan["intent"])
    plan["valid"] = valid and not conflicts
    plan["order"] = order
    plan["diagnostics"] = diagnostics + conflicts + quota_guards(plan)
    plan["routing_mix"] = _routing_mix(plan["nodes"])
    plan["quota_view"] = _quota_view(plan["nodes"])
    tool_only = sum(1 for node in nodes if node.get("executor") == "TOOL_ONLY")
    plan["executor_mix"] = {"TOOL_ONLY": tool_only, "AI": len(nodes) - tool_only}
    _attach_budget(plan, constraints)
    return plan


def _contract_targets(node: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for key in ("context_refs", "config_refs", "symbol_refs"):
        for ref in node.get(key) or []:
            value = ref.get("path") if isinstance(ref, dict) else None
            if value and value not in targets:
                targets.append(value)
    return targets


def _attach_contracts(plan: dict[str, Any], objective: str) -> None:
    """Attach the one explicit COMPLETE TASK CONTRACT each executable node uses."""
    master = _objective_fingerprint(objective)
    for node in plan["nodes"]:
        routing_c = node.get("routing") or {}
        node["task_contract"] = {
            "contract_version": CONTRACT_VERSION,
            "task_id": node["task_id"],
            "objective": node["objective_summary"],
            "master_fingerprint": master,
            "operation": _SLICE_OPERATION.get(node["task_id"],
                                              (node.get("tool") or {}).get("operation") or "execute"),
            "targets": _contract_targets(node),
            "constraints": list(constraints_list(plan)),
            "acceptance": list(node.get("acceptance_criteria") or []),
            "permissions": node.get("intent"),
            "dependencies": list(node.get("depends_on") or []),
            "context_budget": (node.get("context") or {}).get("budget"),
            "capability_requirement": {"tier": routing_c.get("tier"), "effort": routing_c.get("effort")},
        }


def constraints_list(plan: dict[str, Any]) -> list[str]:
    return [f"intent:{plan.get('intent')}"]


def _intent_conflicts(nodes: list[dict[str, Any]], intent: str) -> list[str]:
    if intent != INTENT_READ_ONLY:
        return []
    return [f"READ_ONLY_MUTATION_CONFLICT: {node['task_id']} '{node['title']}' is a mutating node "
            f"under READ_ONLY intent" for node in nodes if node["intent"] == INTENT_MUTATING]


def _bounded_summary(objective: str, limit: int = 120) -> str:
    text = " ".join((objective or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _apply_routing_and_context(plan: dict[str, Any], constraints: dict[str, Any]) -> None:
    surface = constraints.get("surface")
    for node in plan["nodes"]:
        if node.get("executor") == "TOOL_ONLY":
            continue
        recommendation = routing.recommend(node["objective_summary"], task_class=node["task_class"],
                                           requested_tier=constraints.get("min_tier"))
        recommendation["task_family"] = node["task_family"]
        knowledge.apply_learned_default(recommendation)
        tier, effort, conflicts = _apply_constraints(recommendation["recommended_tier"],
                                                     recommendation["recommended_effort"],
                                                     node["task_class"], constraints)
        node["routing"] = {"tier": tier, "effort": effort, "role": _role_for(tier)}
        node["routing_constraints"] = conflicts
        node["historical_evidence"] = len(knowledge.observations_for_family(node["task_family"]))
        node["knowledge_refs"] = [lesson["lesson_id"]
                                  for lesson in knowledge.relevant_lessons(node["objective_summary"])]
        if surface:
            resolution = discovery.capability(recommendation.get("provider"), recommendation.get("model"),
                                              surface)
            node["model_capability"] = {"status": resolution["status"], "surface": surface}
        node["context"] = context_plan(node, recommendation)


def _role_for(tier: str) -> Optional[str]:
    return routing.load_policy()["tiers"].get(tier, {}).get("role")


def _apply_constraints(tier: str, effort: str, task_class: str,
                       constraints: dict[str, Any]) -> tuple[str, str, list[str]]:
    conflicts: list[str] = []
    floor = knowledge.safety_floor(task_class)
    cap = constraints.get("max_tier")
    if constraints.get("local_only") and cap is None:
        cap = "BALANCED"
    if constraints.get("no_premium") and cap is None:
        cap = "STRONG"
    if cap:
        cap_index = TIER_ORDER.index(cap)
        floor_index = TIER_ORDER.index(floor) if floor in TIER_ORDER else 0
        if cap_index < TIER_ORDER.index(tier):
            if cap_index < floor_index:
                conflicts.append(f"requested cap {cap} is below the {task_class} safety floor {floor}; floor wins")
                tier = floor
            else:
                tier = cap
    if constraints.get("min_tier") and TIER_ORDER.index(constraints["min_tier"]) > TIER_ORDER.index(tier):
        tier = constraints["min_tier"]
    effort = routing.load_policy()["tiers"].get(tier, {}).get("effort", effort)
    return tier, effort, conflicts


def context_plan(node: dict[str, Any], recommendation: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Minimum sufficient context: never full chat/history, never the whole repository."""
    task_class = node["task_class"]
    budget = DEFAULT_BUDGET.get(task_class, "SMALL")
    if node["task_id"] == "T1" and not node["depends_on"]:
        budget = "TINY"
    if "needs-large-context" in node.get("risk_signals", []):
        budget = "LARGE"
    include = list(BASE_CONTEXT.get(task_class, BASE_CONTEXT["NORMAL"]))
    manifest = [f"dependency:{dep} output summary" for dep in node["depends_on"]]
    manifest += [f"knowledge:{ref}" for ref in node.get("knowledge_refs", [])]
    return {"budget": budget, "include": include, "excluded": list(EXCLUDED_CATEGORIES),
            "manifest": manifest}


def validate_graph(nodes: list[dict[str, Any]]) -> tuple[bool, list[str], list[str]]:
    """Return (valid, diagnostics, topological_order). Rejects cycles/self/dangling deps."""
    diagnostics: list[str] = []
    ids = [node["task_id"] for node in nodes]
    if len(ids) != len(set(ids)):
        return False, ["duplicate task_id"], []
    known = set(ids)
    graph: dict[str, list[str]] = {}
    for node in nodes:
        deps = node["depends_on"]
        if node["task_id"] in deps:
            diagnostics.append(f"{node['task_id']} depends on itself")
        missing = [dep for dep in deps if dep not in known]
        if missing:
            diagnostics.append(f"{node['task_id']} depends on unknown {', '.join(missing)}")
        graph[node["task_id"]] = [dep for dep in deps if dep in known]
    if diagnostics:
        return False, diagnostics, []
    order: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return False
        if task_id in visited:
            return True
        visiting.add(task_id)
        for dep in graph[task_id]:
            if not visit(dep):
                return False
        visiting.discard(task_id)
        visited.add(task_id)
        order.append(task_id)
        return True

    for task_id in ids:
        if not visit(task_id):
            return False, ["dependency cycle detected"], []
    return True, diagnostics, order


def quota_guards(plan: dict[str, Any]) -> list[str]:
    nodes = plan["nodes"]
    ai_nodes = [node for node in nodes if node.get("routing")]
    warnings: list[str] = []
    tiers = [node["routing"]["tier"] for node in ai_nodes]
    if len(nodes) > MAX_PLAN_NODES:
        warnings.append(f"EXCESSIVE_TASK_SPLITTING: {len(nodes)} nodes exceeds {MAX_PLAN_NODES}")
    if len(ai_nodes) > 1 and all(tier == "STRONG" for tier in tiers) and len(tiers) == len(ai_nodes):
        warnings.append("ALL_TASKS_STRONG: every AI node is STRONG")
    if tiers and all(tier == "MAX" for tier in tiers):
        warnings.append("ALL_TASKS_MAX: every AI node is MAX")
    budgets = [(node.get("context") or {}).get("budget") for node in ai_nodes]
    if budgets and all(budget == "LARGE" for budget in budgets):
        warnings.append("ALL_CONTEXT_LARGE: every node requests LARGE context")
    for node in nodes:
        if not node["acceptance_criteria"]:
            warnings.append(f"EMPTY_ACCEPTANCE_CRITERIA: {node['task_id']}")
        routing = node.get("routing")
        if routing and routing["tier"] in ("STRONG", "MAX") and node["task_class"] in ("MECHANICAL", "NORMAL") \
                and not node.get("risk_signals"):
            warnings.append(f"UNJUSTIFIED_EXPENSIVE_NODE: {node['task_id']} is {routing['tier']} "
                            f"for a {node['task_class']} task without risk signals")
    return warnings


def _routing_mix(nodes: list[dict[str, Any]]) -> dict[str, int]:
    mix = {tier: 0 for tier in TIER_ORDER}
    for node in nodes:
        routing = node.get("routing")
        if routing:
            mix[routing["tier"]] = mix.get(routing["tier"], 0) + 1
    return mix


def _quota_view(nodes: list[dict[str, Any]]) -> dict[str, int]:
    ai = [node for node in nodes if node.get("routing")]
    return {
        "nodes": len(nodes),
        "expensive_nodes": sum(1 for node in ai if node["routing"]["tier"] in ("STRONG", "MAX")),
        "cheap_nodes": sum(1 for node in ai if node["routing"]["tier"] in ("FAST", "BALANCED")),
        "conditional_expensive_nodes": sum(1 for node in ai if node["conditional"]
                                           and node["routing"]["tier"] in ("STRONG", "MAX")),
        "full_context_nodes": sum(1 for node in nodes if (node.get("context") or {}).get("budget") == "LARGE"),
    }


def render_plan(plan: dict[str, Any]) -> str:
    lines = ["PLAN", f"OBJECTIVE: {plan['objective_summary']}",
             f"INTENT: {plan.get('intent')}",
             f"PLANNER: {plan['planner']['required_tier']} / {plan['planner']['required_effort']}  "
             f"mode={plan.get('planner_mode', {}).get('mode')}"]
    for node in plan["nodes"]:
        deps = ",".join(node["depends_on"]) or "none"
        suffix = " (conditional)" if node["conditional"] else ""
        if node.get("executor") == "TOOL_ONLY":
            operation = (node.get("tool") or {}).get("operation")
            lines.append(f"{node['task_id']}{suffix}  {node['title']}  [TOOL_ONLY:{operation}]  depends {deps}")
            continue
        routing = node["routing"]
        lines.append(f"{node['task_id']}{suffix}  {node['title']}  [{node['task_class']}]  "
                     f"route {routing['tier']}/{routing['effort']}  context {node['context']['budget']}  "
                     f"depends {deps}")
    mix = plan["routing_mix"]
    lines.append("ROUTING MIX: " + "  ".join(f"{tier}:{mix[tier]}" for tier in TIER_ORDER))
    view = plan["quota_view"]
    lines.append(f"QUOTA VIEW: nodes {view['nodes']}, expensive {view['expensive_nodes']}, "
                 f"cheap {view['cheap_nodes']}, conditional-expensive {view['conditional_expensive_nodes']}, "
                 f"large-context {view['full_context_nodes']}")
    if plan.get("planning_budget"):
        budget = plan["planning_budget"]
        lines.append(f"PLANNING BUDGET: tool_only {budget['tool_only_nodes']}, direct_ai {budget['direct_ai_nodes']}, "
                     f"planner_ai {budget['planner_ai_calls']}, min_required_ai {budget['minimum_required_ai_calls']}, "
                     f"hard_max {budget['hard_max_ai_calls']}")
    if plan["diagnostics"]:
        lines.append("WARNINGS: " + "; ".join(plan["diagnostics"]))
    return "\n".join(lines)
