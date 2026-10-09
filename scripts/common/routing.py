"""Adaptive model routing V0.1 (deterministic, standard library only).

Answers "what is the cheapest sufficient model/reasoning tier for this task?"
without calling any model or network: a small keyword classifier, a declarative
policy (``config/model-routing.yaml``), and a bounded escalation step. Tiers
reference the existing model roles in ``models/roles.yaml`` rather than
hardcoding provider/model names.

This is a recommendation layer, not an autonomous router: every surface is
``RECOMMEND_ONLY`` in V0.1 and no user-selected model is ever changed.
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
# through to NORMAL (the safer middle), never silently to MECHANICAL.
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
    if candidate not in policy.get("effort_order", list(DEFAULT_EFFORT_ORDER)):
        raise RoutingError(f"unknown effort: {value!r}")
    return candidate


def normalize_tier(value: str, policy: dict[str, Any]) -> str:
    candidate = (value or "").strip().upper()
    if candidate not in policy.get("tier_order", list(DEFAULT_TIER_ORDER)):
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


def _higher(order: tuple[str, ...] | list[str], *values: Optional[str]) -> str:
    present = [value for value in values if value in order]
    return max(present, key=order.index) if present else order[0]


def _tier_for_effort(policy: dict[str, Any], effort: str) -> Optional[str]:
    effort_order = policy.get("effort_order", list(DEFAULT_EFFORT_ORDER))
    tier_order = policy.get("tier_order", list(DEFAULT_TIER_ORDER))
    target = effort_order.index(effort)
    candidates = [
        tier for tier, entry in policy["tiers"].items()
        if effort_order.index(entry.get("effort", "low")) >= target
    ]
    return min(candidates, key=tier_order.index) if candidates else tier_order[-1]


def role_model(role_id: Optional[str]) -> Optional[str]:
    if not role_id:
        return None
    try:
        data = infra.load_json(ROLES_PATH)
    except infra.InfraError:
        return None
    for role in data.get("roles", []):
        if role.get("id") == role_id:
            return role.get("model_reference")
    return None


def recommend(task: str, *, task_class: Optional[str] = None,
              requested_effort: Optional[str] = None, requested_tier: Optional[str] = None,
              surface: Optional[str] = None, policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Recommend a tier/effort. User-requested effort/tier only ever raise it."""
    policy = policy or load_policy()
    cls = normalize_class(task_class) if task_class else classify(task)
    entry = policy["task_classes"][cls]
    effort_order = policy.get("effort_order", list(DEFAULT_EFFORT_ORDER))
    tier_order = policy.get("tier_order", list(DEFAULT_TIER_ORDER))

    effort = entry["effort"]
    tier = entry["tier"]
    if requested_effort:
        effort = _higher(effort_order, effort, normalize_effort(requested_effort, policy))
    if requested_tier:
        tier = _higher(tier_order, tier, normalize_tier(requested_tier, policy))
    tier = _higher(tier_order, tier, _tier_for_effort(policy, effort))
    effort = _higher(effort_order, effort, policy["tiers"].get(tier, {}).get("effort"))
    role = policy["tiers"].get(tier, {}).get("role")
    surfaces = policy.get("surfaces", {})
    return {
        "task_class": cls,
        "recommended_tier": tier,
        "recommended_effort": effort,
        "role": role,
        "model": role_model(role),
        "reason": entry.get("rationale"),
        "escalation_from": None,
        "escalation_to": entry.get("escalates_to"),
        "max_retries": entry.get("max_retries", 0),
        "surface": surface,
        "capability": surfaces.get(surface) if surface else None,
    }


def escalation_step(task_class: str, *, same_tier_attempts: int = 0, succeeded: bool = False,
                    policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Bounded next step. Success stops; the maximum tier stops; otherwise a small
    number of same-tier retries precede a single step up."""
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
