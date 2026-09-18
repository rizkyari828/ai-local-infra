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
import escalation  # noqa: E402
import infra  # noqa: E402
import learning  # noqa: E402
import providers  # noqa: E402
import runtime  # noqa: E402
import telemetry  # noqa: E402

PRICING = {
    "schema_version": 1,
    "pricing_version": "0.1.0",
    "currency": "USD",
    "providers": {
        "deepseek": {
            "models": {
                "deepseek-flash": {
                    "profile": "deepseek-flash",
                    "normal_input_price_per_million": 1.0,
                    "cached_input_price_per_million": 0.1,
                    "output_price_per_million": 2.0,
                }
            }
        }
    },
}


class FakeEscalation:
    def __init__(self, escalated: bool, teacher_result=None, trace_id=None, context_pack=None) -> None:
        self.escalated = escalated
        self.teacher_result = teacher_result
        self.trace_id = trace_id
        self.context_pack = context_pack


class ReasoningFailureProvider(providers.ModelProvider):
    name = "student-fake"
    model = "qwen3.6:27b-coding"

    def execute(self, request):
        raise providers.ProviderError("reasoning_gap", category="model_reasoning_failure", retryable=False)

    def health_check(self):
        return providers.ProviderHealth(provider=self.name, status="healthy", model=self.model)


def result(status: str, provider: str, model: str, **fields: object) -> runtime.RuntimeResult:
    return runtime.RuntimeResult(
        run_id=fields.get("run_id") or str(uuid.uuid4()),
        trace_id=fields.get("trace_id") or str(uuid.uuid4()),
        span_id=None,
        status=status,
        provider=provider,
        model=model,
        content=None,
        duration_ms=fields.get("duration_ms", 1000.0),
        attempts=fields.get("attempts", 1),
        error_category=fields.get("error_category"),
        error_code=fields.get("error_code"),
        retryable=False,
        input_tokens=fields.get("input_tokens"),
        output_tokens=fields.get("output_tokens"),
        cached_input_tokens=fields.get("cached_input_tokens"),
        cache_miss_tokens=fields.get("cache_miss_tokens"),
    )


def outcome(**overrides: object) -> dict:
    record = {
        "schema_version": telemetry.TASK_OUTCOME_SCHEMA_VERSION,
        "task_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "completed_at": "2026-09-18T10:00:05Z",
        "started_at": "2026-09-18T10:00:00Z",
        "run_id": str(uuid.uuid4()),
        "task_type": "bounded",
        "status": "succeeded",
        "success": True,
        "tests_passed": None,
        "escalated": False,
        "escalation_count": 0,
        "attempt_count": 1,
        "initial_provider": "ollama",
        "initial_model": "qwen3.6:27b-coding",
        "initial_variant": None,
        "final_provider": "ollama",
        "final_model": "qwen3.6:27b-coding",
        "final_variant": None,
        "total_latency_ms": 1000.0,
        "provider_reported_cost": None,
        "estimated_total_cost": None,
        "pricing_currency": None,
        "error_category": None,
        "error_code": None,
    }
    record.update(overrides)
    return record


class TaskOutcomeValidationTests(unittest.TestCase):
    def test_example_passes(self) -> None:
        record = infra.load_json(ROOT / "telemetry/schemas/example.task-outcome.json")
        self.assertIsNotNone(telemetry.validate_task_outcome(record))

    def test_schema_matches_fields(self) -> None:
        schema = infra.load_json(ROOT / "telemetry/schemas/task-outcome.schema.json")
        self.assertEqual(set(schema["properties"]), set(telemetry.TASK_OUTCOME_FIELDS))
        self.assertEqual(set(schema["required"]), set(telemetry.TASK_OUTCOME_REQUIRED))

    def test_missing_required_rejected(self) -> None:
        record = outcome()
        record.pop("completed_at")
        with self.assertRaises(infra.InfraError):
            telemetry.validate_task_outcome(record)

    def test_bad_status_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_task_outcome(outcome(status="finished"))

    def test_bad_error_category_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_task_outcome(outcome(success=False, error_category="made_up"))

    def test_negative_cost_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_task_outcome(outcome(estimated_total_cost=-0.01))

    def test_non_boolean_success_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_task_outcome(outcome(success="yes"))

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_task_outcome(outcome(prompt="do not store me"))

    def test_secret_like_value_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_task_outcome(outcome(error_code="api_key=abcdefgh12345"))

    def test_escalation_count_requires_escalated(self) -> None:
        with self.assertRaises(infra.InfraError):
            telemetry.validate_task_outcome(outcome(escalated=False, escalation_count=1))


