"""Explicit human acceptance primitive V0.1 (local, stdlib only).

Judgment work (architecture review, security reasoning, design recommendation, code
review) has no deterministic validator and no trusted structured tool result, so its
result stays UNVERIFIED until a human explicitly accepts or rejects it. Acceptance
binds to the exact task/plan contract identity, the delivered-input identity, the
result identity, the acceptance contract version and the reviewer, with a timestamp.
A stale approval (any bound identity changed) is refused. No UI, no daemon: a tiny
local append-only JSONL record plus a minimal CLI path.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import learning  # scripts/common, on sys.path (secret guard)

ROOT = infra.ROOT
ACCEPTANCE_CONTRACT_VERSION = "acceptance-v2"
ACCEPTED, REJECTED = "ACCEPTED", "REJECTED"
DECISIONS = (ACCEPTED, REJECTED)
MAX_NOTE = 400


class HumanAcceptanceError(RuntimeError):
    pass


def _assert_safe(target: pathlib.Path) -> pathlib.Path:
    resolved = target.expanduser().resolve()
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents:
        raise HumanAcceptanceError(f"refusing human-acceptance state inside the repository: {resolved}")
    return resolved


def records_path(state_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    return pathlib.Path(state_dir or infra.STATE_DIR) / "orchestration" / "human-acceptance.jsonl"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def record(*, run_id: str, task_id: str, decision: str, reviewer: str,
           task_contract_identity: str, delivered_input_identity: str, result_identity: str,
           acceptance_contract: str = ACCEPTANCE_CONTRACT_VERSION, note: Optional[str] = None,
           state_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Persist one explicit human accept/reject bound to exact identities."""
    if decision not in DECISIONS:
        raise HumanAcceptanceError(f"decision must be one of: {', '.join(DECISIONS)}")
    if not reviewer or not str(reviewer).strip():
        raise HumanAcceptanceError("a reviewer marker is required")
    bound = {"task_contract_identity": task_contract_identity,
             "delivered_input_identity": delivered_input_identity,
             "result_identity": result_identity}
    missing = [key for key, value in bound.items() if not value]
    if missing:
        raise HumanAcceptanceError(f"human acceptance requires bound identities: {', '.join(missing)}")
    entry = {"run_id": run_id, "task_id": task_id, "decision": decision,
             "reviewer": str(reviewer).strip(), "acceptance_contract": acceptance_contract,
             "task_contract_identity": task_contract_identity,
             "delivered_input_identity": delivered_input_identity,
             "result_identity": result_identity, "accepted_at": _now()}
    if note:
        text = str(note)
        if len(text) > MAX_NOTE or "\n" in text:
            raise HumanAcceptanceError("note must be short single-line text")
        if learning.contains_secret_like(text):
            raise HumanAcceptanceError("note looks like a credential; refusing to store")
        entry["note"] = text
    path = _assert_safe(records_path(state_dir))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry


def load(*, run_id: str, task_id: str, state_dir: Optional[pathlib.Path] = None) -> Optional[dict[str, Any]]:
    """Return the NEWEST recorded acceptance for a task, or None."""
    path = records_path(state_dir)
    if not path.exists():
        return None
    found: Optional[dict[str, Any]] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("run_id") == run_id and entry.get("task_id") == task_id:
            found = entry
    return found


def verify(entry: Optional[dict[str, Any]], *, task_contract_identity: str,
           delivered_input_identity: str, result_identity: str) -> bool:
    """True only when the recorded decision is ACCEPTED and EVERY bound identity still
    matches the current task/input/result. A stale approval never passes."""
    if not entry or entry.get("decision") != ACCEPTED:
        return False
    return (entry.get("task_contract_identity") == task_contract_identity
            and entry.get("delivered_input_identity") == delivered_input_identity
            and entry.get("result_identity") == result_identity
            and entry.get("acceptance_contract") == ACCEPTANCE_CONTRACT_VERSION)


def is_accepted(*, run_id: str, task_id: str, task_contract_identity: str,
                delivered_input_identity: str, result_identity: str,
                state_dir: Optional[pathlib.Path] = None) -> bool:
    return verify(load(run_id=run_id, task_id=task_id, state_dir=state_dir),
                  task_contract_identity=task_contract_identity,
                  delivered_input_identity=delivered_input_identity, result_identity=result_identity)
