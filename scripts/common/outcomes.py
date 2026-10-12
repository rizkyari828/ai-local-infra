"""Outcome aggregation, candidate generation, and storage observability (V0.2).

Deterministic and local. Historical seed and observed runtime stay distinguishable,
and infrastructure failures are never counted as model-quality failures. Candidates
are suggestions only: nothing here mutates policy, promotes, commits, or deletes.
"""

from __future__ import annotations

import pathlib
from typing import Any, Optional

import evidence  # scripts/common, on sys.path
import derived_knowledge as knowledge  # scripts/common, on sys.path
import routing  # scripts/common, on sys.path

INTELLIGENCE_CATEGORY = "REASONING_INSUFFICIENT"
NON_INTELLIGENCE = tuple(c for c in knowledge.FAILURE_CATEGORIES if c != INTELLIGENCE_CATEGORY)

# Conservative per-class downgrade confidence thresholds (separate from the schema
# baseline min_samples in learned-defaults.yaml). CRITICAL never auto-downgrades.
DOWNGRADE_MIN_SAMPLES = {"MECHANICAL": 3, "NORMAL": 5, "CORRECTNESS_SENSITIVE": 10, "CRITICAL": None}
DOWNGRADE_MIN_RATIO = {"MECHANICAL": 0.9, "NORMAL": 0.9, "CORRECTNESS_SENSITIVE": 0.95, "CRITICAL": 1.0}
_STRONGER = {"FAST": "BALANCED", "BALANCED": "STRONG", "STRONG": "MAX", "MAX": None}
_EFFORT_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "VERY_HIGH": 3, "MAX": 4}


def _family_class(family: Optional[str]) -> Optional[str]:
    for entry in knowledge.load_task_families():
        if entry["task_family"] == family:
            return entry["task_class"]
    return None


