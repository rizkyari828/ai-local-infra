"""Adaptive model routing V0.1 (deterministic, standard library only).

Three deliberately separate layers keep routing reusable when models change:

A. task classification   -> MECHANICAL / NORMAL / CORRECTNESS_SENSITIVE / CRITICAL
B. routing requirement   -> symbolic tier (FAST/BALANCED/STRONG/MAX) + reasoning effort
C. model resolution       -> a concrete provider/model for a tier, via the role
                             registry in ``models/roles.yaml``

Layer C only ever resolves names; it never changes classification, tier floors, or
escalation. Classification and escalation use no provider/model identifiers, so
Qwen/Claude/DeepSeek/OpenAI/any future model can be swapped through configuration.
No model or network call is ever made here, and no user-selected model is changed.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any, Optional

import infra  # scripts/common, on sys.path

ROOT = infra.ROOT
ROUTING_PATH = ROOT / "config/model-routing.yaml"
ROLES_PATH = ROOT / "models/roles.yaml"

CLASSES = ("MECHANICAL", "NORMAL", "CORRECTNESS_SENSITIVE", "CRITICAL")
DEFAULT_EFFORT_ORDER = ("low", "medium", "high", "max")
DEFAULT_TIER_ORDER = ("FAST", "BALANCED", "STRONG", "MAX")

# Conservative, deterministic keyword signals. Word-boundary matched so a term
# like "format" does not fire inside "information". An uncertain task falls
# through to NORMAL (the safer middle), never silently to MECHANICAL. These rules
# never reference providers or models.
_CRITICAL_TERMS = (
    "security-sensitive", "security sensitive", "authentication architecture",
    "auth architecture", "major architecture", "architecture decision",
    "cross-system", "cross system", "data integrity", "data loss",
    "data corruption", "repeated failure", "repeatedly fails", "unresolved",
    "production incident", "outage", "critical",
)
_SENSITIVE_TERMS = (
    "architecture", "concurrency", "race condition", "deadlock", "security",
    "authentication", "authorization", "access control", "persistence",
    "migration", "cross-machine", "cross machine", "resolver", "schema",
    "transaction", "idempotent", "idempotency", "integration failure",
    "tricky", "encryption", "cryptograph",
)
_MECHANICAL_TERMS = (
    "rename", "reformat", "format", "formatting", "typo", "grep", "search",
    "git status", "git log", "git push", "git commit", "config key",
    "config field", "rerun tests", "re-run tests", "run tests",
    "run focused tests", "documentation", "docs", "comment", "whitespace",
    "lint", "changelog",
)
_NORMAL_TERMS = (
    "add test", "add tests", "write test", "write tests", "unit test",
    "test coverage", "with tests", "and tests", "implement",
    "bugfix", "fix bug", "refactor", "new feature", "endpoint", "handler",
)


class RoutingError(RuntimeError):
    pass


def _matches(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        if re.search(r"\b" + re.escape(term) + r"\b", text):
            return True
    return False


def _orders(policy: dict[str, Any]) -> tuple[list[str], list[str]]:
    return (policy.get("effort_order", list(DEFAULT_EFFORT_ORDER)),
            policy.get("tier_order", list(DEFAULT_TIER_ORDER)))


def _higher(order: list[str], *values: Optional[str]) -> str:
    present = [value for value in values if value in order]
    return max(present, key=order.index) if present else order[0]


def _tier_for_effort(policy: dict[str, Any], effort: str) -> Optional[str]:
    effort_order, tier_order = _orders(policy)
    target = effort_order.index(effort)
    candidates = [
        tier for tier, entry in policy["tiers"].items()
        if effort_order.index(entry.get("effort", "low")) >= target
    ]
    return min(candidates, key=tier_order.index) if candidates else tier_order[-1]


# --------------------------------------------------------------------------- #
# A. Task classification (provider/model independent)
# --------------------------------------------------------------------------- #


def load_policy(path: pathlib.Path = ROUTING_PATH) -> dict[str, Any]:
    try:
        policy = infra.load_json(path)
    except infra.InfraError as exc:
        raise RoutingError(f"model routing policy unreadable: {exc}") from exc
    if policy.get("schema_version") != 1:
        raise RoutingError("unsupported model routing schema_version")
    return policy


def normalize_class(value: str) -> str:
    candidate = re.sub(r"[\s-]+", "_", (value or "").strip()).upper()
    if candidate not in CLASSES:
        raise RoutingError(f"unknown task class: {value!r} (expected one of {', '.join(CLASSES)})")
    return candidate


def normalize_effort(value: str, policy: dict[str, Any]) -> str:
    candidate = (value or "").strip().lower()
    if candidate not in _orders(policy)[0]:
        raise RoutingError(f"unknown effort: {value!r}")
    return candidate


def normalize_tier(value: str, policy: dict[str, Any]) -> str:
    candidate = (value or "").strip().upper()
    if candidate not in _orders(policy)[1]:
        raise RoutingError(f"unknown tier: {value!r}")
    return candidate


def classify(task: str, override: Optional[str] = None) -> str:
    """Return a task class. Explicit override wins; otherwise conservative rules."""
    if override:
        return normalize_class(override)
    text = (task or "").lower()
    if _matches(text, _CRITICAL_TERMS):
        return "CRITICAL"
    if _matches(text, _SENSITIVE_TERMS):
        return "CORRECTNESS_SENSITIVE"
    if _matches(text, _MECHANICAL_TERMS) and not _matches(text, _NORMAL_TERMS):
        return "MECHANICAL"
    return "NORMAL"


# --------------------------------------------------------------------------- #
# B. Routing requirement (symbolic tier + effort; provider/model independent)
# --------------------------------------------------------------------------- #


def requirement(task: str = "", *, task_class: Optional[str] = None,
                requested_effort: Optional[str] = None, requested_tier: Optional[str] = None,
                policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Map a task to a symbolic tier/effort. User-requested effort/tier only raise it."""
    policy = policy or load_policy()
    cls = normalize_class(task_class) if task_class else classify(task)
    entry = policy["task_classes"][cls]
    effort_order, tier_order = _orders(policy)

    effort = entry["effort"]
    tier = entry["tier"]
    if requested_effort:
        effort = _higher(effort_order, effort, normalize_effort(requested_effort, policy))
    if requested_tier:
        tier = _higher(tier_order, tier, normalize_tier(requested_tier, policy))
    tier = _higher(tier_order, tier, _tier_for_effort(policy, effort))
    effort = _higher(effort_order, effort, policy["tiers"].get(tier, {}).get("effort"))
    return {
        "task_class": cls,
        "recommended_tier": tier,
        "recommended_effort": effort,
        "reason": entry.get("rationale"),
        "escalation_from": None,
        "escalation_to": entry.get("escalates_to"),
        "max_retries": entry.get("max_retries", 0),
    }


