from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import infra  # noqa: E402
import learning  # noqa: E402
import providers  # noqa: E402
import runtime  # noqa: E402
import telemetry  # noqa: E402
import tool_runtime  # noqa: E402
import tools  # noqa: E402


def make_limits(**tool_overrides: object) -> dict:
    limits = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
    limits["tools"].update(tool_overrides)
    return limits


def read_spans(path: pathlib.Path) -> list:
    return learning.load_jsonl(path)


class RaisingTool(tools.Tool):
    name = "test.raise"
    kind = "other"

    def execute(self, params, context):
        raise RuntimeError("unexpected bug")


class EchoTool(tools.Tool):
    name = "test.echo"
    kind = "other"

    def execute(self, params, context):
        return tools.ToolOutput(output=str(params.get("value", "")), metadata={"result_count": 1})


class ToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temp.name)
        self.ws = self.base / "workspace"
        self.ws.mkdir()
        self.spans = self.base / "spans.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def engine(self, registry=None, **limit_overrides: object) -> tool_runtime.ToolRuntime:
        limits = make_limits(**limit_overrides)
        return tool_runtime.ToolRuntime(self.ws, registry=registry, limits=limits, span_output=self.spans)

    def write(self, relative: str, content: str) -> pathlib.Path:
        path = self.ws / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class RegistryTests(ToolTestCase):
    def test_default_registry_names(self) -> None:
        registry = tools.build_default_registry()
        self.assertEqual(["file.read", "file.search", "file.write", "shell.exec", "test.run"], registry.names())

    def test_register_custom_tool(self) -> None:
        registry = tools.build_default_registry()
        registry.register(EchoTool())
        self.assertIn("test.echo", registry.names())
        self.assertEqual("test.echo", registry.get("test.echo").name)

    def test_unknown_tool_is_configuration_failure(self) -> None:
        result = self.engine().call("does.not.exist", {}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("configuration_failure", result.error_category)
        self.assertEqual("unknown_tool", result.error_code)
        self.assertEqual(1, len(read_spans(self.spans)))


class WorkspaceTests(ToolTestCase):
    def test_resolve_inside_workspace(self) -> None:
        target = self.write("src/app.py", "print('hi')")
        workspace = tools.Workspace(self.ws)
        resolved = workspace.resolve("src/app.py")
        self.assertEqual(target.resolve(), resolved)
        self.assertEqual("src/app.py", workspace.relative(resolved))

    def test_traversal_rejected(self) -> None:
        workspace = tools.Workspace(self.ws)
        with self.assertRaises(tools.ToolValidationError) as caught:
            workspace.resolve("../outside.txt")
        self.assertEqual("path_escape", caught.exception.code)

    def test_absolute_path_rejected(self) -> None:
        workspace = tools.Workspace(self.ws)
        for value in ("/etc/passwd", "C:\\Windows\\system32"):
            with self.assertRaises(tools.ToolValidationError):
                workspace.resolve(value)

    def test_symlink_escape_rejected(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("no", encoding="utf-8")
        link = self.ws / "link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported here")
        with self.assertRaises(tools.ToolValidationError) as caught:
            tools.Workspace(self.ws).resolve("link/secret.txt")
        self.assertEqual("path_escape", caught.exception.code)

    def test_missing_root_is_configuration_error(self) -> None:
        with self.assertRaises(tools.ToolConfigurationError):
            tools.Workspace(self.base / "missing")

    def test_empty_path_rejected(self) -> None:
        with self.assertRaises(tools.ToolValidationError):
            tools.Workspace(self.ws).resolve("   ")


class FileReadTests(ToolTestCase):
    def test_read_success(self) -> None:
        self.write("a.txt", "one\ntwo\nthree\n")
        result = self.engine().call("file.read", {"path": "a.txt"}, trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertEqual("one\ntwo\nthree", result.output)
        self.assertEqual(14, result.metadata["bytes_read"])
        self.assertEqual(3, result.metadata["lines_read"])
        self.assertFalse(result.metadata["truncated"])

    def test_read_line_range(self) -> None:
        self.write("a.txt", "l1\nl2\nl3\nl4\nl5\n")
        result = self.engine().call("file.read", {"path": "a.txt", "start_line": 2, "end_line": 4},
                                    trace_id=str(uuid.uuid4()))
        self.assertEqual("l2\nl3\nl4", result.output)
        self.assertEqual(3, result.metadata["lines_read"])

    def test_read_is_bounded(self) -> None:
        self.write("big.txt", "x" * 5000)
        result = self.engine(max_file_read_bytes=100).call("file.read", {"path": "big.txt"},
                                                            trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertTrue(result.metadata["truncated"])
        self.assertLessEqual(result.metadata["bytes_read"], 100)

    def test_read_missing_file(self) -> None:
        result = self.engine().call("file.read", {"path": "missing.txt"}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("tool_failure", result.error_category)
        self.assertEqual("not_found", result.error_code)

    def test_read_binary_rejected(self) -> None:
        (self.ws / "bin.dat").write_bytes(b"\x00\x01\x02hello")
        result = self.engine().call("file.read", {"path": "bin.dat"}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("binary_file", result.error_code)

    def test_read_traversal_rejected(self) -> None:
        result = self.engine().call("file.read", {"path": "../outside.txt"}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("validation_failure", result.error_category)
        self.assertEqual("path_escape", result.error_code)


class FileSearchTests(ToolTestCase):
    def test_search_finds_matches(self) -> None:
        self.write("a.py", "alpha\nneedle here\nbeta\n")
        self.write("b.py", "no match\n")
        result = self.engine().call("file.search", {"query": "needle"}, trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertEqual(1, result.metadata["result_count"])
        self.assertIn("a.py:2: needle here", result.output)

    def test_search_pattern_filter(self) -> None:
        self.write("a.py", "needle\n")
        self.write("a.txt", "needle\n")
        result = self.engine().call("file.search", {"query": "needle", "pattern": "*.py"},
                                    trace_id=str(uuid.uuid4()))
        self.assertEqual(1, result.metadata["result_count"])
        self.assertIn("a.py", result.output)

    def test_search_result_limit(self) -> None:
        for index in range(5):
            self.write("f%d.txt" % index, "needle\n")
        result = self.engine().call("file.search", {"query": "needle", "max_results": 2},
                                    trace_id=str(uuid.uuid4()))
        self.assertEqual(2, result.metadata["result_count"])
        self.assertTrue(result.metadata["truncated"])

    def test_search_regex(self) -> None:
        self.write("a.txt", "abc123\nxyz\n")
        result = self.engine().call("file.search", {"query": r"abc\d+", "regex": True},
                                    trace_id=str(uuid.uuid4()))
        self.assertEqual(1, result.metadata["result_count"])

    def test_search_invalid_regex(self) -> None:
        result = self.engine().call("file.search", {"query": "(", "regex": True}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("validation_failure", result.error_category)
        self.assertEqual("invalid_regex", result.error_code)


class FileWriteTests(ToolTestCase):
    def test_write_creates_file(self) -> None:
        result = self.engine().call("file.write", {"path": "new/out.txt", "content": "hello", "mkdirs": True},
                                    trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertEqual("hello", (self.ws / "new/out.txt").read_text(encoding="utf-8"))
        self.assertTrue(result.metadata["created"])
        self.assertEqual(5, result.metadata["bytes_written"])

    def test_write_overwrites(self) -> None:
        self.write("out.txt", "old")
        result = self.engine().call("file.write", {"path": "out.txt", "content": "new"},
                                    trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertFalse(result.metadata["created"])
        self.assertEqual("new", (self.ws / "out.txt").read_text(encoding="utf-8"))

    def test_write_size_limit_preserves_original(self) -> None:
        self.write("out.txt", "original")
        result = self.engine(max_file_write_bytes=4).call("file.write", {"path": "out.txt", "content": "too large"},
                                                           trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("resource_limit", result.error_category)
        self.assertEqual("file_too_large", result.error_code)
        self.assertEqual("original", (self.ws / "out.txt").read_text(encoding="utf-8"))
        self.assertEqual([], list(self.ws.glob(".*.tmp")))

    def test_write_failure_is_atomic(self) -> None:
        self.write("out.txt", "original")
        with unittest.mock.patch.object(tools.os, "replace", side_effect=OSError("boom")):
            result = self.engine().call("file.write", {"path": "out.txt", "content": "changed"},
                                        trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("environment_failure", result.error_category)
        self.assertEqual("original", (self.ws / "out.txt").read_text(encoding="utf-8"))
        self.assertEqual([], list(self.ws.glob(".*.tmp")))

    def test_write_traversal_rejected(self) -> None:
        result = self.engine().call("file.write", {"path": "../escape.txt", "content": "no"},
                                    trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("path_escape", result.error_code)
        self.assertFalse((self.base / "escape.txt").exists())

    def test_write_missing_parent(self) -> None:
        result = self.engine().call("file.write", {"path": "nope/out.txt", "content": "x"},
                                    trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("missing_parent", result.error_code)


class ShellTests(ToolTestCase):
    def test_shell_success(self) -> None:
        result = self.engine().call("shell.exec", {"command": "printf hello"}, trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertEqual(0, result.metadata["exit_code"])
        self.assertIn("hello", result.output)

    def test_shell_nonzero_exit(self) -> None:
        result = self.engine().call("shell.exec", {"command": "exit 3"}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("tool_failure", result.error_category)
        self.assertEqual("nonzero_exit", result.error_code)
        self.assertEqual(3, result.metadata["exit_code"])

    def test_shell_timeout(self) -> None:
        started = time.perf_counter()
        result = self.engine(tool_timeout_seconds=1).call("shell.exec", {"command": "sleep 10"},
                                                          trace_id=str(uuid.uuid4()))
        elapsed = time.perf_counter() - started
        self.assertFalse(result.success)
        self.assertEqual("tool_timeout", result.error_category)
        self.assertLess(elapsed, 5.0)

    def test_shell_cancellation(self) -> None:
        cancel = threading.Event()

        def cancel_soon() -> None:
            time.sleep(0.1)
            cancel.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        started = time.perf_counter()
        result = self.engine().call("shell.exec", {"command": "sleep 10"}, cancel=cancel, trace_id=str(uuid.uuid4()))
        elapsed = time.perf_counter() - started
        self.assertFalse(result.success)
        self.assertEqual("user_cancelled", result.error_category)
        self.assertLess(elapsed, 5.0)

    def test_shell_output_truncated(self) -> None:
        command = '"%s" -c "import sys; sys.stdout.write(\'x\' * 200000)"' % sys.executable
        result = self.engine(max_shell_output_bytes=1000).call("shell.exec", {"command": command},
                                                               trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertTrue(result.metadata["truncated"])
        self.assertLessEqual(len(result.output or ""), 1000)

    def test_shell_cwd_escape_rejected(self) -> None:
        result = self.engine().call("shell.exec", {"command": "pwd", "cwd": "../"}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("path_escape", result.error_code)

    def test_shell_env_override(self) -> None:
        result = self.engine().call("shell.exec", {"command": "printf $MY_VAR", "env": {"MY_VAR": "hello"}},
                                    trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertIn("hello", result.output)

    def test_shell_env_secret_rejected(self) -> None:
        result = self.engine().call("shell.exec", {"command": "true", "env": {"API_KEY": "sk-abcdefghijklmnop"}},
                                    trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("validation_failure", result.error_category)


class TestRunTests(ToolTestCase):
    def test_parsed_success(self) -> None:
        command = '"%s" -c "print(\'2 passed, 1 skipped in 0.01s\')"' % sys.executable
        result = self.engine().call("test.run", {"command": command}, trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertEqual(0, result.metadata["exit_code"])
        self.assertTrue(result.metadata["counts_parsed"])
        self.assertEqual(2, result.metadata["passed"])
        self.assertEqual(1, result.metadata["skipped"])
        self.assertEqual(3, result.metadata["result_count"])

    def test_parsed_failure(self) -> None:
        command = '"%s" -c "print(\'1 failed, 2 passed in 0.01s\'); import sys; sys.exit(1)"' % sys.executable
        result = self.engine().call("test.run", {"command": command}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("tool_failure", result.error_category)
        self.assertEqual("tests_failed", result.error_code)
        self.assertEqual(1, result.metadata["failed"])
        self.assertEqual(1, result.metadata["exit_code"])

    def test_counts_absent_not_fabricated(self) -> None:
        result = self.engine().call("test.run", {"command": "exit 0"}, trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertFalse(result.metadata["counts_parsed"])
        self.assertIsNone(result.metadata["passed"])
        self.assertIsNone(result.metadata["result_count"])


class ToolTelemetryTests(ToolTestCase):
    def test_automatic_tool_span(self) -> None:
        self.write("a.txt", "hello\n")
        result = self.engine().call("file.read", {"path": "a.txt"}, trace_id=str(uuid.uuid4()))
        spans = read_spans(self.spans)
        self.assertEqual(1, len(spans))
        span = spans[0]
        telemetry.validate_span_event(span)
        self.assertEqual("tool", span["stage"])
        self.assertEqual("file.read", span["tool_name"])
        self.assertEqual("read", span["tool_kind"])
        self.assertEqual("succeeded", span["tool_outcome"])
        self.assertEqual(result.trace_id, span["trace_id"])
        self.assertEqual(6, span["bytes_read"])
        self.assertTrue(span["target_hash"].startswith("sha256:"))

    def test_shell_span_records_exit_code(self) -> None:
        self.engine().call("shell.exec", {"command": "exit 2"}, trace_id=str(uuid.uuid4()))
        span = read_spans(self.spans)[0]
        self.assertEqual("failed", span["tool_outcome"])
        self.assertEqual(2, span["exit_code"])
        self.assertEqual("nonzero_exit", span["error_code"])

    def test_unknown_exception_is_unknown(self) -> None:
        registry = tools.ToolRegistry([RaisingTool()])
        result = self.engine(registry=registry).call("test.raise", {}, trace_id=str(uuid.uuid4()))
        self.assertFalse(result.success)
        self.assertEqual("unknown", result.error_category)
        self.assertEqual("unknown", read_spans(self.spans)[0]["error_category"])

    def test_spans_can_be_disabled(self) -> None:
        engine = tool_runtime.ToolRuntime(self.ws, span_output=self.spans, record_spans=False)
        engine.call("file.read", {"path": "missing"}, trace_id=str(uuid.uuid4()))
        self.assertFalse(self.spans.exists())

    def test_secret_redaction(self) -> None:
        self.write("secret.txt", "api_key=SuperSecret12345\n")
        result = self.engine().call("file.read", {"path": "secret.txt"}, trace_id=str(uuid.uuid4()))
        self.assertTrue(result.success)
        self.assertIn("SuperSecret12345", result.output or "")
        self.assertNotIn("SuperSecret12345", json.dumps(read_spans(self.spans)[0]))

    def test_repeated_read_target_hashing(self) -> None:
        self.write("a.txt", "content\n")
        trace_id = str(uuid.uuid4())
        engine = self.engine()
        engine.call("file.read", {"path": "a.txt"}, trace_id=trace_id)
        engine.call("file.read", {"path": "a.txt"}, trace_id=trace_id)
        spans = read_spans(self.spans)
        self.assertEqual({spans[0]["target_hash"]}, {spans[1]["target_hash"]})
        summary = telemetry.summarize_trace(trace_id, spans, limits=telemetry.load_limits())
        self.assertGreaterEqual(summary["repeated_reads"], 1)

    def test_tool_spans_do_not_inflate_attempts(self) -> None:
        self.write("a.txt", "content\n")
        trace_id = str(uuid.uuid4())
        engine = self.engine()
        for _ in range(3):
            engine.call("file.read", {"path": "a.txt"}, trace_id=trace_id)
        spans = read_spans(self.spans)
        self.assertTrue(all(span["attempt"] == 1 for span in spans))
        summary = telemetry.summarize_trace(trace_id, spans, limits=telemetry.load_limits())
        self.assertEqual(1, summary["attempts"])
        self.assertFalse(any("attempts" in warning for warning in summary["warnings"]))

    def test_trace_continuity_with_inference(self) -> None:
        self.write("a.txt", "content\n")
        limits = make_limits()
        engine = tool_runtime.ToolRuntime(self.ws, limits=limits, span_output=self.spans)
        agent = runtime.AgentRuntime(providers.FakeModelProvider(), limits=limits,
                                     span_output=self.spans, tools=engine)
        trace_id = str(uuid.uuid4())
        session = agent.open_session(runtime.AgentTask(prompt="do work", trace_id=trace_id))
        inference = agent.run(session=session, task=runtime.AgentTask(prompt="do work", trace_id=trace_id))
        tool = agent.call_tool(session, "file.read", {"path": "a.txt"})
        self.assertEqual(trace_id, inference.trace_id)
        self.assertEqual(trace_id, tool.trace_id)
        stages = [span["stage"] for span in read_spans(self.spans)]
        self.assertEqual(["inference", "tool"], stages)
        self.assertEqual({trace_id}, {span["trace_id"] for span in read_spans(self.spans)})


class BudgetTests(ToolTestCase):
    def test_tools_not_configured(self) -> None:
        agent = runtime.AgentRuntime(providers.FakeModelProvider(), record_spans=False)
        session = agent.open_session(runtime.AgentTask(prompt="x"))
        result = agent.call_tool(session, "file.read", {"path": "a.txt"})
        self.assertFalse(result.success)
        self.assertEqual("configuration_failure", result.error_category)
        self.assertEqual("tools_not_configured", result.error_code)

    def test_max_tool_calls_enforced(self) -> None:
        self.write("a.txt", "content\n")
        limits = make_limits(max_tool_calls=1)
        engine = tool_runtime.ToolRuntime(self.ws, limits=limits, span_output=self.spans)
        agent = runtime.AgentRuntime(providers.FakeModelProvider(), limits=limits,
                                     span_output=self.spans, tools=engine)
        session = agent.open_session(runtime.AgentTask(prompt="x", trace_id=str(uuid.uuid4())))
        first = agent.call_tool(session, "file.read", {"path": "a.txt"})
        second = agent.call_tool(session, "file.read", {"path": "a.txt"})
        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertEqual("resource_limit", second.error_category)
        self.assertEqual("max_tool_calls_exceeded", second.error_code)
        self.assertEqual(1, session.tool_calls)
        spans = read_spans(self.spans)
        self.assertEqual(2, len(spans))
        self.assertEqual("resource_limit", spans[1]["error_category"])

    def test_session_cancellation(self) -> None:
        cancel = threading.Event()
        cancel.set()
        engine = tool_runtime.ToolRuntime(self.ws, limits=make_limits(), span_output=self.spans)
        agent = runtime.AgentRuntime(providers.FakeModelProvider(), tools=engine, span_output=self.spans)
        session = agent.open_session(runtime.AgentTask(prompt="x", cancel=cancel,
                                                       trace_id=str(uuid.uuid4())))
        result = agent.call_tool(session, "file.read", {"path": "a.txt"})
        self.assertFalse(result.success)
        self.assertEqual("user_cancelled", result.error_category)


class ToolLimitsConfigTests(ToolTestCase):
    def test_load_tool_limits_merges_defaults(self) -> None:
        merged = telemetry.load_tool_limits({"tools": {"max_tool_calls": 7}})
        self.assertEqual(7, merged["max_tool_calls"])
        self.assertEqual(telemetry.DEFAULT_TOOL_LIMITS["max_file_read_bytes"], merged["max_file_read_bytes"])

    def test_invalid_tool_limit_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "limits.yaml"
            bad = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
            bad["tools"]["max_tool_calls"] = 0
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(infra.InfraError):
                telemetry.load_limits(path)

    def test_unknown_tool_limit_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "limits.yaml"
            bad = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
            bad["tools"]["typo_limit"] = 1
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(infra.InfraError):
                telemetry.load_limits(path)

    def test_invalid_workspace_is_configuration_error(self) -> None:
        with self.assertRaises(tools.ToolConfigurationError):
            tool_runtime.ToolRuntime(self.base / "missing")


class ToolCliTests(ToolTestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "scripts/agent-run"), *args],
                              cwd=ROOT, text=True, capture_output=True)

    def test_tool_cli_read(self) -> None:
        self.write("a.txt", "hello\n")
        result = self._run("--tool", "file.read", "--workspace", str(self.ws),
                           "--params", json.dumps({"path": "a.txt"}),
                           "--confirm-run", "--json", "--span-output", str(self.spans))
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["success"])
        self.assertEqual("hello", payload["output"])
        self.assertEqual(1, len(read_spans(self.spans)))

    def test_tool_cli_requires_workspace(self) -> None:
        result = self._run("--tool", "file.read", "--confirm-run")
        self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()