def _current_policy(family: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    cls = _family_class(family)
    if not cls:
        return None, None
    entry = routing.load_policy()["task_classes"].get(cls, {})
    return entry.get("tier"), entry.get("effort")


def _is_intelligence(row) -> bool:
    return row["failure_category"] == INTELLIGENCE_CATEGORY


def _is_simulated(row) -> bool:
    if "simulated" not in row.keys():
        return False
    value = row["simulated"]
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false")
    return bool(value)


def _is_runtime(row) -> bool:
    """Real observed runtime evidence only: simulation/fake runs are never production
    quality evidence and must not influence routing/capability decisions."""
    return row["source"] == "observed_runtime" and not _is_simulated(row)


def aggregate(rows: Optional[list] = None, *, db=None, state_dir=None) -> dict[str, Any]:
    rows = rows if rows is not None else evidence.all_rows(db, state_dir)
    def bucket(key):
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            name = row[key] or "unknown"
            entry = out.setdefault(name, {"samples": 0, "success": 0, "reasoning_failures": 0,
                                          "non_intelligence_failures": 0, "escalated": 0})
            entry["samples"] += 1
            entry["success"] += 1 if row["outcome"] == "SUCCESS" else 0
            entry["reasoning_failures"] += 1 if _is_intelligence(row) else 0
            entry["non_intelligence_failures"] += 1 if (row["failure_category"] in NON_INTELLIGENCE) else 0
            entry["escalated"] += 1 if row["escalated"] else 0
        return out
    return {"by_family": bucket("task_family"), "by_surface": bucket("surface"),
            "by_final_tier": bucket("final_tier"), "by_final_effort": bucket("final_effort")}


def family_learning(family: str, rows: Optional[list] = None, *, db=None, state_dir=None) -> dict[str, Any]:
    rows = [row for row in (rows if rows is not None else evidence.all_rows(db, state_dir))
            if row["task_family"] == family]
    runtime = [row for row in rows if _is_runtime(row)]
    return {
        "task_family": family,
        "samples": len(rows),
        "historical": sum(1 for row in rows if not _is_runtime(row)),
        "observed_runtime": len(runtime),
        "success": sum(1 for row in rows if row["outcome"] == "SUCCESS"),
        "reasoning_failures": sum(1 for row in rows if _is_intelligence(row)),
        "non_intelligence_failures": sum(1 for row in rows if row["failure_category"] in NON_INTELLIGENCE),
        "unknown": sum(1 for row in rows if row["outcome"] == "UNKNOWN"),
        "escalations": sum(1 for row in rows if row["escalated"]),
        "success_by_initial_tier": _success_by(rows, "initial_tier"),
        "success_by_final_tier": _success_by(rows, "final_tier"),
        "success_by_effort": _success_by(rows, "final_effort"),
        "recent": [row["recorded_at"] for row in rows[-5:]],
    }


def _success_by(rows: list, key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        if row["outcome"] == "SUCCESS" and row[key]:
            out[row[key]] = out.get(row[key], 0) + 1
    return out


def model_performance(rows: Optional[list] = None, *, task_family: Optional[str] = None,
                      surface: Optional[str] = None, db=None, state_dir=None) -> list[dict[str, Any]]:
    rows = rows if rows is not None else evidence.all_rows(db, state_dir)
    groups: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        if not _is_runtime(row) or not row["model"]:
            continue
        if task_family and row["task_family"] != task_family:
            continue
        if surface and row["surface"] != surface:
            continue
        key = (row["surface"], row["provider"], row["model"], row["native_variant"],
               row["normalized_effort"], row["task_family"])
        entry = groups.setdefault(key, {"surface": row["surface"], "provider": row["provider"],
                                        "model": row["model"], "native_variant": row["native_variant"],
                                        "normalized_effort": row["normalized_effort"],
                                        "task_family": row["task_family"], "samples": 0, "success": 0,
                                        "unverified": 0, "reasoning_escalation": 0, "infra_failures": 0})
        entry["samples"] += 1
        entry["success"] += 1 if row["outcome"] == "SUCCESS" else 0
        entry["unverified"] += 1 if row["outcome"] == "UNKNOWN" else 0
        entry["reasoning_escalation"] += 1 if _is_intelligence(row) else 0
        entry["infra_failures"] += 1 if row["failure_category"] in NON_INTELLIGENCE else 0
        entry["insufficient"] = entry["samples"] < 3
        entry["success_ratio"] = _ratio(entry["success"], entry["samples"])
    return sorted(groups.values(), key=lambda row: (row["task_family"] or "", row["model"] or ""))


def candidates(rows: Optional[list] = None, *, db=None, state_dir=None) -> dict[str, Any]:
    rows = evidence.all_rows(db, state_dir) if rows is None else rows
    runtime = [row for row in rows if _is_runtime(row)]
    found: list[dict[str, Any]] = []

    families = {row["task_family"] for row in runtime if row["task_family"]}
    for family in sorted(families):
        current_tier, current_effort = _current_policy(family)
        current_effort = current_effort.upper() if current_effort else current_effort
        cls = _family_class(family) or "NORMAL"
        min_samples = DOWNGRADE_MIN_SAMPLES.get(cls)
        min_ratio = DOWNGRADE_MIN_RATIO.get(cls, 0.9)
        family_rows = [row for row in runtime if row["task_family"] == family]
        successes = [row for row in family_rows if row["outcome"] == "SUCCESS"]
        reasoning = [row for row in family_rows if _is_intelligence(row)]
        non_intel: dict[str, int] = {}
        for row in family_rows:
            if row["failure_category"] in NON_INTELLIGENCE:
                non_intel[row["failure_category"]] = non_intel.get(row["failure_category"], 0) + 1
        blocking = []
        if cls == "CRITICAL":
            blocking.append("CRITICAL never auto-downgrades")

        if len(family_rows) < 3:
            found.append(_candidate("INSUFFICIENT_EVIDENCE", family, current_tier, current_effort,
                                    len(family_rows), len(successes), len(reasoning), non_intel, blocking,
                                    "fewer than 3 observed runtime samples"))

        # cheaper tier sufficient
        if min_samples is not None:
            for tier in _lower_tiers(current_tier):
                at_tier = [row for row in successes if row["final_tier"] == tier]
                weak = [row for row in reasoning if row["final_tier"] == tier]
                # Denominator includes representative negatives/unknowns at that tier:
                # a non-empty success set must not imply 100% success.
                attempts_at_tier = [row for row in family_rows if row["final_tier"] == tier]
                ratio = _ratio(len(at_tier), len(attempts_at_tier))
                if len(at_tier) >= min_samples and not weak and ratio >= min_ratio:
                    found.append(_candidate("CHEAPER_TIER_SUFFICIENT", family, current_tier, current_effort,
                                            len(attempts_at_tier), len(at_tier), 0, non_intel, blocking,
                                            f"{len(at_tier)}/{len(attempts_at_tier)} successes at {tier} "
                                            f"without reasoning escalation",
                                            proposed_tier=tier, ratio=ratio))
        else:
            lower = [tier for tier in _lower_tiers(current_tier)
                     if any(row["final_tier"] == tier for row in successes)]
            if lower:
                found.append(_candidate("CHEAPER_TIER_SUFFICIENT", family, current_tier, current_effort,
                                        len(successes), len(successes), len(reasoning), non_intel, blocking,
                                        "lower tiers succeeded, but CRITICAL never auto-downgrades",
                                        proposed_tier=lower[0]))

        # lower effort sufficient (same tier). The denominator is EVERY observed runtime
        # attempt at that effort (successes + failures + unverified), so a large
        # unverified population cannot masquerade as high-confidence success.
        for effort in _lower_efforts(current_effort):
            at_effort = [row for row in family_rows
                         if (row["final_effort"] or "").upper() == effort
                         and row["final_tier"] == current_tier]
            if not at_effort:
                continue
            effort_success = [row for row in at_effort if row["outcome"] == "SUCCESS"]
            ratio = _ratio(len(effort_success), len(at_effort))
            if len(at_effort) >= (min_samples or 3) and not any(
                    _is_intelligence(row) for row in at_effort) and ratio >= min_ratio:
                found.append(_candidate("LOWER_EFFORT_SUFFICIENT", family, current_tier, current_effort,
                                        len(at_effort), len(effort_success), 0, non_intel, blocking,
                                        f"{len(effort_success)}/{len(at_effort)} successes at {effort} effort",
                                        proposed_effort=effort))

        # current tier too weak
        at_current = [row for row in reasoning if row["final_tier"] == current_tier]
        if len(at_current) >= 2 and _STRONGER.get(current_tier):
            found.append(_candidate("CURRENT_TIER_TOO_WEAK", family, current_tier, current_effort,
                                    len(family_rows), len(successes), len(at_current), non_intel, blocking,
                                    f"{len(at_current)} reasoning failures at {current_tier}",
                                    proposed_tier=_STRONGER[current_tier]))

        # current effort too low
        low_effort_reasoning = [row for row in reasoning
                                if (row["final_effort"] or "").upper() == current_effort]
        if len(low_effort_reasoning) >= 2:
            found.append(_candidate("CURRENT_EFFORT_TOO_LOW", family, current_tier, current_effort,
                                    len(family_rows), len(successes), len(low_effort_reasoning), non_intel,
                                    blocking, f"{len(low_effort_reasoning)} reasoning failures at {current_effort} effort",
                                    proposed_effort=_higher_effort(current_effort)))

        for category, count in sorted(non_intel.items()):
            if count >= 3:
                found.append(_candidate("RECURRING_NON_INTELLIGENCE_FAILURE", family, current_tier, current_effort,
                                        len(family_rows), len(successes), len(reasoning), {category: count},
                                        blocking, f"{count} recurring {category} (not model weakness)"))

    # model/variant evidence. Qualification uses the representative denominator
    # (successes / all observed attempts), so unverified volume blocks a
    # high-confidence "effective" claim.
    for row in model_performance(runtime):
        if row["insufficient"]:
            continue
        if row["reasoning_escalation"] >= 2:
            found.append({"type": "MODEL_VARIANT_UNRELIABLE", "task_family": row["task_family"],
                          "current": None, "proposed": None, "surface": row["surface"],
                          "provider": row["provider"], "model": row["model"],
                          "native_variant": row["native_variant"], "samples": row["samples"],
                          "success": row["success"], "unverified": row["unverified"],
                          "success_ratio": row["success_ratio"],
                          "reasoning_failures": row["reasoning_escalation"],
                          "non_intelligence_failures": row["infra_failures"], "confidence": "medium",
                          "reason": f"{row['reasoning_escalation']} reasoning escalations for {row['model']}",
                          "blocking": []})
        elif (row["success"] >= 3 and row["reasoning_escalation"] == 0
              and row["success_ratio"] >= 0.9):
            found.append({"type": "MODEL_VARIANT_EFFECTIVE", "task_family": row["task_family"],
                          "current": None, "proposed": None, "surface": row["surface"],
                          "provider": row["provider"], "model": row["model"],
                          "native_variant": row["native_variant"], "samples": row["samples"],
                          "success": row["success"], "unverified": row["unverified"],
                          "success_ratio": row["success_ratio"],
                          "reasoning_failures": 0,
                          "non_intelligence_failures": row["infra_failures"],
                          "confidence": "high" if row["samples"] >= 5 else "medium",
                          "reason": f"{row['success']}/{row['samples']} successes without reasoning "
                                    f"escalation for {row['model']}",
                          "blocking": []})
    return {"candidates": found, "count": len(found)}


def _candidate(kind, family, tier, effort, samples, success, reasoning, non_intel, blocking, reason,
               proposed_tier=None, proposed_effort=None, ratio=None) -> dict[str, Any]:
    confident = samples >= 5 and (ratio is None or ratio >= 0.9)
    return {"type": kind, "task_family": family, "current": {"tier": tier, "effort": effort},
            "proposed": {"tier": proposed_tier, "effort": proposed_effort}, "samples": samples,
            "success": success, "success_ratio": ratio, "reasoning_failures": reasoning,
            "non_intelligence_failures": dict(non_intel), "escalations": reasoning,
            "confidence": "high" if confident else "medium",
            "reason": reason, "blocking": list(blocking)}


def _lower_tiers(tier: Optional[str]) -> list[str]:
    order = list(routing.DEFAULT_TIER_ORDER)
    return order[:order.index(tier)] if tier in order else []


def _lower_efforts(effort: Optional[str]) -> list[str]:
    if not effort or effort not in _EFFORT_RANK:
        return []
    return [name for name, rank in _EFFORT_RANK.items() if rank < _EFFORT_RANK[effort]]


def _higher_effort(effort: Optional[str]) -> Optional[str]:
    if not effort or effort not in _EFFORT_RANK:
        return None
    higher = [name for name, rank in _EFFORT_RANK.items() if rank > _EFFORT_RANK[effort]]
    return min(higher, key=lambda name: _EFFORT_RANK[name]) if higher else None


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def prune_dry_run(rows: Optional[list] = None, keep_per_group: int = 50, *, db=None,
                  state_dir=None) -> dict[str, Any]:
    """Observe storage growth. Reports eligibility only; never deletes. --apply is deferred."""
    rows = evidence.all_rows(db, state_dir) if rows is None else rows
    approved_families = {d["task_family"] for d in knowledge.load_learned_defaults().get("defaults", [])
                         if d.get("approved")}
    groups: dict[tuple, list] = {}
    for row in rows:
        if _is_runtime(row) and row["outcome"] == "SUCCESS":
            groups.setdefault((row["task_family"], row["final_tier"], row["final_effort"]), []).append(row)
    eligible = 0
    reasons: dict[str, int] = {}
    for (family, _tier, _effort), group in groups.items():
        group.sort(key=lambda row: row["recorded_at"] or "")
        keep = len(group) if family in approved_families else keep_per_group
        removable = max(0, len(group) - keep)
        if removable:
            eligible += removable
            reasons["redundant observed runtime success beyond retained aggregate support"] = \
                reasons.get("redundant observed runtime success beyond retained aggregate support", 0) + removable
    db_size = 0
    target = pathlib.Path(db) if db is not None else evidence.db_path(state_dir)
    if target.exists():
        db_size = target.stat().st_size
    avg = (db_size / len(rows)) if rows else 0
    return {"dry_run": True, "eligible_rows": eligible, "reasons": reasons,
            "never_eligible": ["historical_verified rows", "failures", "unknown outcomes",
                               "rows supporting an approved learned default"],
            "estimated_bytes": int(avg * eligible), "db_size_bytes": db_size,
            "policy": {"keep_per_group": keep_per_group, "automatic_prune": False, "scheduler": False},
            "apply": "deferred in V0.2 (aggregation-before-delete not yet implemented)"}
