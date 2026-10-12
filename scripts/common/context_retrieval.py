"""Context Retrieval Optimization V0.1 — minimum sufficient, bounded, provenance-aware.

Deterministic retrieval only: no embeddings, no vector DB, no AI summarizer, no AI
planner. It turns a task's context request into a bounded :class:`ContextPack` plus a
provenance :class:`ContextManifest`, enforcing a symbolic budget (TINY/SMALL/MEDIUM/
LARGE) that maps to measurable character ceilings from config. Required context is never
silently dropped: if it cannot safely fit, the pack is rejected with a bounded status and
the AI worker is not called.

Reuses existing knowledge/config as authoritative sources (never duplicated). Graphify is
optional and never rebuilt here; a stale/missing graph falls back to deterministic
repository/file lookup.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import learning  # scripts/common, on sys.path (secret guard)
import derived_knowledge as knowledge  # scripts/common, on sys.path

ROOT = infra.ROOT
BUDGETS_PATH = ROOT / "config/context-budgets.yaml"
DEFAULT_BUDGETS = {"TINY": 4000, "SMALL": 12000, "MEDIUM": 40000, "LARGE": 120000}

SOURCE_KINDS = ("OBJECTIVE_SUMMARY", "ACCEPTANCE_CRITERIA", "DEPENDENCY_FACT", "FILE_RANGE",
                "SYMBOL", "DIFF", "CONFIG_VALUE", "KNOWLEDGE", "RULE", "RUNTIME_FACT")
# Lower number = higher retrieval priority.
SOURCE_PRIORITY = {kind: index for index, kind in enumerate(
    ("OBJECTIVE_SUMMARY", "ACCEPTANCE_CRITERIA", "DEPENDENCY_FACT", "RUNTIME_FACT", "CONFIG_VALUE",
     "SYMBOL", "FILE_RANGE", "DIFF", "KNOWLEDGE", "RULE"))}

STATUS_READY = "CONTEXT_READY"
STATUS_BUDGET = "CONTEXT_BUDGET_INSUFFICIENT"
STATUS_MISSING = "CONTEXT_REQUIRED_SOURCE_MISSING"
STATUS_UNSAFE = "CONTEXT_UNSAFE"
STATUS_STALE = "CONTEXT_STALE_SOURCE"
VALIDATION_STATES = (STATUS_READY, STATUS_BUDGET, STATUS_MISSING, STATUS_UNSAFE, STATUS_STALE)

GRAPHIFY_CURRENT, GRAPHIFY_STALE, GRAPHIFY_MISSING, GRAPHIFY_INVALID = (
    "CURRENT", "STALE", "MISSING", "INVALID")

MAX_ITEM_CHARS = 24000
MAX_FILE_BYTES = 262144
REDACTED = "[REDACTED]"


class ContextError(RuntimeError):
    pass


def load_budgets(path: pathlib.Path = BUDGETS_PATH) -> dict[str, int]:
    try:
        data = infra.load_json(path)
    except infra.InfraError:
        return dict(DEFAULT_BUDGETS)
    budgets = {key: int(value) for key, value in (data.get("budgets") or {}).items()
               if key in DEFAULT_BUDGETS and int(value) > 0}
    return {**DEFAULT_BUDGETS, **budgets}


def _fingerprint(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def _safe_path(root: pathlib.Path, value: str) -> pathlib.Path:
    if not value or value.startswith(("/", "~")) or "\x00" in value:
        raise ContextError(f"unsafe path reference: {value!r}")
    candidate = (root / value).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise ContextError(f"path escapes repository: {value}")
    return candidate


def _is_binary(path: pathlib.Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\x00" in chunk


def _redact(text: str) -> tuple[str, bool]:
    """Redact secret-like *values* while keeping structure where possible."""
    if not learning.contains_secret_like(text):
        return text, False
    lines = []
    for line in text.splitlines():
        if learning.contains_secret_like(line):
            key = line.split(":", 1)[0].split("=", 1)[0].strip()
            lines.append(f"{key}: {REDACTED}" if key else REDACTED)
        else:
            lines.append(line)
    return "\n".join(lines), True


def graphify_status(repo_root: pathlib.Path, head: Optional[str]) -> str:
    """Cheap provenance check of an existing external graph; never rebuilds."""
    graph_dir = None
    if infra.STATE_DIR:
        graph_dir = pathlib.Path(infra.STATE_DIR).parent / "share/local-ai-infra/graphify/local-ai-infra/graphify-out"
    if graph_dir is None or not graph_dir.exists():
        return GRAPHIFY_MISSING
    manifest_path = graph_dir / "lai-freshness.json"
    if not manifest_path.exists():
        return GRAPHIFY_MISSING
    try:
        manifest = infra.load_json(manifest_path)
    except infra.InfraError:
        return GRAPHIFY_INVALID
    if manifest.get("git_head") and head and manifest["git_head"] != head:
        return GRAPHIFY_STALE
    return GRAPHIFY_CURRENT if manifest.get("git_head") else GRAPHIFY_INVALID


class ContextRequest(dict):
    """Bounded description of what a task needs. Never contains the raw master prompt."""

    @classmethod
    def build(cls, *, task_id: str, task_family: Optional[str], task_class: Optional[str],
              executor_type: str, context_budget: str, objective_summary: str,
              acceptance_criteria: list[str], dependency_facts: Optional[list[dict]] = None,
              source_refs: Optional[list[dict]] = None, symbol_refs: Optional[list[dict]] = None,
              config_refs: Optional[list[dict]] = None, knowledge_refs: Optional[list[str]] = None,
              diff_refs: Optional[list[dict]] = None,
              repository: Optional[dict] = None) -> "ContextRequest":
        return cls({
            "task_id": task_id, "task_family": task_family, "task_class": task_class,
            "executor_type": executor_type, "context_budget": context_budget or "SMALL",
            "objective_summary": objective_summary, "acceptance_criteria": list(acceptance_criteria or []),
            "dependency_facts": list(dependency_facts or []), "source_refs": list(source_refs or []),
            "symbol_refs": list(symbol_refs or []), "config_refs": list(config_refs or []),
            "knowledge_refs": list(knowledge_refs or []), "diff_refs": list(diff_refs or []),
            "repository": dict(repository or {}),
        })


def _item(source_kind: str, *, source_id: str, content: str, reason: str, required: bool,
          path: Optional[str] = None, symbol: Optional[str] = None, line_range: Optional[list[int]] = None,
          knowledge_id: Optional[str] = None, priority: Optional[int] = None) -> dict[str, Any]:
    if source_kind not in SOURCE_KINDS:
        raise ContextError(f"unknown source kind: {source_kind}")
    item = {"source_kind": source_kind, "source_id": source_id, "path": path, "symbol": symbol,
            "range": line_range, "knowledge_id": knowledge_id, "reason": reason,
            "priority": priority if priority is not None else SOURCE_PRIORITY[source_kind],
            "required": bool(required), "content": content, "fingerprint": None,
            "size": 0, "truncated": False, "redacted": False}
    return _finalize_item(item, content)


def _finalize_item(item: dict[str, Any], content: str) -> dict[str, Any]:
    """Redact + bound the FINAL delivered content, then fingerprint THAT content.

    Any change to the final delivered characters (including inside merged ranges)
    must change the item fingerprint; the fingerprint is never derived from
    pre-merge or pre-sanitized content."""
    content, redacted = _redact(content)
    truncated = len(content) > MAX_ITEM_CHARS
    if truncated:
        content = content[:MAX_ITEM_CHARS]
    item["content"] = content
    item["size"] = len(content)
    item["truncated"] = truncated
    item["redacted"] = redacted
    item["fingerprint"] = _fingerprint(item["source_kind"], item["source_id"], item["path"],
                                       item["symbol"], content)
    return item


def _symbol_range(path: pathlib.Path, symbol: str, context: int = 40) -> Optional[tuple[int, int]]:
    match = re.compile(rf"^\s*(?:def|class)\s+{re.escape(symbol)}\b", re.MULTILINE)
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    found = match.search(text)
    if not found:
        return None
    start = text[:found.start()].count("\n") + 1
    return start, start + context


def _read_range(path: pathlib.Path, start: int, end: int) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ContextError("file too large for direct context")
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    return "\n".join(lines[max(0, start - 1):end])


def retrieve(request: ContextRequest, *, root: Optional[pathlib.Path] = None,
             graph_status: Optional[str] = None) -> tuple[list[dict[str, Any]], bool, Optional[str]]:
    """Deterministically retrieve candidate items. Returns (items, fallback_used, error)."""
    root = pathlib.Path(root or ROOT)
    items: list[dict[str, Any]] = []
    fallback_used = False
    items.append(_item("OBJECTIVE_SUMMARY", source_id=request["task_id"],
                       content=request["objective_summary"], reason="task objective", required=True))
    if request["acceptance_criteria"]:
        items.append(_item("ACCEPTANCE_CRITERIA", source_id=request["task_id"],
                           content="\n".join(request["acceptance_criteria"]),
                           reason="acceptance criteria", required=True))
    for fact in request["dependency_facts"]:
        items.append(_item("DEPENDENCY_FACT", source_id=fact.get("task_id", "?"),
                           content=json.dumps(fact, sort_keys=True), reason="dependency result",
                           required=bool(fact.get("required", True))))
    for ref in request["source_refs"]:
        try:
            path = _safe_path(root, ref["path"])
        except ContextError as exc:
            return items, fallback_used, str(exc)
        if not path.exists() or _is_binary(path):
            return items, fallback_used, f"required source missing/unsupported: {ref['path']}"
        try:
            content = _read_range(path, int(ref.get("start", 1)), int(ref.get("end", 200)))
        except (ContextError, OSError, UnicodeDecodeError) as exc:
            return items, fallback_used, f"cannot read {ref['path']}: {type(exc).__name__}"
        items.append(_item("FILE_RANGE", source_id=ref["path"], content=content,
                           reason=ref.get("reason", "explicit reference"), required=bool(ref.get("required", True)),
                           path=ref["path"], line_range=[int(ref.get("start", 1)), int(ref.get("end", 200))]))
    for ref in request["symbol_refs"]:
        try:
            path = _safe_path(root, ref["path"])
        except ContextError as exc:
            return items, fallback_used, str(exc)
        located = _symbol_range(path, ref["symbol"]) if path.exists() else None
        if located is None:
            fallback_used = True
            return items, fallback_used, f"symbol not found: {ref['symbol']}"
        start, end = located
        items.append(_item("SYMBOL", source_id=f"{ref['path']}::{ref['symbol']}",
                           content=_read_range(path, start, end), reason=ref.get("reason", "target symbol"),
                           required=bool(ref.get("required", True)), path=ref["path"], symbol=ref["symbol"],
                           line_range=[start, end]))
    for ref in request["config_refs"]:
        try:
            path = _safe_path(root, ref["path"])
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ContextError, OSError, json.JSONDecodeError) as exc:
            return items, fallback_used, f"config unreadable: {ref.get('path')} ({type(exc).__name__})"
        node: Any = data
        for part in str(ref.get("key", "")).split("."):
            node = node.get(part) if isinstance(node, dict) else None
        items.append(_item("CONFIG_VALUE", source_id=f"{ref['path']}:{ref.get('key')}",
                           content=json.dumps({ref.get("key"): node}, sort_keys=True),
                           reason="structured config value", required=bool(ref.get("required", True)),
                           path=ref["path"]))
    for ref in request["diff_refs"]:
        try:
            path = _safe_path(root, ref["path"])
        except ContextError as exc:
            return items, fallback_used, str(exc)
        if not path.exists():
            return items, fallback_used, f"diff target missing: {ref['path']}"
        # Bounded diff range: use the referenced explicit range (deterministic), not a full diff.
        content = _read_range(path, int(ref.get("start", 1)), int(ref.get("end", 160)))
        items.append(_item("DIFF", source_id=f"{ref['path']}:changed", content=content,
                           reason="changed range", required=bool(ref.get("required", True)),
                           path=ref["path"], line_range=[int(ref.get("start", 1)), int(ref.get("end", 160))]))
    for lesson_id in request["knowledge_refs"]:
        for lesson in knowledge.load_lessons():
            if lesson["lesson_id"] == lesson_id:
                items.append(_item("KNOWLEDGE", source_id=lesson_id, knowledge_id=lesson_id,
                                   content=lesson["lesson"], reason="verified knowledge", required=False))
                break
        else:
            for failure in knowledge.load_failures():
                if failure["failure_id"] == lesson_id:
                    items.append(_item("KNOWLEDGE", source_id=lesson_id, knowledge_id=lesson_id,
                                       content=failure["lesson"], reason="verified failure lesson", required=False))
                    break
    return items, fallback_used, None


def deduplicate(items: list[dict[str, Any]], *,
                root: Optional[pathlib.Path] = None) -> tuple[list[dict[str, Any]], int]:
    """Merge identical content across sources and overlapping file ranges.

    Overlap merging re-reads the authoritative file from the SAME workspace root the
    context was retrieved from, then re-redacts and re-fingerprints the merged final
    content. Sanitization therefore always applies to the final delivered content and
    a previously removed secret can never reappear through a merge."""
    root = pathlib.Path(root or ROOT)
    by_fingerprint: dict[str, dict[str, Any]] = {}
    dropped = 0
    for item in items:
        existing = by_fingerprint.get(item["fingerprint"])
        if existing is None:
            by_fingerprint[item["fingerprint"]] = item
            continue
        dropped += item["size"]
        existing["required"] = existing["required"] or item["required"]
        if item["reason"] not in existing["reason"]:
            existing["reason"] += f"; {item['reason']}"
    merged = list(by_fingerprint.values())
    # Overlap merge for same-path ranges.
    by_path: dict[str, list[dict[str, Any]]] = {}
    others: list[dict[str, Any]] = []
    for item in merged:
        if item["source_kind"] in ("FILE_RANGE", "SYMBOL", "DIFF") and item.get("path"):
            by_path.setdefault(item["path"], []).append(item)
        else:
            others.append(item)
    for path, group in by_path.items():
        group.sort(key=lambda i: (i["range"] or [0, 0])[0])
        current = group[0]
        for nxt in group[1:]:
            cur_range, next_range = current["range"] or [0, 0], nxt["range"] or [0, 0]
            if next_range[0] <= cur_range[1] + 1:  # overlapping/adjacent
                end = max(cur_range[1], next_range[1])
                current["range"] = [cur_range[0], end]
                target = _safe_path(root, path)
                if target.exists():
                    content = _read_range(target, current["range"][0], end)
                else:
                    content = current["content"] + "\n" + nxt["content"]
                _finalize_item(current, content)
                current["required"] = current["required"] or nxt["required"]
                dropped += nxt["size"]
            else:
                others.append(current)
                current = nxt
        others.append(current)
    return others, dropped


def enforce_budget(items: list[dict[str, Any]], budget: str, budgets: dict[str, int]) -> tuple[list[dict], list[dict], bool]:
    """Ceiling, not target. Required items are never dropped; optional items are added
    only while they fit, so optional context can NEVER push the final pack over budget.
    When required context alone exceeds the ceiling the pack is refused (fits=False)."""
    limit = budgets.get(budget, budgets["SMALL"])
    required = sorted((item for item in items if item["required"]), key=lambda i: i["priority"])
    optional = sorted((item for item in items if not item["required"]), key=lambda i: i["priority"])
    required_total = sum(item["size"] for item in required)
    if required_total > limit:
        return required, optional, False  # required context cannot fit
    used = required_total
    kept_optional: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for item in optional:
        if used + item["size"] <= limit:
            kept_optional.append(item)
            used += item["size"]
        else:
            omitted.append(item)
    kept = sorted(required + kept_optional, key=lambda i: i["priority"])
    return kept, omitted, True


def validate(items: list[dict[str, Any]], *, fits: bool, required_error: Optional[str],
             graph_status: str, used_graphify: bool, required_truncated: bool = False) -> str:
    if required_error:
        return STATUS_UNSAFE if "escapes" in required_error else STATUS_MISSING
    if not fits:
        return STATUS_BUDGET
    if required_truncated:
        # Required content would be materially truncated: fail closed rather than
        # deliver a silently incomplete pack. No bounded/truncated representation is
        # opted in by the task contract in this milestone.
        return STATUS_BUDGET
    if used_graphify and graph_status in (GRAPHIFY_STALE, GRAPHIFY_INVALID):
        return STATUS_STALE
    return STATUS_READY


def manifest_fingerprint(request: ContextRequest, items: list[dict[str, Any]], budget: str) -> str:
    return _fingerprint("manifest", request["task_id"], budget,
                        sorted(item["fingerprint"] for item in items))


def build_pack(request: ContextRequest, *, root: Optional[pathlib.Path] = None,
               graph_status: Optional[str] = None, budgets: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """Build a validated ContextPack + provenance manifest. No AI call is made here."""
    if request.get("executor_type") == "TOOL_ONLY":
        return {"status": STATUS_READY, "items": [], "omitted": [], "pack": None,
                "manifest": {"task_id": request["task_id"], "executor_type": "TOOL_ONLY",
                             "context_required": False, "item_count": 0, "size": 0,
                             "budget": request.get("context_budget"), "graphify_status": None,
                             "graphify_used": False, "fallback_used": False,
                             "manifest_fingerprint": _fingerprint("tool-only", request["task_id"])}}
    root = pathlib.Path(root or ROOT)
    budgets = budgets or load_budgets()
    graph_status = graph_status or graphify_status(root, (request.get("repository") or {}).get("head"))
    items, fallback_used, error = retrieve(request, root=root, graph_status=graph_status)
    items, dropped_duplicates = deduplicate(items, root=root)
    kept, omitted, fits = enforce_budget(items, request["context_budget"], budgets)
    used_graphify = False  # deterministic retrieval used; Graphify is advisory only in V0.1
    required_truncated = any(i["truncated"] for i in kept if i["required"])
    status = validate(kept, fits=fits, required_error=error, graph_status=graph_status,
                      used_graphify=used_graphify, required_truncated=required_truncated)
    manifest = {
        "task_id": request["task_id"], "executor_type": request["executor_type"],
        "context_budget": request["context_budget"], "budget_limit": budgets.get(request["context_budget"]),
        "item_count": len(kept), "size": sum(i["size"] for i in kept),
        "required_count": sum(1 for i in kept if i["required"]),
        "optional_count": sum(1 for i in kept if not i["required"]),
        "omitted_count": len(omitted), "duplicate_chars_avoided": dropped_duplicates,
        "required_fits": fits, "required_truncated": required_truncated,
        "graphify_status": graph_status, "graphify_used": used_graphify,
        "fallback_used": fallback_used or graph_status != GRAPHIFY_CURRENT,
        "items": [{"source_kind": i["source_kind"], "source_id": i["source_id"], "path": i["path"],
                   "symbol": i["symbol"], "range": i["range"], "fingerprint": i["fingerprint"],
                   "size": i["size"], "required": i["required"], "reason": i["reason"],
                   "redacted": i["redacted"], "truncated": i["truncated"]} for i in kept],
        "error": error,
    }
    manifest["manifest_fingerprint"] = manifest_fingerprint(request, kept, request["context_budget"])
    pack = {"objective_summary": request["objective_summary"],
            "acceptance_criteria": request["acceptance_criteria"],
            "items": [{"source_kind": i["source_kind"], "source_id": i["source_id"],
                       "content": i["content"]} for i in kept]}
    return {"status": status, "items": kept, "omitted": omitted, "pack": pack, "manifest": manifest}


def write_manifest(state_dir: pathlib.Path, run_id: str, task_id: str, manifest: dict[str, Any]) -> pathlib.Path:
    """Persist a bounded manifest (no bodies) under the local state dir (never Git)."""
    target = pathlib.Path(state_dir) / "orchestration" / run_id / "context" / f"{task_id}.json"
    if infra.ROOT.resolve() in target.resolve().parents:
        raise ContextError("context manifest must not be written inside the repository")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return target