class TaskOutcomeBuildTests(unittest.TestCase):
    def test_successful_task_without_escalation(self) -> None:
        trace_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        student = result("succeeded", "ollama", "qwen3.6:27b-coding", trace_id=trace_id, run_id=run_id,
                         duration_ms=1234.0, attempts=1)
        record = telemetry.build_task_outcome(student, task_type="bounded", tests_passed=True,
                                              completed_at="2026-09-18T10:00:05Z", pricing_config=PRICING)
        self.assertEqual(trace_id, record["task_id"])
        self.assertEqual(run_id, record["run_id"])
        self.assertTrue(record["success"])
        self.assertFalse(record["escalated"])
        self.assertEqual(0, record["escalation_count"])
        self.assertEqual(1, record["attempt_count"])
        self.assertEqual("ollama", record["initial_provider"])
        self.assertEqual("ollama", record["final_provider"])
        self.assertIsNone(record["initial_variant"])
        self.assertEqual(1234.0, record["total_latency_ms"])
        self.assertTrue(record["tests_passed"])
        self.assertIsNone(record["error_category"])

    def test_failed_task(self) -> None:
        student = result("failed", "ollama", "qwen", duration_ms=500.0,
                         error_category="model_reasoning_failure", error_code="reasoning_gap")
        record = telemetry.build_task_outcome(student, completed_at="2026-09-18T10:00:05Z")
        self.assertFalse(record["success"])
        self.assertEqual("failed", record["status"])
        self.assertEqual("model_reasoning_failure", record["error_category"])
        self.assertEqual("reasoning_gap", record["error_code"])

    def test_unknown_tests_passed_is_null(self) -> None:
        student = result("succeeded", "ollama", "qwen")
        record = telemetry.build_task_outcome(student, completed_at="2026-09-18T10:00:05Z")
        self.assertIsNone(record["tests_passed"])

    def test_unknown_cost_stays_null(self) -> None:
        student = result("succeeded", "ollama", "qwen", input_tokens=1000, output_tokens=500)
        record = telemetry.build_task_outcome(student, completed_at="2026-09-18T10:00:05Z")
        self.assertIsNone(record["estimated_total_cost"])
        self.assertIsNone(record["provider_reported_cost"])

    def test_cost_aggregation_across_escalation(self) -> None:
        student = result("failed", "deepseek", "deepseek-flash", duration_ms=2000.0, attempts=1,
                         error_category="model_reasoning_failure", error_code="reasoning_gap",
                         input_tokens=1000, cache_miss_tokens=1000, cached_input_tokens=0, output_tokens=500)
        teacher = result("succeeded", "deepseek", "deepseek-flash", trace_id=student.trace_id,
                         duration_ms=3000.0, attempts=2,
                         input_tokens=2000, cache_miss_tokens=2000, cached_input_tokens=0, output_tokens=1000)
        record = telemetry.build_task_outcome(
            student, escalation=FakeEscalation(True, teacher), completed_at="2026-09-18T10:00:05Z",
            pricing_config=PRICING)
        # student: 1000/1e6*1 + 500/1e6*2 = 0.002; teacher: 2000/1e6*1 + 1000/1e6*2 = 0.004
        self.assertAlmostEqual(0.006, record["estimated_total_cost"], places=8)
        self.assertEqual("USD", record["pricing_currency"])
        self.assertTrue(record["success"])
        self.assertTrue(record["escalated"])
        self.assertEqual(1, record["escalation_count"])
        self.assertEqual(3, record["attempt_count"])
        self.assertEqual(5000.0, record["total_latency_ms"])
        self.assertEqual("deepseek", record["initial_provider"])
        self.assertEqual("deepseek", record["final_provider"])

    def test_partial_cost_is_not_invented(self) -> None:
        student = result("failed", "deepseek", "deepseek-flash", input_tokens=1000,
                         cache_miss_tokens=1000, cached_input_tokens=0, output_tokens=500)
        teacher = result("succeeded", "deepseek", "deepseek-flash", trace_id=student.trace_id,
                         input_tokens=1000, output_tokens=500)  # cache miss unknown -> cost unknown
        record = telemetry.build_task_outcome(
            student, escalation=FakeEscalation(True, teacher), completed_at="2026-09-18T10:00:05Z",
            pricing_config=PRICING)
        self.assertIsNone(record["estimated_total_cost"])

    def test_blocked_escalation_keeps_student_as_final(self) -> None:
        student = result("failed", "ollama", "qwen", error_category="model_reasoning_failure")
        record = telemetry.build_task_outcome(
            student, escalation=FakeEscalation(False), completed_at="2026-09-18T10:00:05Z")
        self.assertFalse(record["escalated"])
        self.assertEqual("ollama", record["final_provider"])
        self.assertEqual(student.run_id, record["run_id"])

    def test_initial_variant_from_context_pack(self) -> None:
        student = result("failed", "ollama", "qwen")
        pack = type("Pack", (), {"source_variant": "low"})()
        record = telemetry.build_task_outcome(
            student, escalation=FakeEscalation(False, context_pack=pack), completed_at="2026-09-18T10:00:05Z")
        self.assertEqual("low", record["initial_variant"])

    def test_no_prompt_or_response_fields(self) -> None:
        record = telemetry.build_task_outcome(result("succeeded", "ollama", "qwen"),
                                              completed_at="2026-09-18T10:00:05Z")
        self.assertEqual(set(telemetry.TASK_OUTCOME_FIELDS), set(record))
        for forbidden in ("prompt", "response", "content", "source", "chain_of_thought"):
            self.assertNotIn(forbidden, record)

    def test_task_id_defaults_to_trace_id(self) -> None:
        trace_id = str(uuid.uuid4())
        record = telemetry.build_task_outcome(result("succeeded", "ollama", "qwen", trace_id=trace_id),
                                              completed_at="2026-09-18T10:00:05Z")
        self.assertEqual(trace_id, record["task_id"])


