"""Derived knowledge V0.1 (portable, standard library only).

Loads Git-shareable knowledge (``knowledge/``) and provides bounded, deterministic
lookup plus a reviewed promotion workflow. Shared knowledge is tool-neutral and
surface-neutral; it never contains prompts, secrets, sessions, or runtime state.

Historical observations are advisory evidence only: they never create a permanent
floor and never auto-promote. Only an approved learned default (human-reviewed,
threshold-met) may influence a task family's initial tier/effort, and never below
the classification safety floor or a user override.
"""

from __future__ import annotations

import pathlib
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import learning  # scripts/common, on sys.path (secret guard, lifecycle vocabulary)
import routing  # scripts/common, on sys.path (symbolic tier order)

ROOT = infra.ROOT
KNOWLEDGE_DIR = ROOT / "knowledge"
HISTORICAL_SEED_PATH = KNOWLEDGE_DIR / "routing/historical-seed.yaml"
TASK_FAMILIES_PATH = KNOWLEDGE_DIR / "routing/task-families.yaml"
LEARNED_DEFAULTS_PATH = KNOWLEDGE_DIR / "routing/learned-defaults.yaml"
LESSONS_PATH = KNOWLEDGE_DIR / "lessons/lessons.yaml"
FAILURES_PATH = KNOWLEDGE_DIR / "failures/failures.yaml"

MAX_SHARED_TEXT = 400  # bounded; rejects raw dumps and huge blobs

EVIDENCE_OUTCOMES = ("SUCCESS", "FAILURE", "UNKNOWN")
# Operational evidence failure axis. Distinct from learning.FAILURE_CATEGORIES
# (the student code-failure taxonomy); reuses routing/ESCALATION terminology.
FAILURE_CATEGORIES = (
    "REASONING_INSUFFICIENT", "IMPLEMENTATION_ERROR", "INFRA_FAILURE", "PROVIDER_503",
    "RATE_LIMIT", "TIMEOUT", "AUTH_FAILURE", "ACCOUNT_MODEL_UNSUPPORTED",
    "SAFETY_GUARD", "STALE_SESSION", "CONFIG_DRIFT", "UNKNOWN_FAILURE",
    "PERMISSION_REQUIRED", "READ_ONLY_VIOLATION", "CALL_BUDGET_EXHAUSTED",
)
# Only reasoning/correctness evidence may influence intelligence escalation.
INTELLIGENCE_CATEGORIES = ("REASONING_INSUFFICIENT",)
SOURCE_TYPES = ("historical_verified", "observed_runtime", "manual_override", "discovered")


class KnowledgeError(RuntimeError):
    pass


def _load(path: pathlib.Path, key: str) -> list[dict[str, Any]]:
    try:
        data = infra.load_json(path)
    except infra.InfraError as exc:
        raise KnowledgeError(f"knowledge file unreadable: {exc}") from exc
    if data.get("schema_version") != 1:
        raise KnowledgeError(f"unsupported schema_version in {path}")
    return list(data.get(key, []))


def load_historical_seed(path: pathlib.Path = HISTORICAL_SEED_PATH) -> list[dict[str, Any]]:
    return _load(path, "observations")


def load_task_families(path: pathlib.Path = TASK_FAMILIES_PATH) -> list[dict[str, Any]]:
    return _load(path, "families")


def load_lessons(path: pathlib.Path = LESSONS_PATH) -> list[dict[str, Any]]:
    return _load(path, "lessons")


def load_failures(path: pathlib.Path = FAILURES_PATH) -> list[dict[str, Any]]:
    return _load(path, "failures")


def load_learned_defaults(path: pathlib.Path = LEARNED_DEFAULTS_PATH) -> dict[str, Any]:
    try:
        data = infra.load_json(path)
    except infra.InfraError as exc:
        raise KnowledgeError(f"learned defaults unreadable: {exc}") from exc
    if data.get("schema_version") != 1:
        raise KnowledgeError("unsupported learned-defaults schema_version")
    return data


def classify_failure(category: Optional[str]) -> dict[str, Any]:
    if not category:
        return {"category": None, "known": True, "intelligence_escalation": True}
    known = category in FAILURE_CATEGORIES
    return {"category": category, "known": known,
            "intelligence_escalation": category in INTELLIGENCE_CATEGORIES}


def task_family_for(task: str, families: Optional[list[dict[str, Any]]] = None) -> Optional[str]:
    text = (task or "").lower()
    best, best_hits = None, 0
    for family in families if families is not None else load_task_families():
        hits = sum(1 for keyword in family.get("keywords", []) if keyword.lower() in text)
        if hits > best_hits:
            best, best_hits = family["task_family"], hits
    return best


