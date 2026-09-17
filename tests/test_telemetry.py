from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import infra  # noqa: E402
import learning  # noqa: E402
import telemetry  # noqa: E402


def span(**overrides: object) -> dict:
    record = {
        "schema_version": telemetry.SPAN_SCHEMA_VERSION,
        "trace_id": str(uuid.uuid4()),
        "span_id": str(uuid.uuid4()),
        "timestamp_utc": "2026-09-17T10:00:00Z",
        "parent_span_id": None,
        "run_id": None,
        "stage": "inference",
        "duration_ms": 1200.0,
        "model": "qwen3.6:27b-coding",
        "provider": "ollama",
        "role": "student",
        "attempt": 1,
        "retry_count": 0,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "context_size": 8192,
        "context_utilization": 0.5,
        "tool_name": None,
        "tool_kind": None,
        "tool_outcome": None,
        "target_hash": None,
        "repeated": None,
        "error_category": None,
        "error_code": None,
        "limits_version": "0.1.0",
        "sanitized_note": None,
    }
    record.update(overrides)
    return record


def escalation_record(**overrides: object) -> dict:
    record = {field: None for field in learning.ESCALATION_FIELDS}
    record.update({
        "schema_version": "1.1.0",
        "run_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "timestamp_utc": "2026-09-17T10:00:00Z",
        "task_class": "bounded",
        "student_model": "qwen3.6:27b-coding",
        "student_outcome": "failed",
        "failure_category": "test_failure",
        "trace_id": str(uuid.uuid4()),
        "lesson_candidate": False,
        "eval_candidate": False,
        "training_candidate": False,
        "human_review_outcome": "unreviewed",
    })
    record.update(overrides)
    return record


class LimitsTests(unittest.TestCase):
    def test_defaults_when_missing(self) -> None:
        limits = telemetry.load_limits(pathlib.Path("/nonexistent/limits.yaml"))
        self.assertEqual(telemetry.DEFAULT_LIMITS, limits)

    def test_repo_limits_load(self) -> None:
        limits = telemetry.load_limits()
        self.assertIsInstance(limits["timeouts"]["model_timeout_seconds"], int)

    def test_invalid_timeout_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "limits.yaml"
            bad = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
            bad["timeouts"]["model_timeout_seconds"] = 0
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(infra.InfraError):
                telemetry.load_limits(path)

    def test_invalid_utilization_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "limits.yaml"
            bad = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
            bad["anomaly_thresholds"]["context_utilization_warn"] = 1.5
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(infra.InfraError):
                telemetry.load_limits(path)

    def test_max_runtime_below_model_timeout_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "limits.yaml"
            bad = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
            bad["timeouts"]["max_runtime_seconds"] = bad["timeouts"]["model_timeout_seconds"] - 1
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(infra.InfraError):
                telemetry.load_limits(path)


class SpanValidationTests(unittest.TestCase):
    def test_valid_span_passes(self) -> None:
        self.assertIsNotNone(telemetry.validate_span_event(span()))

    def test_schema_matches_fields(self) -> None:
        schema = infra.load_json(ROOT / "telemetry/schemas/span-event.schema.json")
        self.assertEqual(set(schema["properties"]), set(telemetry.SPAN_FIELDS))
        self.assertEqual(set(schema["required"]), set(telemetry.SPAN_REQUIRED))

    def test_bad_stage_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(stage="thinking"))

    def test_negative_duration_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(duration_ms=-1))

    def test_attempt_below_one_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(attempt=0))

    def test_bad_trace_uuid_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(trace_id="not-a-uuid"))

    def test_bad_tool_kind_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(stage="tool", tool_kind="delete"))

    def test_bad_error_category_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(error_category="vibes"))

    def test_raw_target_hash_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(stage="tool", target_hash="src/app.ts"))

    def test_utilization_above_one_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(context_utilization=1.5))

    def test_non_boolean_repeated_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(stage="tool", repeated="yes"))

    def test_secret_note_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(span(sanitized_note="api_key=abcdefgh12345"))

    def test_escalation_schema_matches_fields(self) -> None:
        schema = infra.load_json(ROOT / "telemetry/schemas/escalation-run.schema.json")
        self.assertEqual(set(schema["properties"]), set(learning.ESCALATION_FIELDS))

    def test_escalation_110_requires_trace_id(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation_record(trace_id=None))

    def test_escalation_100_without_trace_id_valid(self) -> None:
        record = escalation_record(schema_version="1.0.0", trace_id=None)
        self.assertIsNotNone(learning.validate_escalation_record(record))


