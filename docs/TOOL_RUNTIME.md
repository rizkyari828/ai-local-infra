# Tool runtime

The smallest safe and observable tool execution layer for coding-agent
workflows. It gives the agent bounded "hands" (read, search, write, shell, test)
before it gets autonomy. There is no planner, ReAct loop, router, or hosted
provider here.

## Architecture

```text
AgentRuntime
├─ ModelProvider -> OllamaProvider -> Qwen
└─ ToolRuntime
   ├─ file.read
   ├─ file.search
   ├─ file.write
   ├─ shell.exec
   └─ test.run
```

Every tool call:

```text
validate -> enforce workspace/limits -> timeout/cancellation
        -> execute -> normalize result -> classify failure -> emit tool span
```

Provider/model result types (`ProviderResponse`, `RuntimeResult`) stay separate
from tool results (`ToolResult`).

## Session integration

`AgentRuntime.open_session(task)` creates one `TaskSession` that owns the
`trace_id`, `run_id`, runtime budget, cancellation event, and tool-call counter.
Inference and tools share it, so a task produces one correlated trace:

```text
Trace abc
  ├─ inference span
  ├─ file.read span
  ├─ file.search span
  ├─ shell.exec span
  └─ test.run span
```

```python
session = agent.open_session(task)
agent.run(task, session=session)                 # inference
agent.call_tool(session, "file.read", {"path": "src/app.ts"})
```

## Tool contract and registry

`Tool` has `validate()`, `execute()`, and `describe()`; `kind` is one of the span
`tool_kind` values. `ToolRegistry` is a small name -> tool map with
`build_default_registry()`. Add a tool by registering it; runtime execution does
not change.

## Workspace boundary

All file operations are confined to an explicitly constructed
`Workspace(root)`. Paths are normalized and resolved before access, and
containment is checked on real (symlink-resolved) paths:

- absolute paths and Windows/UNC paths are rejected;
- `..` traversal that escapes the root is rejected (`path_escape`);
- symlink escapes resolve outside the root and are rejected;
- writes outside the root are impossible; the shell working directory must be
  inside the root.

This is structural protection, not a command blacklist. A shell command can
still reach the host the way any process can; run tools only in a trusted
workspace.

## Tools

| Tool | kind | Purpose | Key limits |
|---|---|---|---|
| `file.read` | read | read a bounded byte/line window | `max_file_read_bytes` |
| `file.search` | search | substring/regex search, bounded results | `max_search_results`, `max_shell_output_bytes` |
| `file.write` | write | atomic temp -> fsync -> replace | `max_file_write_bytes` |
| `shell.exec` | shell | one bounded command, explicit env, cwd in root | `tool_timeout_seconds`, `max_shell_output_bytes` |
| `test.run` | test | run tests, normalize exit code and parsed counts | same as shell |

`file.read` never loads more than the read cap and marks `truncated`. File
search is pure stdlib (no platform dependency), skips binary files and `.git`,
and bounds result count and output size. `file.write` never leaves a partial
file. `test.run`/`shell.exec` capture exit code and bounded stdout/stderr, with
stdout/stderr truncated deterministically.

`test.run` reports `passed`/`failed`/`skipped`/`errors`/`result_count` only when
a pytest/unittest summary is parsed; otherwise counts stay `null` and only the
exit code and bounded output are preserved.

Child processes get an explicit environment (a small allowlist such as `PATH`,
`HOME`, `TMPDIR`) instead of the full parent environment, so host credentials are
not forwarded by default.

## Limits actually enforced

From [`../config/limits.yaml`](../config/limits.yaml) `tools`:

- `tool_timeout_seconds` — per-call deadline (kill on expiry).
- `max_tool_calls` — per-session tool-call budget.
- `max_file_read_bytes` — read/scan cap.
- `max_file_write_bytes` — write cap.
- `max_shell_output_bytes` — stdout/stderr/output cap.
- `max_search_results` — result cap.

Exceeding `max_tool_calls` stops the call, returns `resource_limit`
(`max_tool_calls_exceeded`), and still emits a tool span. `trace-report` warns
when a trace's `tool_calls` exceed `max_tool_calls`.

## Automatic telemetry

Each tool call emits one validated `tool` span (no source, prompts, or raw
command output):

```text
trace_id, run_id, span_id, stage=tool, tool_name, tool_kind, tool_outcome
attempt, duration_ms, error_category, error_code, limits_version
target_hash, bytes_read, bytes_written, exit_code, result_count
```

`target_hash` is the existing privacy-preserving digest. Repeated-read detection
uses it: repeated `file.read` calls of the same path share a `target_hash`, and
`trace-report` reports `repeated_reads`. v0.1 detects, measures, and reports;
it does not block repeated reads. To change a file's identity, the path must
change; content-change detection is deferred.

## Error classification

Tool failures reuse `telemetry.RUNTIME_ERROR_CATEGORIES`: `tool_failure`,
`tool_timeout`, `validation_failure`, `environment_failure`, `resource_limit`,
`user_cancelled`, `configuration_failure`, `unknown`. Tool and environment
failures are never recorded as `model_reasoning_failure`, so they never become
negative learning signal about the model.

## Cancellation

`shell.exec`/`test.run` poll the session cancellation event and kill the process
group, returning `user_cancelled` rather than `unknown` and leaving no runaway
children where practical. File tools are bounded and fast.

## Testing

Tests use a temporary workspace and deterministic commands; no Ollama, Qwen,
GPU, or destructive operation outside the temp directory is required. Coverage
includes traversal/absolute/symlink escape, bounded read/write, atomic failure,
shell success/non-zero/timeout/cancellation/truncation, test normalization,
tool-call budget, automatic spans, trace continuity, repeated-read hashing,
secret redaction, and limit configuration validation.

## Not in scope

No autonomous tool selection, ReAct/planner loop, DeepSeek/Codex provider,
escalation, routing, vector search, queueing, containers, distributed
execution, arbitrary remote shell, or browser automation.