def observations_for_family(task_family: Optional[str],
                            seed: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    if not task_family:
        return []
    return [row for row in (seed if seed is not None else load_historical_seed())
            if row.get("task_family") == task_family]


def relevant_lessons(query: str, lessons: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    text = (query or "").lower()
    matches = []
    for lesson in lessons if lessons is not None else load_lessons():
        terms = [lesson.get("lesson_id", "")] + list(lesson.get("tags", [])) + lesson.get("title", "").lower().split()
        if any(term and str(term).lower() in text for term in terms):
            matches.append(lesson)
    return matches


def failure_lessons(category: Optional[str],
                    failures: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    rows = failures if failures is not None else load_failures()
    if category:
        rows = [row for row in rows if row.get("category") == category]
    return [{"failure_id": row["failure_id"], "category": row["category"],
             "intelligence_escalation": row.get("intelligence_escalation", False),
             "lesson": row["lesson"]} for row in rows]


def learned_default(task_family: Optional[str],
                    defaults: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    data = defaults if defaults is not None else load_learned_defaults()
    for default in data.get("defaults", []):
        if default.get("task_family") == task_family and default.get("approved"):
            return default
    return None


def safety_floor(task_class: str, defaults: Optional[dict[str, Any]] = None) -> str:
    data = defaults if defaults is not None else load_learned_defaults()
    floors = data.get("policy", {}).get("safety_floor", {})
    return floors.get(task_class, "MAX" if task_class == "CRITICAL" else "FAST")


def apply_learned_default(rec: dict[str, Any], *,
                          defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Clamp an approved learned default onto a recommendation. Safety floor and the
    symbolically-required tier always win; the default can only move within bounds."""
    data = defaults if defaults is not None else load_learned_defaults()
    default = learned_default(rec.get("task_family"), defaults=data)
    rec["learned_default"] = default
    rec["learned_default_applied"] = False
    if not default:
        return rec
    tier_order = data.get("policy", {}).get("tier_order", list(routing.DEFAULT_TIER_ORDER))
    floor = safety_floor(rec["task_class"], defaults=data)
    # effective tier = max(safety floor, user override, learned default). A learned
    # default may lower a family only when the safety floor and user override allow it.
    candidates = [tier for tier in (floor, rec.get("requested_tier"), default.get("tier")) if tier in tier_order]
    effective = max(candidates, key=tier_order.index) if candidates else rec["recommended_tier"]
    if effective != rec["recommended_tier"]:
        rec["recommended_tier"] = effective
        rec["recommended_effort"] = default.get("effort", rec["recommended_effort"])
        rec["learned_default_applied"] = True
    return rec


def _reject_unsafe_text(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise KnowledgeError(f"{label} must be text")
    if len(value) > MAX_SHARED_TEXT:
        raise KnowledgeError(f"{label} exceeds {MAX_SHARED_TEXT} characters; keep shared knowledge concise")
    if "\n" in value:
        raise KnowledgeError(f"{label} must be a single concise line (no raw dumps)")
    if learning.contains_secret_like(value):
        raise KnowledgeError(f"{label} looks like it may contain a credential; refusing to promote")


def promote(candidate: dict[str, Any], *, apply: bool = False,
            defaults_path: pathlib.Path = LEARNED_DEFAULTS_PATH) -> dict[str, Any]:
    """Review-gated promotion. Dry-run by default; --apply mutates shared knowledge.

    Rejects secrets, raw/dump-like text, and single-sample overgeneralization.
    """
    destination = candidate.get("destination")
    data = load_learned_defaults(defaults_path)
    policy = data.get("policy", {})
    if not candidate.get("reviewer") or not candidate.get("human_accepted"):
        raise KnowledgeError("promotion requires a reviewer and human_accepted = true")
    text = candidate.get("lesson") or candidate.get("text") or ""
    _reject_unsafe_text(str(text), f"{destination} text")
    samples = int(candidate.get("samples", 0))
    if destination == "learned-default":
        if samples < int(policy.get("min_samples", 3)):
            raise KnowledgeError("learned default needs the minimum sample count (single successes are not universal)")
        if float(candidate.get("success_ratio", 0.0)) < float(policy.get("min_success_ratio", 0.8)):
            raise KnowledgeError("learned default needs sufficient success evidence")
        entry = {"task_family": candidate["task_family"], "tier": candidate["tier"],
                 "effort": candidate["effort"], "approved": True, "samples": samples,
                 "success_ratio": float(candidate.get("success_ratio", 0.0)),
                 "reviewer": candidate["reviewer"], "note": str(text)}
        target_key, target_path = "defaults", defaults_path
    else:
        raise KnowledgeError(f"unsupported promotion destination: {destination!r}")
    result = {"applied": False, "destination": destination, "path": str(target_path), "entry": entry}
    if not apply:
        return result
    entries = [row for row in data.get(target_key, []) if row.get("task_family") != entry["task_family"]]
    entries.append(entry)
    data[target_key] = entries
    infra.atomic_json(target_path, data, mode=0o644)
    result["applied"] = True
    return result
