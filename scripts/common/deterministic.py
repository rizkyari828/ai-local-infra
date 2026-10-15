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
import hashlib
import pathlib
import re
import subprocess
import sys
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import learning  # scripts/common, on sys.path (secret guard)

ROOT = infra.ROOT
TEST_COMMANDS = {"unit_tests": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]}

# Explicit opt-in capture contracts; these are not executable validation expressions.
RESULT_CONTRACTS = ('structured_result', 'review_result')
MAX_RESPONSE_BYTES = 2048


def validate_result_contract(node: dict[str, Any]) -> None:
    criteria = node.get('acceptance_criteria') or []
    declared = [key for key in RESULT_CONTRACTS if key in node or key in criteria]
    if not declared:
        return
    if len(declared) != 1 or node.get('executor') == 'TOOL_ONLY':
        raise ValueError('RESULT_CONTRACT_INVALID')
    key = declared[0]
    spec = node.get(key)
    required = {'kind', 'source', 'allow_explanation'} if key == 'structured_result' else {'source_task_id', 'allow_explanation'}
    if (key not in criteria or not isinstance(spec, dict) or set(spec) != required
            or type(spec.get('allow_explanation')) is not bool):
        raise ValueError('RESULT_CONTRACT_INVALID')
    if key == 'structured_result':
        source = spec['source']
        if (spec['kind'] != 'reverse_text_v1' or not isinstance(source, str)
                or not re.fullmatch(r'[A-Za-z0-9 ._-]{1,128}', source)
                or learning.contains_secret_like(source)):
            raise ValueError('RESULT_CONTRACT_INVALID')
    elif (not isinstance(spec['source_task_id'], str)
          or spec['source_task_id'] not in node.get('depends_on', [])
          or (node.get('execution_requirement') or {}).get('role', (node.get('routing') or {}).get('role')) != 'reviewer'):
        raise ValueError('RESULT_CONTRACT_INVALID')


def verify_response(key: str, spec: dict[str, Any], response: Any, complete: Any) -> dict[str, Any]:
    """Validate one bounded block from the COMPLETE capture, preserving outside text."""
    def verdict(status, reason, fields=None):
        return {'status': status, 'reason': reason, 'fields': fields or {}}
    if not isinstance(response, str) or complete is not True:
        return verdict('UNVERIFIED', 'COMPLETE_CAPTURE_MISSING')
    if len(response.encode('utf-8', 'replace')) > MAX_RESPONSE_BYTES:
        return verdict('FAILED', 'RESPONSE_TOO_LARGE')
    if learning.contains_secret_like(response):
        return verdict('FAILED', 'SENSITIVE_EVIDENCE')
    tag = 'VERIFIABLE_RESULT' if key == 'structured_result' else 'REVIEW_RESULT'
    begin, end = 'BEGIN_' + tag, 'END_' + tag
    if begin not in response and end not in response:
        return verdict('UNVERIFIED', 'RESULT_BLOCK_MISSING')
    lines = response.splitlines()
    if (response.count(begin) != 1 or response.count(end) != 1
            or begin not in lines or end not in lines or lines.index(begin) >= lines.index(end)):
        return verdict('FAILED', 'RESULT_BLOCK_AMBIGUOUS')
    start, stop = lines.index(begin), lines.index(end)
    outside = lines[:start] + lines[stop + 1:]
    if (not spec['allow_explanation'] and any(line.strip() for line in outside)) or any(
            re.match(r'^\s*(SOURCE|REVERSED|LENGTH|REVIEW_VERDICT):', line) for line in outside):
        return verdict('FAILED', 'UNDECLARED_OR_CONFLICTING_TEXT')
    fields = {}
    for line in lines[start + 1:stop]:
        match = re.fullmatch(r'([A-Z_]+): (.*)', line)
        if not match or match[1] in fields:
            return verdict('FAILED', 'RESULT_FIELDS_MALFORMED')
        fields[match[1]] = match[2]
    if key == 'structured_result':
        expected = {'SOURCE': spec['source'], 'REVERSED': spec['source'][::-1], 'LENGTH': str(len(spec['source']))}
        if set(fields) != set(expected):
            return verdict('FAILED', 'RESULT_FIELDS_INVALID')
        return {**verdict('PASSED' if fields == expected else 'FAILED',
                         'DETERMINISTIC_FACTS_MATCH' if fields == expected else 'DETERMINISTIC_FACTS_MISMATCH', fields),
                'expected': expected}
    if set(fields) != {'REVIEW_VERDICT'} or fields['REVIEW_VERDICT'] not in ('PASS', 'FAIL'):
        return verdict('FAILED', 'REVIEW_VERDICT_INVALID')
    return verdict('PASSED' if fields['REVIEW_VERDICT'] == 'PASS' else 'FAILED',
                   'REVIEW_PASS' if fields['REVIEW_VERDICT'] == 'PASS' else 'REVIEW_FAIL', fields)


def evidence_fingerprint(evidence: dict[str, Any]) -> str:
    material = {k: v for k, v in evidence.items() if k != 'evidence_fingerprint'}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode('utf-8')).hexdigest()