class TaskOutcomeDedupeTests(unittest.TestCase):
    def test_dedupe_keeps_latest(self) -> None:
        task_id = str(uuid.uuid4())
        first = outcome(task_id=task_id, success=False, status="failed")
        second = outcome(task_id=task_id, success=True, status="succeeded")
        unique = telemetry.dedupe_task_outcomes([first, second])
        self.assertEqual(1, len(unique))
        self.assertTrue(unique[0]["success"])

    def test_summarize_does_not_double_count(self) -> None:
        task_id = str(uuid.uuid4())
        metrics = telemetry.summarize_task_outcomes([
            outcome(task_id=task_id, success=False, status="failed"),
            outcome(task_id=task_id, success=True, status="succeeded"),
        ])
        self.assertEqual(1, metrics["task_count"])
        self.assertEqual(2, metrics["record_count"])
        self.assertEqual(1, metrics["duplicate_count"])
        self.assertEqual(1.0, metrics["success_rate"])


class TaskOutcomeMetricsTests(unittest.TestCase):
    def test_rates_averages_and_breakdown(self) -> None:
        records = [
            outcome(task_id=str(uuid.uuid4()), success=True, escalated=False, attempt_count=1,
                    total_latency_ms=1000.0, estimated_total_cost=0.010, task_type="bounded",
                    final_provider="ollama", final_model="qwen", final_variant=None),
            outcome(task_id=str(uuid.uuid4()), success=True, escalated=True, escalation_count=1,
                    attempt_count=3, total_latency_ms=5000.0, estimated_total_cost=0.030,
                    task_type="bounded", final_provider="deepseek", final_model="deepseek-flash",
                    final_variant="high"),
            outcome(task_id=str(uuid.uuid4()), success=False, escalated=False, attempt_count=2,
                    total_latency_ms=2000.0, estimated_total_cost=None, task_type="other",
                    final_provider="ollama", final_model="qwen", final_variant=None),
        ]
        metrics = telemetry.summarize_task_outcomes(records)
        self.assertEqual(3, metrics["task_count"])
        self.assertEqual(2, metrics["succeeded"])
        self.assertAlmostEqual(2 / 3, metrics["success_rate"], places=6)
        self.assertAlmostEqual(1 / 3, metrics["escalation_rate"], places=6)
        self.assertAlmostEqual((1 + 3 + 2) / 3, metrics["avg_attempts"], places=3)
        self.assertAlmostEqual((1000 + 5000 + 2000) / 3, metrics["avg_latency_ms"], places=3)
        self.assertAlmostEqual((0.010 + 0.030) / 2, metrics["avg_cost_per_task"], places=6)
        self.assertAlmostEqual((0.010 + 0.030) / 2, metrics["avg_cost_per_successful_task"], places=6)
        self.assertEqual(2, metrics["tasks_with_known_cost"])
        self.assertEqual({"bounded", "other"}, {row["task_type"] for row in metrics["by_task_type"]})

    def test_average_cost_per_successful_task_excludes_failures(self) -> None:
        records = [
            outcome(task_id=str(uuid.uuid4()), success=True, estimated_total_cost=0.020),
            outcome(task_id=str(uuid.uuid4()), success=False, status="failed",
                    error_category="unknown", estimated_total_cost=0.040),
        ]
        metrics = telemetry.summarize_task_outcomes(records)
        self.assertAlmostEqual(0.030, metrics["avg_cost_per_task"], places=6)
        self.assertAlmostEqual(0.020, metrics["avg_cost_per_successful_task"], places=6)

    def test_empty_input(self) -> None:
        metrics = telemetry.summarize_task_outcomes([])
        self.assertEqual(0, metrics["task_count"])
        self.assertIsNone(metrics["success_rate"])
        self.assertIsNone(metrics["avg_cost_per_task"])

    def test_unknown_success_is_not_counted_as_failure(self) -> None:
        records = [
            outcome(task_id=str(uuid.uuid4()), success=None, status="succeeded"),
            outcome(task_id=str(uuid.uuid4()), success=True),
        ]
        metrics = telemetry.summarize_task_outcomes(records)
        self.assertEqual(2, metrics["task_count"])
        self.assertEqual(1, metrics["succeeded"])
        self.assertEqual(1, metrics["tasks_with_known_success"])
        self.assertEqual(1.0, metrics["success_rate"])

    def test_all_unknown_success_rate_is_null(self) -> None:
        metrics = telemetry.summarize_task_outcomes(
            [outcome(task_id=str(uuid.uuid4()), success=None, status="unknown")])
        self.assertEqual(0, metrics["tasks_with_known_success"])
        self.assertIsNone(metrics["success_rate"])

    def test_breakdown_by_final_provider_model_variant(self) -> None:
        records = [
            outcome(task_id=str(uuid.uuid4()), final_provider="deepseek", final_model="deepseek-flash",
                    final_variant="high"),
            outcome(task_id=str(uuid.uuid4()), final_provider="deepseek", final_model="deepseek-flash",
                    final_variant="low"),
        ]
        metrics = telemetry.summarize_task_outcomes(records)
        variants = {row["variant"] for row in metrics["by_final_provider_model_variant"]}
        self.assertEqual({"high", "low"}, variants)


