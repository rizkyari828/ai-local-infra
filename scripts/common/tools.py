#!/usr/bin/env python3
"""Safe, observable tool primitives for the Agent Runtime (stdlib only).

This module owns the *what* of a tool: validation, the workspace boundary, and
execution. It never writes telemetry; ``tool_runtime`` normalizes results and
emits tool spans. Provider/model result types stay separate from tool results.

Security is structural, not a command blacklist: an explicit workspace root,
path normalization, bounded read/write/output sizes, per-call timeouts, and an
explicit child environment. Shell commands are still able to read the host the
way any process can; the workspace boundary constrains the working directory and
file tools, not the operating system. Run tools only in a trusted workspace.
"""

from __future__ import annotations

import fnmatch
import os
import pathlib
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import learning
import telemetry

BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP", "SHELL", "USER")
DEFAULT_SKIP_DIRS = (".git",)
WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
LINE_PREVIEW_LIMIT = 400
PROCESS_POLL_SECONDS = 0.02

PYTEST_COUNT_PATTERN = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|warnings?)")
UNITTEST_COUNT_PATTERN = re.compile(r"(failures|errors|skipped)=(\d+)")
UNITTEST_RAN_PATTERN = re.compile(r"Ran\s+(\d+)\s+tests?")


class ToolError(Exception):
    """A classified tool failure.

    ``category`` is always one of ``telemetry.RUNTIME_ERROR_CATEGORIES``.
    ``output``/``metadata`` may carry an execution result that is still useful
    even though the tool is reported as failed (for example a non-zero exit).
    """

    category = "tool_failure"
    retryable = False
    default_code = "tool_error"

    def __init__(
        self,
        code: Optional[str] = None,
        detail: Optional[str] = None,
        *,
        category: Optional[str] = None,
        retryable: Optional[bool] = None,
        output: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        if category is not None:
            self.category = category if category in telemetry.RUNTIME_ERROR_CATEGORIES else "unknown"
        if retryable is not None:
            self.retryable = bool(retryable)
        self.code = _safe_code(code or self.default_code)
        self.detail = _safe_detail(detail)
        self.output = output
        self.metadata = metadata or {}
        super().__init__("%s:%s" % (self.category, self.code))


class ToolValidationError(ToolError):
    category = "validation_failure"
    default_code = "invalid_request"


class ToolTimeout(ToolError):
    category = "tool_timeout"
    default_code = "timeout"


class ToolFailure(ToolError):
    category = "tool_failure"
    default_code = "failure"


class ToolEnvironmentError(ToolError):
    category = "environment_failure"
    default_code = "environment_error"


class ToolResourceLimit(ToolError):
    category = "resource_limit"
    default_code = "resource_limit"


class ToolCancelled(ToolError):
    category = "user_cancelled"
    default_code = "cancelled"


class ToolConfigurationError(ToolError):
    category = "configuration_failure"
    default_code = "configuration_error"


def _safe_code(value: Any) -> str:
    text = str(value) if value else "error"
    return text if re.match(r"^[A-Za-z0-9_.:-]{1,64}$", text) else "error"


def _safe_detail(value: Any, limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text or learning.contains_secret_like(text):
        return None
    return text[:limit]


class Workspace:
    """An explicit filesystem root that every file operation is confined to."""

    def __init__(self, root: Any) -> None:
        try:
            resolved = pathlib.Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ToolConfigurationError("invalid_workspace", "workspace root does not exist") from None
        if not resolved.is_dir():
            raise ToolConfigurationError("invalid_workspace", "workspace root must be a directory")
        self.root = resolved

    def contains(self, resolved: pathlib.Path) -> bool:
        try:
            resolved.relative_to(self.root)
            return True
        except ValueError:
            return False

    def relative(self, resolved: pathlib.Path) -> str:
        return resolved.relative_to(self.root).as_posix()

    def resolve(self, relative_path: Any, *, must_exist: bool = False, allow_root: bool = False) -> pathlib.Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ToolValidationError("invalid_path", "path must be a non-empty string")
        if "\x00" in relative_path:
            raise ToolValidationError("invalid_path", "path must not contain NUL bytes")
        cleaned = relative_path.strip()
        if WINDOWS_PATH_PATTERN.match(cleaned) or pathlib.PurePosixPath(cleaned).is_absolute():
            raise ToolValidationError("absolute_path_rejected", "absolute paths are not allowed")
        if os.sep == "\\":
            cleaned = cleaned.replace("/", "\\")
        try:
            resolved = (self.root / cleaned).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise ToolValidationError("invalid_path", "path could not be resolved") from None
        if not self.contains(resolved):
            raise ToolValidationError("path_escape", "path escapes the workspace root")
        if resolved == self.root and not allow_root:
            raise ToolValidationError("invalid_path", "path must reference an entry inside the workspace")
        if must_exist and not resolved.exists():
            raise ToolFailure("not_found", "path does not exist")
        return resolved


@dataclass
class ToolOutput:
    """A successful tool result: bounded output plus normalized metadata."""

    output: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    """Everything a tool may rely on for one bounded, cancellable execution."""

    workspace: Workspace
    timeout_seconds: float
    limits: dict
    cancel: Optional[threading.Event] = None
    trace_id: Optional[str] = None
    run_id: Optional[str] = None


class Tool:
    """Minimal tool contract: validate, execute, describe."""

    name = "tool"
    kind = "other"
    description = ""

    def validate(self, params: Any) -> dict:
        if not isinstance(params, dict):
            raise ToolValidationError("invalid_params", "tool parameters must be an object")
        return params

    def execute(self, params: dict, context: ToolContext) -> ToolOutput:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "kind": self.kind, "description": self.description}


def _require_positive_int(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolValidationError("invalid_params", "%s must be a positive integer" % label)
    return value


def _require_positive_number(value: Any, label: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ToolValidationError("invalid_params", "%s must be a positive number" % label)
    return float(value)


class FileReadTool(Tool):
    name = "file.read"
    kind = "read"
    description = "Read a bounded byte/line window from a workspace file."

    def validate(self, params: Any) -> dict:
        params = super().validate(params)
        path = params.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ToolValidationError("invalid_params", "path is required")
        start = _require_positive_int(params.get("start_line"), "start_line")
        end = _require_positive_int(params.get("end_line"), "end_line")
        if start is not None and end is not None and end < start:
            raise ToolValidationError("invalid_params", "end_line must be >= start_line")
        return {"path": path, "start_line": start, "end_line": end}

    def execute(self, params: dict, context: ToolContext) -> ToolOutput:
        cap = context.limits["max_file_read_bytes"]
        resolved = context.workspace.resolve(params["path"], must_exist=True)
        if not resolved.is_file():
            raise ToolFailure("not_a_file", "path is not a regular file")
        try:
            with resolved.open("rb") as handle:
                data = handle.read(cap + 1)
        except OSError:
            raise ToolEnvironmentError("read_failed", "file could not be read") from None
        truncated = len(data) > cap
        data = data[:cap]
        if b"\x00" in data:
            raise ToolFailure("binary_file", "refusing to read a binary file")
        lines = data.decode("utf-8", errors="replace").splitlines()
        start = (params.get("start_line") or 1) - 1
        end = params.get("end_line")
        selected = lines[start:end] if end is not None else lines[start:]
        rel = context.workspace.relative(resolved)
        return ToolOutput(
            output="\n".join(selected),
            metadata={
                "path": rel,
                "bytes_read": len(data),
                "lines_read": len(selected),
                "truncated": truncated,
                "target_hash": telemetry.hash_target(rel),
            },
        )


class FileSearchTool(Tool):
    name = "file.search"
    kind = "search"
    description = "Search workspace text files by substring or regex."

    def validate(self, params: Any) -> dict:
        params = super().validate(params)
        query = params.get("query")
        if not isinstance(query, str) or not query:
            raise ToolValidationError("invalid_params", "query is required")
        path = params.get("path", ".")
        if not isinstance(path, str) or not path.strip():
            raise ToolValidationError("invalid_params", "path must be a non-empty string")
        pattern = params.get("pattern")
        if pattern is not None and (not isinstance(pattern, str) or not pattern):
            raise ToolValidationError("invalid_params", "pattern must be a non-empty string")
        for label in ("regex", "ignore_case"):
            value = params.get(label)
            if value is not None and not isinstance(value, bool):
                raise ToolValidationError("invalid_params", "%s must be true or false" % label)
        max_results = _require_positive_int(params.get("max_results"), "max_results")
        return {"query": query, "path": path, "pattern": pattern,
                "regex": bool(params.get("regex")), "ignore_case": bool(params.get("ignore_case")),
                "max_results": max_results}

    def execute(self, params: dict, context: ToolContext) -> ToolOutput:
        configured_limit = context.limits["max_search_results"]
        limit = min(params["max_results"] or configured_limit, configured_limit)
        per_file_cap = context.limits["max_file_read_bytes"]
        output_cap = context.limits["max_shell_output_bytes"]
        root = context.workspace.resolve(params["path"], must_exist=True, allow_root=True)
        if not root.is_dir():
            raise ToolFailure("not_a_directory", "search path is not a directory")
        matcher = None
        if params["regex"]:
            try:
                matcher = re.compile(params["query"], re.IGNORECASE if params["ignore_case"] else 0)
            except re.error:
                raise ToolValidationError("invalid_regex", "query is not a valid regular expression") from None
        needle = params["query"] if not params["ignore_case"] else params["query"].lower()

        results: list = []
        used = 0
        files_scanned = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in DEFAULT_SKIP_DIRS)
            for filename in sorted(filenames):
                if params["pattern"] and not fnmatch.fnmatch(filename, params["pattern"]):
                    continue
                candidate = pathlib.Path(dirpath) / filename
                try:
                    resolved = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if not resolved.is_file() or not context.workspace.contains(resolved):
                    continue
                files_scanned += 1
                try:
                    with resolved.open("rb") as handle:
                        data = handle.read(per_file_cap)
                    if b"\x00" in data:
                        continue
                except OSError:
                    continue
                text = data.decode("utf-8", errors="replace")
                rel = context.workspace.relative(resolved)
                for number, line in enumerate(text.splitlines(), start=1):
                    haystack = line if not params["ignore_case"] else line.lower()
                    if matcher.search(line) if matcher else needle in haystack:
                        entry = "%s:%d: %s" % (rel, number, line.strip()[:LINE_PREVIEW_LIMIT])
                        results.append(entry)
                        used += len(entry) + 1
                        if len(results) >= limit or used >= output_cap:
                            truncated = True
                            break
                if len(results) >= limit or used >= output_cap:
                    break
            if len(results) >= limit or used >= output_cap:
                break
        return ToolOutput(
            output="\n".join(results),
            metadata={
                "result_count": len(results),
                "files_scanned": files_scanned,
                "truncated": truncated,
            },
        )


class FileWriteTool(Tool):
    name = "file.write"
    kind = "write"
    description = "Atomically write or replace a workspace file."

    def validate(self, params: Any) -> dict:
        params = super().validate(params)
        path = params.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ToolValidationError("invalid_params", "path is required")
        content = params.get("content")
        if not isinstance(content, str):
            raise ToolValidationError("invalid_params", "content must be a string")
        mkdirs = params.get("mkdirs")
        if mkdirs is not None and not isinstance(mkdirs, bool):
            raise ToolValidationError("invalid_params", "mkdirs must be true or false")
        return {"path": path, "content": content, "mkdirs": bool(mkdirs)}

    def execute(self, params: dict, context: ToolContext) -> ToolOutput:
        encoded = params["content"].encode("utf-8")
        if len(encoded) > context.limits["max_file_write_bytes"]:
            raise ToolResourceLimit("file_too_large", "content exceeds the configured write limit")
        resolved = context.workspace.resolve(params["path"])
        if resolved.exists() and resolved.is_dir():
            raise ToolFailure("is_directory", "path is a directory")
        created = not resolved.exists()
        parent = resolved.parent
        if not parent.exists():
            if not params["mkdirs"]:
                raise ToolFailure("missing_parent", "parent directory does not exist")
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ToolEnvironmentError("mkdir_failed", "parent directory could not be created") from None
        try:
            handle_fd, raw = tempfile.mkstemp(prefix=".%s." % resolved.name, suffix=".tmp", dir=str(parent))
        except OSError:
            raise ToolEnvironmentError("write_failed", "temporary file could not be created") from None
        temp_path = pathlib.Path(raw)
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, resolved)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise ToolEnvironmentError("write_failed", "file could not be written") from None
        rel = context.workspace.relative(resolved)
        return ToolOutput(
            output=None,
            metadata={
                "path": rel,
                "bytes_written": len(encoded),
                "created": created,
                "target_hash": telemetry.hash_target(rel),
            },
        )


def build_environment(overrides: Optional[dict]) -> dict:
    env = {key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}
    if not overrides:
        return env
    for key, value in overrides.items():
        if not isinstance(key, str) or not key or "=" in key:
            raise ToolValidationError("invalid_env", "environment variable names must be simple strings")
        if not isinstance(value, str):
            raise ToolValidationError("invalid_env", "environment variable values must be strings")
        if learning.contains_secret_like(value):
            raise ToolValidationError("invalid_env", "environment value looks like a credential")
        env[key] = value
    return env


def _drain(stream: Any, chunks: list, stats: dict, prefix: str, cap: int) -> None:
    total = stored = 0
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            total += len(chunk)
            room = cap - stored
            if room > 0:
                piece = chunk[:room]
                chunks.append(piece)
                stored += len(piece)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
    stats[prefix + "_total"] = total
    stats[prefix + "_stored"] = stored


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            process.kill()
        except OSError:
            pass


@dataclass
class ProcessOutcome:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool


def run_process(command: str, *, cwd: pathlib.Path, env: dict, timeout_seconds: float,
                cancel: Optional[threading.Event], output_cap: int) -> ProcessOutcome:
    """Run one bounded command; kill the process group on timeout or cancellation."""
    if not isinstance(command, str) or not command.strip():
        raise ToolValidationError("empty_command", "command must be a non-empty string")
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        raise ToolEnvironmentError("spawn_failed", "command could not be started") from None
    out_chunks: list = []
    err_chunks: list = []
    stats: dict = {}
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, out_chunks, stats, "out", output_cap), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, err_chunks, stats, "err", output_cap), daemon=True),
    ]
    for reader in readers:
        reader.start()
    started = time.perf_counter()
    timed_out = cancelled = False
    try:
        while True:
            if cancel is not None and cancel.is_set():
                cancelled = True
                _terminate(process)
                break
            if time.perf_counter() - started > timeout_seconds:
                timed_out = True
                _terminate(process)
                break
            if process.poll() is not None:
                break
            time.sleep(PROCESS_POLL_SECONDS)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        for reader in readers:
            reader.join(timeout=2)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    if cancelled:
        raise ToolCancelled("cancelled", "command was cancelled")
    if timed_out:
        raise ToolTimeout("timeout", "command exceeded the configured timeout")
    truncated = stats.get("out_total", 0) > stats.get("out_stored", 0) or \
        stats.get("err_total", 0) > stats.get("err_stored", 0)
    return ProcessOutcome(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=b"".join(out_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(err_chunks).decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
        stdout_bytes=stats.get("out_total", 0),
        stderr_bytes=stats.get("err_total", 0),
        truncated=truncated,
    )


def _combine_output(outcome: ProcessOutcome) -> Optional[str]:
    parts = []
    if outcome.stdout:
        parts.append(outcome.stdout.rstrip("\n"))
    if outcome.stderr:
        parts.append("--- stderr ---\n" + outcome.stderr.rstrip("\n"))
    return "\n".join(parts) if parts else None


class ShellExecTool(Tool):
    name = "shell.exec"
    kind = "shell"
    description = "Run one bounded shell command inside the workspace."

    def validate(self, params: Any) -> dict:
        params = super().validate(params)
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolValidationError("invalid_params", "command is required")
        cwd = params.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ToolValidationError("invalid_params", "cwd must be a non-empty string")
        env = params.get("env")
        if env is not None and not isinstance(env, dict):
            raise ToolValidationError("invalid_params", "env must be an object")
        timeout = _require_positive_number(params.get("timeout_seconds"), "timeout_seconds")
        return {"command": command, "cwd": cwd, "env": env, "timeout_seconds": timeout}

    def execute(self, params: dict, context: ToolContext) -> ToolOutput:
        cwd = context.workspace.resolve(params["cwd"], must_exist=True, allow_root=True)
        if not cwd.is_dir():
            raise ToolFailure("not_a_directory", "working directory is not a directory")
        env = build_environment(params["env"])
        timeout = min(params["timeout_seconds"] or context.timeout_seconds, context.timeout_seconds)
        outcome = run_process(params["command"], cwd=cwd, env=env, timeout_seconds=timeout,
                              cancel=context.cancel, output_cap=context.limits["max_shell_output_bytes"])
        metadata = {
            "exit_code": outcome.exit_code,
            "stdout_bytes": outcome.stdout_bytes,
            "stderr_bytes": outcome.stderr_bytes,
            "truncated": outcome.truncated,
            "target_hash": telemetry.hash_target(" ".join(params["command"].split())),
        }
        output = _combine_output(outcome)
        if outcome.exit_code != 0:
            raise ToolFailure("nonzero_exit", "command exited with %d" % outcome.exit_code,
                              output=output, metadata=metadata)
        return ToolOutput(output=output, metadata=metadata)


def parse_test_counts(text: str) -> dict:
    """Parse pytest/unittest counts only when a reliable summary is present."""
    counts: dict = {}
    ran = UNITTEST_RAN_PATTERN.search(text)
    if ran:
        counts["total"] = int(ran.group(1))
    for label, value in UNITTEST_COUNT_PATTERN.findall(text):
        counts[label if label != "failures" else "failed"] = int(value)
    for number, label in PYTEST_COUNT_PATTERN.findall(text):
        key = "errors" if label in ("error", "errors") else label
        if key != "warnings":
            counts[key] = int(number)
    return counts


class TestRunTool(Tool):
    name = "test.run"
    kind = "test"
    description = "Run a test command and normalize exit code and parsed counts."

    def validate(self, params: Any) -> dict:
        params = super().validate(params)
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolValidationError("invalid_params", "command is required")
        cwd = params.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ToolValidationError("invalid_params", "cwd must be a non-empty string")
        env = params.get("env")
        if env is not None and not isinstance(env, dict):
            raise ToolValidationError("invalid_params", "env must be an object")
        timeout = _require_positive_number(params.get("timeout_seconds"), "timeout_seconds")
        return {"command": command, "cwd": cwd, "env": env, "timeout_seconds": timeout}

    def execute(self, params: dict, context: ToolContext) -> ToolOutput:
        cwd = context.workspace.resolve(params["cwd"], must_exist=True, allow_root=True)
        if not cwd.is_dir():
            raise ToolFailure("not_a_directory", "working directory is not a directory")
        env = build_environment(params["env"])
        timeout = min(params["timeout_seconds"] or context.timeout_seconds, context.timeout_seconds)
        outcome = run_process(params["command"], cwd=cwd, env=env, timeout_seconds=timeout,
                              cancel=context.cancel, output_cap=context.limits["max_shell_output_bytes"])
        output = _combine_output(outcome)
        counts = parse_test_counts((outcome.stdout or "") + "\n" + (outcome.stderr or ""))
        total = counts.get("total")
        if total is None:
            known = [counts.get(key) for key in ("passed", "failed", "errors", "skipped")]
            total = sum(value for value in known if isinstance(value, int)) or None
        metadata = {
            "exit_code": outcome.exit_code,
            "duration_ms": outcome.duration_ms,
            "truncated": outcome.truncated,
            "counts_parsed": bool(counts),
            "passed": counts.get("passed"),
            "failed": counts.get("failed"),
            "skipped": counts.get("skipped"),
            "errors": counts.get("errors"),
            "result_count": total,
            "target_hash": telemetry.hash_target(" ".join(params["command"].split())),
        }
        if outcome.exit_code != 0:
            code = "tests_failed" if (counts.get("failed") or counts.get("errors")) else "nonzero_exit"
            raise ToolFailure(code, "test command exited with %d" % outcome.exit_code,
                              output=output, metadata=metadata)
        return ToolOutput(output=output, metadata=metadata)


class ToolRegistry:
    """A small name -> tool map; extend without touching runtime code."""

    def __init__(self, tools: Optional[list] = None) -> None:
        self._tools: dict = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool) or not tool.name:
            raise ToolConfigurationError("invalid_tool", "a tool must be a Tool with a name")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> list:
        return sorted(self._tools)

    def describe(self) -> list:
        return [self._tools[name].describe() for name in self.names()]


def build_default_registry() -> ToolRegistry:
    return ToolRegistry([
        FileReadTool(),
        FileSearchTool(),
        FileWriteTool(),
        ShellExecTool(),
        TestRunTool(),
    ])
