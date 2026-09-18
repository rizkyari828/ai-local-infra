#!/usr/bin/env python3
"""Provider-neutral shared escalation context pack (stdlib only).

A context pack is *distilled task state* that can move between providers/models
without copying a full conversation or any provider's prompt/KV cache. Provider
prompt and KV caches stay provider-specific; the shared layer only carries
concise, factual, provider-neutral state.

Design rules:

- Store summaries, never reasoning: no chain-of-thought, full prompts, full
  model responses, source files, credentials, or auth headers. The existing
  ``learning.contains_secret_like`` guard rejects credential-like text.
- All task fields are optional except the goal and the pack version.
- Growth is bounded: list fields keep the most recent ``max_items`` unique
  entries, and free text is truncated. No tokenizer dependency; an approximate
  character guard is used, matching the existing 200-char diagnostic limit.
- One deterministic renderer emits stable sections (goal, constraints, lessons,
  files, state) before volatile ones (attempts, failure, tests, next action) so
  each provider can build its own cache from the same shared text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import infra
import learning

CONTEXT_PACK_VERSION = "1.0.0"

# Bounded-growth guards. Sized after existing conventions: provider diagnostics
# cap free text at 200 characters and prompt-prefix hashing at 2048 characters.
# The goal is a bounded task statement: the full prompt is forwarded to the
# provider but is never persisted beyond this bound.
MAX_ITEMS = 8
MAX_ITEM_CHARS = 200
MAX_GOAL_CHARS = 512
CHARS_PER_TOKEN = 4

# Free-text fields, in stable-then-volatile order.
TEXT_FIELDS = (
    "goal",
    "task_type",
    "current_state",
    "failure_category",
    "error_summary",
    "recommended_next_action",
    "source_provider",
    "source_model",
    "source_variant",
)
LIST_FIELDS = (
    "relevant_files",
    "constraints",
    "changes_attempted",
    "approaches_tried",
    "failed_tests",
    "test_results",
    "observations",
    "lessons",
)
CONTEXT_PACK_REQUIRED = ("context_pack_version", "goal")
CONTEXT_PACK_FIELDS = ("context_pack_version",) + TEXT_FIELDS + LIST_FIELDS

# Learning records that may be referenced as reusable lessons. Reading such
# records does not write to, or duplicate, the learning store.
LESSON_LIFECYCLE = ("reviewed", "validated", "promoted")


@dataclass
class ContextPack:
    """Provider-neutral distilled state for one task handoff."""

    context_pack_version: str = CONTEXT_PACK_VERSION
    goal: Optional[str] = None
    task_type: Optional[str] = None
    current_state: Optional[str] = None
    relevant_files: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    changes_attempted: list = field(default_factory=list)
    approaches_tried: list = field(default_factory=list)
    failure_category: Optional[str] = None
    error_summary: Optional[str] = None
    failed_tests: list = field(default_factory=list)
    test_results: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    lessons: list = field(default_factory=list)
    recommended_next_action: Optional[str] = None
    source_provider: Optional[str] = None
    source_model: Optional[str] = None
    source_variant: Optional[str] = None

    def as_dict(self) -> dict:
        record = {"context_pack_version": self.context_pack_version}
        for name in TEXT_FIELDS:
            record[name] = getattr(self, name)
        for name in LIST_FIELDS:
            record[name] = list(getattr(self, name))
        return record

    @classmethod
    def from_dict(cls, data: dict) -> "ContextPack":
        validate_context_pack(data)
        pack = cls(context_pack_version=data["context_pack_version"], goal=data["goal"])
        for name in TEXT_FIELDS:
            if name != "goal":
                setattr(pack, name, data.get(name))
        for name in LIST_FIELDS:
            setattr(pack, name, list(data.get(name) or []))
        return pack


def _positive_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value


def _guard(limits: Optional[dict]) -> tuple:
    section = (limits or {}).get("context_pack") or {}
    if not isinstance(section, dict):
        section = {}
    return (
        _positive_int(section.get("max_items"), MAX_ITEMS),
        _positive_int(section.get("max_item_chars"), MAX_ITEM_CHARS),
        _positive_int(section.get("max_goal_chars"), MAX_GOAL_CHARS),
    )


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise infra.InfraError("Context pack text fields must be strings")
    text = " ".join(value.split())
    if not text:
        return None
    if learning.contains_secret_like(text):
        raise infra.InfraError("Context pack text looks like it may contain a credential; do not record secrets")
    return text[:limit]


def _clean_items(values: Any, max_items: int, item_chars: int) -> list:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if isinstance(values, (set, tuple)):
        values = list(values)
    if not isinstance(values, list):
        raise infra.InfraError("Context pack list fields must be lists of strings")
    items: list = []
    for value in values:
        text = _clean_text(value, item_chars)
        if text is not None and text not in items:
            items.append(text)
    return items[-max_items:]


def _merge_unique(existing: list, additions: list, max_items: int) -> list:
    merged = list(existing)
    for item in additions:
        if item not in merged:
            merged.append(item)
    return merged[-max_items:]


def build_context_pack(
    goal: str,
    *,
    task_type: Optional[str] = None,
    current_state: Optional[str] = None,
    relevant_files: Optional[list] = None,
    constraints: Optional[list] = None,
    changes_attempted: Optional[list] = None,
    approaches_tried: Optional[list] = None,
    failure_category: Optional[str] = None,
    error_summary: Optional[str] = None,
    failed_tests: Optional[list] = None,
    test_results: Optional[list] = None,
    observations: Optional[list] = None,
    lessons: Optional[list] = None,
    recommended_next_action: Optional[str] = None,
    source_provider: Optional[str] = None,
    source_model: Optional[str] = None,
    source_variant: Optional[str] = None,
    limits: Optional[dict] = None,
) -> ContextPack:
    """Create a minimal pack; only ``goal`` is required."""
    max_items, item_chars, goal_chars = _guard(limits)
    clean_goal = _clean_text(goal, goal_chars)
    if not clean_goal:
        raise infra.InfraError("Context pack requires a non-empty goal")
    if failure_category is not None:
        learning.validate_failure_category(failure_category)
    return ContextPack(
        goal=clean_goal,
        task_type=_clean_text(task_type, item_chars),
        current_state=_clean_text(current_state, goal_chars),
        relevant_files=_clean_items(relevant_files, max_items, item_chars),
        constraints=_clean_items(constraints, max_items, item_chars),
        changes_attempted=_clean_items(changes_attempted, max_items, item_chars),
        approaches_tried=_clean_items(approaches_tried, max_items, item_chars),
        failure_category=failure_category,
        error_summary=_clean_text(error_summary, item_chars),
        failed_tests=_clean_items(failed_tests, max_items, item_chars),
        test_results=_clean_items(test_results, max_items, item_chars),
        observations=_clean_items(observations, max_items, item_chars),
        lessons=_clean_items(lessons, max_items, item_chars),
        recommended_next_action=_clean_text(recommended_next_action, item_chars),
        source_provider=_clean_text(source_provider, item_chars),
        source_model=_clean_text(source_model, item_chars),
        source_variant=_clean_text(source_variant, item_chars),
    )


def record_attempt(
    pack: ContextPack,
    *,
    task_type: Optional[str] = None,
    current_state: Optional[str] = None,
    approach: Optional[Any] = None,
    changes: Optional[Any] = None,
    failure_category: Optional[str] = None,
    error_summary: Optional[str] = None,
    failed_tests: Optional[Any] = None,
    test_results: Optional[Any] = None,
    observations: Optional[Any] = None,
    lessons: Optional[Any] = None,
    relevant_files: Optional[Any] = None,
    constraints: Optional[Any] = None,
    recommended_next_action: Optional[str] = None,
    source_provider: Optional[str] = None,
    source_model: Optional[str] = None,
    source_variant: Optional[str] = None,
    limits: Optional[dict] = None,
) -> ContextPack:
    """Append one concise, deduplicated attempt and refresh the latest evidence.

    Scalar failure/handoff fields always reflect the most recent attempt; list
    fields accumulate unique entries and are capped so a multi-hop handoff never
    grows without bound.
    """
    if not isinstance(pack, ContextPack):
        raise infra.InfraError("record_attempt requires a ContextPack")
    max_items, item_chars, goal_chars = _guard(limits)

    def add(name: str, values: Any) -> None:
        if values is None:
            return
        cleaned = _clean_items(values, max_items, item_chars)
        setattr(pack, name, _merge_unique(getattr(pack, name), cleaned, max_items))

    add("approaches_tried", approach)
    add("changes_attempted", changes)
    add("failed_tests", failed_tests)
    add("test_results", test_results)
    add("observations", observations)
    add("lessons", lessons)
    add("relevant_files", relevant_files)
    add("constraints", constraints)

    if task_type is not None and pack.task_type is None:
        pack.task_type = _clean_text(task_type, item_chars)
    if current_state is not None:
        pack.current_state = _clean_text(current_state, goal_chars)
    if failure_category is not None:
        learning.validate_failure_category(failure_category)
        pack.failure_category = failure_category
    if error_summary is not None:
        pack.error_summary = _clean_text(error_summary, item_chars)
    if recommended_next_action is not None:
        pack.recommended_next_action = _clean_text(recommended_next_action, item_chars)
    for name, value in (("source_provider", source_provider),
                        ("source_model", source_model),
                        ("source_variant", source_variant)):
        if value is not None:
            setattr(pack, name, _clean_text(value, item_chars))
    return pack


def select_lessons(
    candidates: Optional[list],
    *,
    task_type: Optional[str] = None,
    failure_category: Optional[str] = None,
    limit: int = 3,
) -> list:
    """Select relevant existing lessons from reviewed learning candidates.

    This only *reads* records that already went through the learning pipeline
    (destination ``lesson``, lifecycle ``reviewed``/``validated``/``promoted``).
    It never writes or duplicates the learning store.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        limit = 1
    selected: list = []
    for record in candidates or []:
        if not isinstance(record, dict):
            continue
        if record.get("proposed_destination") != "lesson":
            continue
        if record.get("promotion_status") not in LESSON_LIFECYCLE:
            continue
        if task_type and record.get("task_class") and record.get("task_class") != task_type:
            continue
        if failure_category and record.get("failure_category") != failure_category:
            continue
        text = record.get("validated_correction") or record.get("reviewed_root_cause")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            clean = _clean_text(text, MAX_ITEM_CHARS)
        except infra.InfraError:
            continue
        if clean and clean not in selected:
            selected.append(clean)
        if len(selected) >= limit:
            break
    return selected