def valid_verified_result(value: Any) -> bool:
    """Recheck persisted evidence; neither stored booleans nor its digest prove facts."""
    if not isinstance(value, dict) or value.get('schema') != 'verified-result-v1':
        return False
    try:
        key = value['criterion']
        required = {'schema', 'criterion', 'contract', 'source_task_id', 'source_task_fingerprint',
                    'result_type', 'captured_output', 'verification', 'acceptance', 'evidence_fingerprint'}
        if key == 'review_result':
            required.add('reviewed_source')
        if (set(value) != required or len(json.dumps(value)) > 6000
                or value['acceptance'] != 'PASSED' or key not in RESULT_CONTRACTS
                or value['evidence_fingerprint'] != evidence_fingerprint(value)
                or not isinstance(value['source_task_id'], str) or not value['source_task_id']
                or not isinstance(value['result_type'], str)
                or not isinstance(value['source_task_fingerprint'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', value['source_task_fingerprint'])
                or learning.contains_secret_like(json.dumps(value))):
            return False
        validate_result_contract({'acceptance_criteria': [key], key: value['contract'],
                                  'execution_requirement': {'role': 'reviewer'},
                                  'depends_on': [value['contract'].get('source_task_id')]})
        if key == 'review_result':
            source = value['reviewed_source']
            if (set(source) != {'task_id', 'result_fingerprint', 'evidence_fingerprint'}
                    or source['task_id'] != value['contract']['source_task_id']
                    or not all(isinstance(v, str) and v for v in source.values())):
                return False
        check = verify_response(key, value['contract'], value['captured_output'], True)
        return check['status'] == 'PASSED' and check == value['verification']
    except (KeyError, TypeError, ValueError, AttributeError):
        return False

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
#
# Complete intent coverage: a deterministic operation is only claimed when the
# WHOLE request can be satisfied deterministically. Partial understanding (a
# comparison we cannot evaluate, or an explanatory request that keyword-overlaps
# a repo probe) must NOT be reported as a completed deterministic task.
# --------------------------------------------------------------------------- #

_STRONG_REPO_STATE = (
    "repository state", "repo state", "clean state", "clean tree", "clean working",
    "working tree", "git status", "git state", "current branch", "current head",
    "current commit", "branch and head", "head and branch",
)
_WEAK_REPO_STATE = ("branch", "head", "commit sha")
_TEST_PHRASES = ("run tests", "run the tests", "run focused tests", "run the test suite")
_EXPLANATORY_CUES = ("explain", "describe", "why ", "how ", "walk through", "walkthrough",
                     "teach", "summarize", "summarise")
_EQUALITY_CUES = re.compile(r"(?:==|\bequals\b|\bmatches\b|\bis equal to\b|\bsame as\b)", re.IGNORECASE)
_HASH = r"[0-9a-fA-F]{7,40}"
_HEAD_MATCH = re.compile(
    rf"\b(?:git\s+)?head\b[^0-9a-fA-F]{{0,24}}?(?:equals|matches|==|=|\bis\b)\s*[\"'`]?({_HASH})\b",
    re.IGNORECASE)
_HEAD_MATCH_REV = re.compile(
    rf"[\"'`]?({_HASH})[\"'`]?\s*(?:equals|matches|==)\s*(?:the\s+)?(?:git\s+)?head\b", re.IGNORECASE)
_CONFIG_VALUE = re.compile(
    r"([A-Za-z0-9_./-]+\.(?:json|yaml|yml|toml))\s+(?:key\s+)?([A-Za-z0-9_.\-]+)"
    r"\s+(?:equals|==|=|\bis\b)\s+[\"'`]?([^\"'`\s,]+)", re.IGNORECASE)
_FILE_EXISTS = re.compile(r"\b(?:does|if)\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)\s+exist", re.IGNORECASE)


def _has_explanatory_cue(text: str) -> bool:
    lowered = text.lower()
    return any(cue in lowered for cue in _EXPLANATORY_CUES)


def classify_objective(text: str, *, root: Optional[pathlib.Path] = None) -> list[dict[str, Any]]:
    """Return deterministic tool tasks only for explicit, safe phrases whose WHOLE
    intent is captured. Never maps arbitrary prose to a command; never claims a
    comparison or explanation was performed when only a partial probe matched."""
    root = pathlib.Path(root or ROOT)
    lowered = (text or "").lower()
    if not lowered.strip() or _has_explanatory_cue(text):
        return []
    tasks: list[dict[str, Any]] = []
    equality = _EQUALITY_CUES.search(text) is not None
    understood_equality = False

    head_match = _HEAD_MATCH.search(text) or _HEAD_MATCH_REV.search(text)
    if head_match:
        tasks.append({"executor": "TOOL_ONLY", "operation": "git_head_matches",
                      "args": {"expected": head_match.group(1).lower()}})
        understood_equality = True

    config_match = _CONFIG_VALUE.search(text)
    if config_match and (root / config_match.group(1)).exists():
        tasks.append({"executor": "TOOL_ONLY", "operation": "config_value_matches",
                      "args": {"path": config_match.group(1), "key": config_match.group(2),
                               "expected": config_match.group(3)}})
        understood_equality = True

    # A stated equality we could not evaluate must not be reported as done.
    if equality and not understood_equality:
        return []

    filename_match = _FILE_EXISTS.search(text)
    if filename_match:
        tasks.append({"executor": "TOOL_ONLY", "operation": "file_exists",
                      "args": {"path": filename_match.group(1)}})

    if any(phrase in lowered for phrase in _STRONG_REPO_STATE) or (
            sum(1 for cue in _WEAK_REPO_STATE if cue in lowered) >= 2):
        tasks.append({"executor": "TOOL_ONLY", "operation": "repository_state", "args": {}})

    for token in (text or "").split():
        token = token.strip(".,;:()[]{}\"'`")
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