class AiRunValidationTests(unittest.TestCase):
    def test_example_event_passes(self) -> None:
        record = infra.load_json(ROOT / "telemetry/schemas/example.ai-run.json")
        self.assertIsNotNone(telemetry.validate_ai_run_event(record))

    def test_schema_matches_fields(self) -> None:
        schema = infra.load_json(ROOT / "telemetry/schemas/ai-run.schema.json")
        self.assertEqual(set(schema["properties"]), set(telemetry.AI_RUN_FIELDS))
        self.assertEqual(set(schema["required"]), set(telemetry.AI_RUN_REQUIRED))

    def test_missing_status_rejected(self) -> None:
        record = infra.load_json(ROOT / "telemetry/schemas/example.ai-run.json")
        record.pop("status")
        with self.assertRaises(infra.InfraError):
            telemetry.validate_ai_run_event(record)

    def test_negative_duration_rejected(self) -> None:
        record = infra.load_json(ROOT / "telemetry/schemas/example.ai-run.json")
        record["duration_ms"] = -5
        with self.assertRaises(infra.InfraError):
            telemetry.validate_ai_run_event(record)

    def test_bad_status_rejected(self) -> None:
        record = infra.load_json(ROOT / "telemetry/schemas/example.ai-run.json")
        record["status"] = "finished"
        with self.assertRaises(infra.InfraError):
            telemetry.validate_ai_run_event(record)


class HashTests(unittest.TestCase):
    def test_deterministic_and_prefixed(self) -> None:
        first = telemetry.hash_target("src/app.ts")
        self.assertEqual(first, telemetry.hash_target("src/app.ts"))
        self.assertTrue(first.startswith("sha256:"))
        self.assertNotIn("src/app.ts", first)

    def test_empty_target_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.hash_target("   ")


class TraceReportTests(unittest.TestCase):
    def test_summarize_stages_tools_and_warnings(self) -> None:
        trace_id = str(uuid.uuid4())
        target = telemetry.hash_target("src/app.ts")
        spans = [
            span(trace_id=trace_id, stage="inference", duration_ms=100, attempt=2, retry_count=1,
                 input_tokens=1000, output_tokens=100),
            span(trace_id=trace_id, stage="tool", duration_ms=10, tool_kind="read",
                 tool_name="read_file", tool_outcome="succeeded", target_hash=target),
            span(trace_id=trace_id, stage="tool", duration_ms=10, tool_kind="read",
                 tool_name="read_file", tool_outcome="succeeded", target_hash=target),
            span(trace_id=trace_id, stage="tool", duration_ms=10, tool_kind="read",
                 tool_name="read_file", tool_outcome="succeeded", target_hash=target),
            span(trace_id=trace_id, stage="tool", duration_ms=15, tool_kind="shell",
                 tool_name="shell", tool_outcome="timeout", error_category="tool_timeout"),
            span(trace_id=trace_id, stage="validation", duration_ms=50),
            span(trace_id=trace_id, stage="inference", duration_ms=30, context_utilization=0.95),
        ]
        limits = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
        limits["anomaly_thresholds"].update({
            "max_repeated_reads": 1, "max_retries": 0, "max_attempts": 1, "context_utilization_warn": 0.9,
        })
        summary = telemetry.summarize_trace(trace_id, spans, limits=limits)
        self.assertEqual(130.0, summary["duration_by_stage"]["inference"])
        self.assertEqual(45.0, summary["duration_by_stage"]["tool"])
        self.assertEqual(50.0, summary["duration_by_stage"]["validation"])
        self.assertEqual(7, summary["span_count"])
        self.assertEqual(4, summary["tool_calls"])
        self.assertEqual(1, summary["tool_failures"])
        self.assertEqual(1, summary["tool_timeouts"])
        self.assertEqual(3, summary["file_reads"])
        self.assertEqual(1, summary["shell_commands"])
        self.assertEqual(2, summary["repeated_reads"])
        self.assertEqual(1, summary["retries"])
        self.assertEqual(2, summary["attempts"])
        self.assertEqual(1000, summary["input_tokens"])
        self.assertEqual(0.95, summary["max_context_utilization"])
        self.assertEqual(4, len(summary["warnings"]))

    def test_tokens_null_when_unavailable(self) -> None:
        summary = telemetry.summarize_trace(str(uuid.uuid4()), [span(input_tokens=None, output_tokens=None)])
        self.assertIsNone(summary["input_tokens"])
        self.assertIsNone(summary["output_tokens"])

    def test_duplicate_spans_deduped(self) -> None:
        record = span()
        summary = telemetry.summarize_trace(record["trace_id"], [record, dict(record)], limits=telemetry.DEFAULT_LIMITS)
        self.assertEqual(1, summary["span_count"])
        self.assertEqual(1, summary["duplicate_span_count"])

    def test_build_report_links_escalation_and_counts_uncorrelated(self) -> None:
        trace_id = str(uuid.uuid4())
        spans = [span(trace_id=trace_id, stage="inference", duration_ms=100),
                 span(trace_id=None, stage="tool", duration_ms=5)]
        escalations = [escalation_record(trace_id=trace_id)]
        report = telemetry.build_trace_report(spans, escalations)
        self.assertEqual(1, report["trace_count"])
        self.assertEqual(1, report["uncorrelated_span_count"])
        self.assertEqual("qwen3.6:27b-coding", report["traces"][0]["escalation"]["student_model"])