class TaskOutcomeEscalationIntegrationTests(unittest.TestCase):
    def test_low_to_high_lifecycle(self) -> None:
        limits = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            fallback = providers.FakeModelProvider(model="deepseek-flash", content="FIXED")
            policy = escalation.EscalationPolicy(enabled=True, mode="automatic", hosted_allowed=True,
                                                 fallback_provider="deepseek", max_depth=1)
            controller = escalation.EscalationController(
                policy, fallback, limits=limits, span_output=base / "spans.jsonl",
                escalation_output=base / "escalations.jsonl")
            agent = runtime.AgentRuntime(ReasoningFailureProvider(), limits=limits,
                                         span_output=base / "spans.jsonl")
            task = runtime.AgentTask(prompt="fix the stream=false bug", task_type="bounded")
            session = agent.open_session(task)
            student = agent.run(task, session=session)
            outcome = controller.maybe_escalate(session, task, student, task_class="bounded",
                                                source_variant="low")

            record = telemetry.build_task_outcome(
                student, escalation=outcome, task_type="bounded", started_at="2026-09-18T10:00:00Z",
                completed_at="2026-09-18T10:00:05Z", initial_variant="low", final_variant="high",
                pricing_config=PRICING)
        self.assertTrue(record["success"])
        self.assertTrue(record["escalated"])
        self.assertEqual(1, record["escalation_count"])
        self.assertEqual("student-fake", record["initial_provider"])
        self.assertEqual("qwen3.6:27b-coding", record["initial_model"])
        self.assertEqual("low", record["initial_variant"])
        self.assertEqual("fake", record["final_provider"])
        self.assertEqual("deepseek-flash", record["final_model"])
        self.assertEqual("high", record["final_variant"])
        self.assertEqual(2, record["attempt_count"])
        self.assertGreater(record["total_latency_ms"], 0)


class TaskOutcomeCliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "scripts/agent-run"), *args],
                              cwd=ROOT, text=True, capture_output=True)

    def test_outcome_written_once_per_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            trace_id = str(uuid.uuid4())
            result = self._run("--provider", "fake", "--prompt", "hello", "--confirm-run",
                               "--trace-id", trace_id, "--task-type", "bounded", "--json",
                               "--span-output", str(base / "spans.jsonl"),
                               "--outcome-output", str(base / "outcomes.jsonl"))
            self.assertEqual(0, result.returncode, result.stderr)
            rows = learning.load_jsonl(base / "outcomes.jsonl")
            self.assertEqual(1, len(rows))
            self.assertEqual(trace_id, rows[0]["task_id"])
            self.assertTrue(rows[0]["success"])
            self.assertEqual("bounded", rows[0]["task_type"])

    def test_outcome_never_stores_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            secret = "deploy with api_key=SuperSecret12345"
            result = self._run("--provider", "fake", "--prompt", secret, "--confirm-run",
                               "--json", "--span-output", str(base / "spans.jsonl"),
                               "--outcome-output", str(base / "outcomes.jsonl"))
            self.assertEqual(0, result.returncode, result.stderr)
            raw = (base / "outcomes.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("SuperSecret", raw)
            self.assertNotIn("api_key", raw)
            telemetry.validate_task_outcome(learning.load_jsonl(base / "outcomes.jsonl")[0])


if __name__ == "__main__":
    unittest.main()