def validate_context_pack(record: dict) -> dict:
    """Validate one persisted context pack before it is written or loaded."""
    if not isinstance(record, dict):
        raise infra.InfraError("Context pack must be an object")
    unknown = sorted(set(record) - set(CONTEXT_PACK_FIELDS))
    if unknown:
        raise infra.InfraError("Context pack has unknown fields: %s" % ", ".join(unknown))
    missing = sorted(name for name in CONTEXT_PACK_REQUIRED if not record.get(name))
    if missing:
        raise infra.InfraError("Context pack missing fields: %s" % ", ".join(missing))
    if record["context_pack_version"] != CONTEXT_PACK_VERSION:
        raise infra.InfraError("Unsupported context_pack_version")
    for name in TEXT_FIELDS:
        value = record.get(name)
        if value is not None and not isinstance(value, str):
            raise infra.InfraError("Context pack field '%s' must be a string" % name)
    for name in LIST_FIELDS:
        value = record.get(name)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise infra.InfraError("Context pack field '%s' must be a list of strings" % name)
    for name in CONTEXT_PACK_FIELDS:
        value = record.get(name)
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = value
        else:
            values = []
        if any(learning.contains_secret_like(item) for item in values):
            raise infra.InfraError(
                "Context pack field '%s' looks like it may contain a credential; do not record secrets" % name
            )
    if record.get("failure_category") is not None:
        learning.validate_failure_category(record["failure_category"])
    return record