class ScriptTests(unittest.TestCase):
    def _run(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / f"scripts/{script}"), *args],
                              cwd=ROOT, text=True, capture_output=True)

    def test_record_span_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "spans.jsonl"
            result = self._run(
                "record-span", "--trace-id", str(uuid.uuid4()), "--stage", "inference",
                "--duration-ms", "18100", "--model", "qwen3.6:27b-coding", "--provider", "ollama",
                "--role", "student", "--attempt", "1", "--input-tokens", "48000",
                "--output-tokens", "6200", "--context-utilization", "0.98", "--output", str(output),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            records = learning.load_jsonl(output)
            self.assertEqual(1, len(records))
            telemetry.validate_span_event(records[0])
            self.assertEqual("0.1.0", records[0]["limits_version"])

    def test_record_span_hashes_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "spans.jsonl"
            result = self._run(
                "record-span", "--trace-id", str(uuid.uuid4()), "--stage", "tool",
                "--duration-ms", "12", "--tool-kind", "read", "--tool-name", "read_file",
                "--tool-outcome", "succeeded", "--target", "src/app.ts", "--output", str(output),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            records = learning.load_jsonl(output)
            self.assertTrue(records[0]["target_hash"].startswith("sha256:"))
            self.assertNotIn("src/app.ts", json.dumps(records[0]))

    def test_record_span_rejects_negative_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "spans.jsonl"
            result = self._run(
                "record-span", "--trace-id", str(uuid.uuid4()), "--stage", "tool",
                "--duration-ms", "-1", "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())

    def test_record_span_rejects_raw_target_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "spans.jsonl"
            result = self._run(
                "record-span", "--trace-id", str(uuid.uuid4()), "--stage", "tool",
                "--duration-ms", "5", "--target-hash", "src/app.ts", "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())

    def test_record_span_rejects_secret_without_echo(self) -> None:
        secret = "SuperSecret12345"
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "spans.jsonl"
            result = self._run(
                "record-span", "--trace-id", str(uuid.uuid4()), "--stage", "other",
                "--duration-ms", "5", "--note", f"password={secret}", "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_trace_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trace_id = str(uuid.uuid4())
            spans = pathlib.Path(temp) / "spans.jsonl"
            spans.write_text(json.dumps(span(trace_id=trace_id, stage="inference", duration_ms=100)) + "\n",
                             encoding="utf-8")
            result = self._run("trace-report", "--spans", str(spans), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(1, report["trace_count"])
            self.assertEqual(trace_id, report["traces"][0]["trace_id"])

    def test_record_escalation_trace_defaults_to_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "runs.jsonl"
            run_id = str(uuid.uuid4())
            result = self._run(
                "record-escalation", "--run-id", run_id, "--task-class", "bounded",
                "--student-model", "qwen3.6:27b-coding", "--student-outcome", "failed",
                "--failure-category", "test_failure", "--output", str(output),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            record = learning.load_jsonl(output)[0]
            self.assertEqual("1.1.0", record["schema_version"])
            self.assertEqual(run_id, record["trace_id"])

    def test_record_escalation_custom_trace_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "runs.jsonl"
            trace_id = str(uuid.uuid4())
            result = self._run(
                "record-escalation", "--run-id", str(uuid.uuid4()), "--trace-id", trace_id,
                "--task-class", "bounded", "--student-model", "qwen3.6:27b-coding",
                "--student-outcome", "failed", "--failure-category", "test_failure",
                "--output", str(output),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(trace_id, learning.load_jsonl(output)[0]["trace_id"])


class ConfigSyntaxTests(unittest.TestCase):
    def test_new_schema_and_config_parse(self) -> None:
        for relative in ("telemetry/schemas/span-event.schema.json", "config/limits.yaml"):
            self.assertIsInstance(infra.load_json(ROOT / relative), dict)


if __name__ == "__main__":
    unittest.main()
