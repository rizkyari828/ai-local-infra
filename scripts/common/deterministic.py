"""Deterministic (TOOL_ONLY) executor V0.1 — bounded, whitelisted, no arbitrary shell.

Represents execution that can be decided WITHOUT semantic/reasoning judgment. It is
NOT an AI tier and never appears in provider/model resolution. Operations take
structured arguments (never a free-form command string) and are read-only in V0.1.

Security: no arbitrary shell; unknown operations, escaping paths, secret-bearing
arguments, and mutating operations under READ_ONLY are rejected. Results are bounded
structured data (no terminal transcripts).
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import learning  # scripts/common, on sys.path (secret guard)

ROOT = infra.ROOT
TEST_COMMANDS = {"unit_tests": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]}

# Whitelisted operations. `mutating=False` for every V0.1 operation.
OPERATIONS = {
    "repository_state": {"mutating": False},
    "git_head_matches": {"mutating": False},
    "file_exists": {"mutating": False},
    "config_parse": {"mutating": False},
    "config_value_matches": {"mutating": False},
    "run_tests": {"mutating": False},
    "finalize": {"mutating": False},
}


class ToolPolicyError(RuntimeError):
    pass


def _safe_relpath(root: pathlib.Path, value: Any) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise ToolPolicyError("path must be a non-empty string")
    candidate = (root / value).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise ToolPolicyError(f"path escapes repository: {value}")
    return candidate


def _reject_secrets(value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if learning.contains_secret_like(text):
        raise ToolPolicyError("argument looks like a credential; refusing deterministic execution")


class ToolExecutor:
    def __init__(self, *, root: Optional[pathlib.Path] = None, timeout: int = 120) -> None:
        self.root = pathlib.Path(root or ROOT)
        self.timeout = timeout

    def _git(self, *args: str) -> Optional[str]:
        try:
            result = subprocess.run(["git", "-C", str(self.root), *args],
                                    check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError):
            return None
        return result.stdout.strip()

    def can_execute(self, task: dict[str, Any]) -> bool:
        try:
            self._validate(task)
        except ToolPolicyError:
            return False
        return True

    def _validate(self, task: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise ToolPolicyError("task must be an object")
        operation = task.get("operation")
        if operation not in OPERATIONS:
            raise ToolPolicyError(f"unknown deterministic operation: {operation!r}")
        if task.get("mode") == "READ_ONLY" and OPERATIONS[operation]["mutating"]:
            raise ToolPolicyError(f"mutating operation {operation!r} blocked under READ_ONLY")
        args = task.get("args") or {}
        if not isinstance(args, dict):
            raise ToolPolicyError("args must be an object")
        _reject_secrets(args)
        if operation in ("file_exists", "config_parse", "config_value_matches"):
            _safe_relpath(self.root, args.get("path"))
        if operation == "run_tests" and args.get("command", "unit_tests") not in TEST_COMMANDS:
            raise ToolPolicyError("unknown/test command not in allowlist")
        return task

    def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        self._validate(task)
        operation = task["operation"]
        args = task.get("args") or {}
        if operation == "repository_state":
            return self._repository_state()
        if operation == "git_head_matches":
            head = self._git("rev-parse", "HEAD")
            return {"executor": "git", "operation": operation, "head": head,
                    "matches": bool(head) and head == args.get("expected")}
        if operation == "file_exists":
            path = _safe_relpath(self.root, args["path"])
            return {"executor": "fs", "operation": operation, "path": args["path"], "exists": path.is_file()}
        if operation == "config_parse":
            path = _safe_relpath(self.root, args["path"])
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return {"executor": "config", "operation": operation, "path": args["path"],
                        "parsed": isinstance(data, dict), "keys": len(data) if isinstance(data, dict) else 0}
            except (OSError, json.JSONDecodeError):
                return {"executor": "config", "operation": operation, "path": args["path"], "parsed": False}
        if operation == "config_value_matches":
            return self._config_value_matches(args)
        if operation == "run_tests":
            return self._run_tests(args)
        if operation == "finalize":
            return {"executor": "orchestrator", "operation": "finalize", "ok": True}
        raise ToolPolicyError(f"unhandled operation {operation!r}")

    def _repository_state(self) -> dict[str, Any]:
        return {"executor": "git", "operation": "repository_state",
                "branch": self._git("rev-parse", "--abbrev-ref", "HEAD"),
                "head": self._git("rev-parse", "HEAD"),
                "clean": not bool(self._git("status", "--porcelain"))}

    def _config_value_matches(self, args: dict[str, Any]) -> dict[str, Any]:
        path = _safe_relpath(self.root, args["path"])
        try:
            node: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"executor": "config", "operation": "config_value_matches", "path": args["path"],
                    "matches": False, "actual": None, "error": "unreadable"}
        for part in str(args.get("key", "")).split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        return {"executor": "config", "operation": "config_value_matches", "path": args["path"],
                "key": args.get("key"), "matches": node == args.get("expected"), "actual": node}

    def _run_tests(self, args: dict[str, Any]) -> dict[str, Any]:
        command = TEST_COMMANDS[args.get("command", "unit_tests")]
        try:
            completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"executor": "tests", "operation": "run_tests", "ok": False, "summary": "timeout"}
        except OSError as exc:
            return {"executor": "tests", "operation": "run_tests", "ok": False, "summary": type(exc).__name__}
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        summary = tail[-1] if tail else ""
        return {"executor": "tests", "operation": "run_tests", "ok": completed.returncode == 0,
                "exit_code": completed.returncode, "summary": summary[-200:]}

    def validate_result(self, task: dict[str, Any], result: dict[str, Any]) -> bool:
        operation = task["operation"]
        if operation in ("repository_state", "file_exists"):
            return "error" not in result
        if operation in ("config_parse",):
            return bool(result.get("parsed"))
        if operation in ("git_head_matches", "config_value_matches"):
            return bool(result.get("matches"))
        if operation == "run_tests":
            return bool(result.get("ok"))
        if operation == "finalize":
            return True
        return False


# --------------------------------------------------------------------------- #
# Conservative objective -> deterministic task mapping (explicit/safe only)
# --------------------------------------------------------------------------- #

_REPO_STATE_PHRASES = ("branch", "head", "clean state", "clean-state", "clean tree",
                       "repository state", "repo state")
_TEST_PHRASES = ("run tests", "run the tests", "run focused tests", "run the test suite")


def classify_objective(text: str, *, root: Optional[pathlib.Path] = None) -> list[dict[str, Any]]:
    """Return deterministic tool tasks only for explicit, safe phrases. Never maps
    arbitrary prose to a command. Config paths must be explicit and in-repo."""
    root = pathlib.Path(root or ROOT)
    lowered = (text or "").lower()
    tasks: list[dict[str, Any]] = []
    if any(phrase in lowered for phrase in _REPO_STATE_PHRASES):
        tasks.append({"executor": "TOOL_ONLY", "operation": "repository_state", "args": {}})
    for token in (text or "").split():
        if token.endswith((".yaml", ".json", ".toml")) and (root / token).exists():
            tasks.append({"executor": "TOOL_ONLY", "operation": "config_parse",
                          "args": {"path": token}})
    if any(phrase in lowered for phrase in _TEST_PHRASES):
        tasks.append({"executor": "TOOL_ONLY", "operation": "run_tests", "args": {"command": "unit_tests"}})
    # de-duplicate by (operation, path)
    seen, unique = set(), []
    for task in tasks:
        key = (task["operation"], json.dumps(task["args"], sort_keys=True))
        if key not in seen:
            seen.add(key)
            unique.append(task)
    return unique