def estimate_tokens(text: Any) -> int:
    """Rough token guard without a tokenizer dependency (ceil chars / 4)."""
    if not isinstance(text, str):
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _append_field(lines: list, title: str, value: Any) -> None:
    if isinstance(value, str) and value.strip():
        lines.append("")
        lines.append("## %s" % title)
        lines.append(value)


def _append_items(lines: list, title: str, values: Any) -> None:
    items = [item for item in (values or []) if isinstance(item, str) and item.strip()]
    if items:
        lines.append("")
        lines.append("## %s" % title)
        lines.extend("- %s" % item for item in items)


def render_context_pack(pack: Any) -> str:
    """Render a pack into deterministic provider-ready text.

    Stable sections (goal, type, constraints, lessons, files, state) come first;
    volatile sections (attempts, failure, tests, observations, next action) and
    provenance come last. No timestamps or random ids are emitted, so rendering
    the same pack twice yields identical text.
    """
    data = pack.as_dict() if isinstance(pack, ContextPack) else validate_context_pack(pack)
    lines = ["context_pack_version: %s" % data["context_pack_version"]]
    _append_field(lines, "Goal", data.get("goal"))
    _append_field(lines, "Task type", data.get("task_type"))
    _append_items(lines, "Constraints", data.get("constraints"))
    _append_items(lines, "Relevant lessons", data.get("lessons"))
    _append_items(lines, "Relevant files", data.get("relevant_files"))
    _append_field(lines, "Current state", data.get("current_state"))
    _append_items(lines, "Approaches tried", data.get("approaches_tried"))
    _append_items(lines, "Changes attempted", data.get("changes_attempted"))
    failure = []
    if data.get("failure_category"):
        failure.append("category: %s" % data["failure_category"])
    if data.get("error_summary"):
        failure.append("summary: %s" % data["error_summary"])
    if failure:
        lines.append("")
        lines.append("## Failure")
        lines.extend("- %s" % item for item in failure)
    _append_items(lines, "Failed tests", data.get("failed_tests"))
    _append_items(lines, "Test results", data.get("test_results"))
    _append_items(lines, "Observations", data.get("observations"))
    _append_field(lines, "Recommended next action", data.get("recommended_next_action"))
    source = []
    if data.get("source_provider"):
        source.append("provider: %s" % data["source_provider"])
    if data.get("source_model"):
        source.append("model: %s" % data["source_model"])
    if data.get("source_variant"):
        source.append("variant: %s" % data["source_variant"])
    if source:
        lines.append("")
        lines.append("## Source")
        lines.extend("- %s" % item for item in source)
    return "\n".join(lines).strip() + "\n"