# --------------------------------------------------------------------------- #
# C. Model resolution (the only layer that knows providers/models)
# --------------------------------------------------------------------------- #


def load_roles(registry: Optional[dict[str, Any]] = None,
               path: pathlib.Path = ROLES_PATH) -> dict[str, Any]:
    """Return role_id -> role record. An explicit registry overrides the file."""
    if registry is not None:
        return registry
    try:
        data = infra.load_json(path)
    except infra.InfraError:
        return {}
    return {role.get("id"): role for role in data.get("roles", []) if role.get("id")}


def resolve_model(tier: str, policy: dict[str, Any],
                  registry: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    role = policy["tiers"].get(tier, {}).get("role")
    record = load_roles(registry).get(role) if role else None
    return {
        "role": role,
        "model": (record or {}).get("model_reference"),
        "provider": (record or {}).get("provider"),
        "resolved": bool(record and record.get("model_reference")),
    }


def model_lookup(model_name: Optional[str], policy: dict[str, Any],
                 registry: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve a concrete model to its best symbolic tier/role/provider, if known."""
    if not model_name:
        return {"model": None, "tier": None, "provider": None, "role": None}
    roles = load_roles(registry)
    matches = []
    for tier, entry in policy["tiers"].items():
        role_id = entry.get("role")
        record = roles.get(role_id) or {}
        if record.get("model_reference") == model_name:
            matches.append((tier, role_id, record))
    if not matches:
        return {"model": model_name, "tier": None, "provider": None, "role": None}
    _, tier_order = _orders(policy)
    tier, role_id, record = max(matches, key=lambda item: tier_order.index(item[0]))
    return {"model": model_name, "tier": tier, "provider": record.get("provider"), "role": role_id}


def model_tier(model_name: Optional[str], policy: dict[str, Any],
               registry: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Best (highest) symbolic tier a concrete model can satisfy, or None if unknown."""
    return model_lookup(model_name, policy, registry)["tier"]


def surface_support(surface: Optional[str], tier: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Surface capability and whether it can provide ``tier``. Never downgrades: an
    unsupported tier is reported as ``UNAVAILABLE_ON_SURFACE``."""
    if not surface:
        return {"surface": None, "capability": None, "availability": None}
    spec = policy.get("surfaces", {}).get(surface)
    if spec is None:
        return {"surface": surface, "capability": None, "availability": "UNKNOWN_SURFACE"}
    if isinstance(spec, str):  # backward-compatible shorthand
        return {"surface": surface, "capability": spec, "availability": "AVAILABLE"}
    tiers = spec.get("tiers")
    availability = "AVAILABLE" if (not tiers or tier in tiers) else "UNAVAILABLE_ON_SURFACE"
    return {"surface": surface, "capability": spec.get("capability"), "availability": availability}


def recommend(task: str = "", *, task_class: Optional[str] = None,
              requested_effort: Optional[str] = None, requested_tier: Optional[str] = None,
              user_model: Optional[str] = None, surface: Optional[str] = None,
              policy: Optional[dict[str, Any]] = None,
              registry: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Compose requirement + model resolution + surface support + user override."""
    policy = policy or load_policy()
    _, tier_order = _orders(policy)
    rec = requirement(task, task_class=task_class, requested_effort=requested_effort,
                      requested_tier=requested_tier, policy=policy)
    resolution = resolve_model(rec["recommended_tier"], policy, registry)
    rec.update({"role": resolution["role"], "model": resolution["model"],
                "provider": resolution["provider"], "resolved": resolution["resolved"]})
    rec.update(surface_support(surface, rec["recommended_tier"], policy))

    rec["user_model"] = user_model
    rec["user_model_status"] = None
    if user_model:
        lookup = model_lookup(user_model, policy, registry)
        if lookup["tier"] is None:
            rec["user_model_status"] = "UNVERIFIED"
        elif tier_order.index(lookup["tier"]) >= tier_order.index(rec["recommended_tier"]):
            rec["user_model_status"] = "RESPECTED"
        else:
            rec["user_model_status"] = "CONFLICT"
        rec["model"] = user_model
        rec["provider"] = lookup["provider"]
        rec["resolved"] = bool(lookup["tier"])
    return rec


def escalation_step(task_class: str, *, same_tier_attempts: int = 0, succeeded: bool = False,
                    policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Bounded next step. Success stops; the maximum tier stops; otherwise a small
    number of same-tier retries precede a single step up. Symbolic only."""
    policy = policy or load_policy()
    cls = normalize_class(task_class)
    entry = policy["task_classes"][cls]
    if succeeded:
        return {"action": "stop", "reason": "acceptance met", "task_class": cls,
                "escalation_from": None, "escalation_to": None}
    target = entry.get("escalates_to")
    if not target:
        return {"action": "stop", "reason": "already at maximum tier; report rather than retry",
                "task_class": cls, "escalation_from": None, "escalation_to": None}
    if same_tier_attempts < int(entry.get("max_retries", 0)):
        return {"action": "retry", "reason": "same-tier retry budget remains", "task_class": cls,
                "escalation_from": None, "escalation_to": None}
    return {"action": "escalate", "reason": "escalating after bounded same-tier attempts",
            "task_class": target, "escalation_from": cls, "escalation_to": target}
