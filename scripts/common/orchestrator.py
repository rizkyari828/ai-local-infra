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

from typing import Any, Optional

import routing  # scripts/common, on sys.path
import derived_knowledge as knowledge  # scripts/common, on sys.path
import discovery  # scripts/common, on sys.path

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
        "implement": any(w in text for w in ("fix", "implement", "change", "add", "update", "refactor",
                                             "patch", "configure", "align")),
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
        "conditional": conditional, "status": "planned",
        "risk_signals": list(risk_signals or []),
        "acceptance_criteria": list(acceptance or []),
        "produces_artifacts": list(produces or []), "consumes_results_from": list(consumes or depends_on),
    }


def decompose(objective: str, *, constraints: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Build a small, valid task graph from one objective. Deterministic; no model."""
    constraints = constraints or {}
    sig = _signals(objective)
    nodes: list[dict[str, Any]] = []
    nodes.append(_node("T1", "Inspect repository and gather current state", "MECHANICAL", [],
                       acceptance=["repository state summarized", "relevant files identified"],
                       produces=["repo_state_summary"], family="repository_inspection"))

    last_analysis = "T1"
    arch_class = "CRITICAL" if sig["security"] else "CORRECTNESS_SENSITIVE"
    if sig["architecture"]:
        nodes.append(_node("T2", "Analyze architecture / cross-machine behavior", arch_class, ["T1"],
                           risk_signals=["cross-machine", "architecture"],
                           acceptance=["architecture decision documented", "affected files identified"],
                           produces=["architecture_decision"], family="multi_machine_acceptance"))
        last_analysis = "T2"

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

    if len(nodes) > 1:
        depended_on = {dep for node in nodes for dep in node["depends_on"]}
        leaves = sorted(node["task_id"] for node in nodes if node["task_id"] not in depended_on)
        nodes.append(_node("T6", "Final verification and integration report", "NORMAL", leaves,
                           acceptance=["upstream acceptance criteria checked", "unresolved blockers reported"],
                           produces=["final_report"], family="report_recovery_validation"))

    plan = {"schema_version": 1, "objective_summary": _bounded_summary(objective),
            "planner": classify_planner(objective), "nodes": nodes}
    _apply_routing_and_context(plan, constraints)
    valid, diagnostics, order = validate_graph(plan["nodes"])
    plan["valid"] = valid
    plan["order"] = order
    plan["diagnostics"] = diagnostics + quota_guards(plan)
    plan["routing_mix"] = _routing_mix(plan["nodes"])
    plan["quota_view"] = _quota_view(plan["nodes"])
    return plan


def _bounded_summary(objective: str, limit: int = 120) -> str:
    text = " ".join((objective or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _apply_routing_and_context(plan: dict[str, Any], constraints: dict[str, Any]) -> None:
    surface = constraints.get("surface")
    for node in plan["nodes"]:
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
    warnings: list[str] = []
    tiers = [node["routing"]["tier"] for node in nodes]
    if len(nodes) > MAX_PLAN_NODES:
        warnings.append(f"EXCESSIVE_TASK_SPLITTING: {len(nodes)} nodes exceeds {MAX_PLAN_NODES}")
    if len(nodes) > 1 and all(tier == "STRONG" for tier in tiers):
        warnings.append("ALL_TASKS_STRONG: every node is STRONG")
    if tiers and all(tier == "MAX" for tier in tiers):
        warnings.append("ALL_TASKS_MAX: every node is MAX")
    budgets = [node["context"]["budget"] for node in nodes]
    if budgets and all(budget == "LARGE" for budget in budgets):
        warnings.append("ALL_CONTEXT_LARGE: every node requests LARGE context")
    for node in nodes:
        if node["routing"]["tier"] in ("STRONG", "MAX") and node["task_class"] in ("MECHANICAL", "NORMAL") \
                and not node.get("risk_signals"):
            warnings.append(f"UNJUSTIFIED_EXPENSIVE_NODE: {node['task_id']} is {node['routing']['tier']} "
                            f"for a {node['task_class']} task without risk signals")
        if not node["acceptance_criteria"]:
            warnings.append(f"EMPTY_ACCEPTANCE_CRITERIA: {node['task_id']}")
    return warnings


def _routing_mix(nodes: list[dict[str, Any]]) -> dict[str, int]:
    mix = {tier: 0 for tier in TIER_ORDER}
    for node in nodes:
        mix[node["routing"]["tier"]] = mix.get(node["routing"]["tier"], 0) + 1
    return mix


def _quota_view(nodes: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "nodes": len(nodes),
        "expensive_nodes": sum(1 for node in nodes if node["routing"]["tier"] in ("STRONG", "MAX")),
        "cheap_nodes": sum(1 for node in nodes if node["routing"]["tier"] in ("FAST", "BALANCED")),
        "conditional_expensive_nodes": sum(1 for node in nodes if node["conditional"]
                                           and node["routing"]["tier"] in ("STRONG", "MAX")),
        "full_context_nodes": sum(1 for node in nodes if node["context"]["budget"] == "LARGE"),
    }


def render_plan(plan: dict[str, Any]) -> str:
    lines = ["PLAN", f"OBJECTIVE: {plan['objective_summary']}",
             f"PLANNER: {plan['planner']['required_tier']} / {plan['planner']['required_effort']}"]
    for node in plan["nodes"]:
        routing = node["routing"]
        deps = ",".join(node["depends_on"]) or "none"
        suffix = " (conditional)" if node["conditional"] else ""
        lines.append(f"{node['task_id']}{suffix}  {node['title']}  [{node['task_class']}]  "
                     f"route {routing['tier']}/{routing['effort']}  context {node['context']['budget']}  "
                     f"depends {deps}")
    mix = plan["routing_mix"]
    lines.append("ROUTING MIX: " + "  ".join(f"{tier}:{mix[tier]}" for tier in TIER_ORDER))
    view = plan["quota_view"]
    lines.append(f"QUOTA VIEW: nodes {view['nodes']}, expensive {view['expensive_nodes']}, "
                 f"cheap {view['cheap_nodes']}, conditional-expensive {view['conditional_expensive_nodes']}, "
                 f"large-context {view['full_context_nodes']}")
    if plan["diagnostics"]:
        lines.append("WARNINGS: " + "; ".join(plan["diagnostics"]))
    return "\n".join(lines)
