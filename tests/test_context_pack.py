from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import time
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import context_pack  # noqa: E402
import escalation  # noqa: E402
import infra  # noqa: E402
import learning  # noqa: E402
import providers  # noqa: E402
import runtime  # noqa: E402
import telemetry  # noqa: E402

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


class ReasoningFailureProvider(providers.ModelProvider):
    name = "ollama"
    model = "qwen3.6:27b-coding"

    def execute(self, request):
        raise providers.ProviderError("reasoning_gap", category="model_reasoning_failure", retryable=False)

    def health_check(self):
        return providers.ProviderHealth(provider=self.name, status="healthy", model=self.model)


def make_limits() -> dict:
    return json.loads(json.dumps(telemetry.DEFAULT_LIMITS))


def failed_result(category: str, model: str = "qwen3.6:27b-coding", provider: str = "ollama",
                  code: str = "reasoning_gap") -> runtime.RuntimeResult:
    return runtime.RuntimeResult(
        run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()), span_id=str(uuid.uuid4()),
        status="failed", provider=provider, model=model, content=None, duration_ms=1.0,
        attempts=1, error_category=category, error_code=code, retryable=False,
    )


def lesson_candidate(**overrides: object) -> dict:
    record = {
        "proposed_destination": "lesson",
        "promotion_status": "validated",
        "task_class": "bounded",
        "failure_category": "test_failure",
        "reviewed_root_cause": "student skipped the existing helper",
        "validated_correction": "search for an existing helper before writing one",
    }
    record.update(overrides)
    return record


class ContextPackBuildTests(unittest.TestCase):
    def test_minimal_pack(self) -> None:
        pack = context_pack.build_context_pack("fix the bug")
        self.assertEqual(context_pack.CONTEXT_PACK_VERSION, pack.context_pack_version)
        self.assertEqual("fix the bug", pack.goal)
        self.assertEqual([], pack.relevant_files)
        self.assertIsNone(pack.failure_category)

    def test_goal_is_required(self) -> None:
        with self.assertRaises(infra.InfraError):
            context_pack.build_context_pack("   ")

    def test_optional_fields_may_be_missing(self) -> None:
        record = context_pack.build_context_pack("fix the bug").as_dict()
        self.assertIsNotNone(context_pack.validate_context_pack(record))
        self.assertTrue(context_pack.render_context_pack(record))

    def test_relevant_files_store_names_not_contents(self) -> None:
        content = "def leaked():\n    return 'full source body' * 40"
        pack = context_pack.build_context_pack("fix the bug", relevant_files=["src/app.py", content])
        self.assertIn("src/app.py", pack.relevant_files)
        stored = [item for item in pack.relevant_files if item != "src/app.py"][0]
        self.assertLessEqual(len(stored), context_pack.MAX_ITEM_CHARS)
        self.assertNotEqual(content, stored)

    def test_failure_and_test_summaries(self) -> None:
        pack = context_pack.build_context_pack(
            "fix the bug",
            failure_category="test_failure",
            error_summary="assertEqual failed in test_stream",
            failed_tests=["test_stream_disabled", "test_stream_options"],
        )
        self.assertEqual("test_failure", pack.failure_category)
        self.assertEqual("assertEqual failed in test_stream", pack.error_summary)
        self.assertEqual(["test_stream_disabled", "test_stream_options"], pack.failed_tests)

    def test_unknown_failure_category_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            context_pack.build_context_pack("fix", failure_category="vibes")

    def test_learning_references_stored(self) -> None:
        pack = context_pack.build_context_pack("fix", lessons=["search for an existing helper first"])
        self.assertIn("search for an existing helper first", pack.lessons)

    def test_no_prompt_history_or_chain_of_thought_fields(self) -> None:
        keys = set(context_pack.build_context_pack("fix").as_dict())
        self.assertEqual(set(context_pack.CONTEXT_PACK_FIELDS), keys)
        for forbidden in ("prompt", "full_prompt", "conversation", "history", "chain_of_thought", "response"):
            self.assertNotIn(forbidden, keys)

    def test_unknown_field_rejected(self) -> None:
        record = context_pack.build_context_pack("fix").as_dict()
        record["chain_of_thought"] = "model reasoned that..."
        with self.assertRaises(infra.InfraError):
            context_pack.validate_context_pack(record)

    def test_goal_truncated_to_bound(self) -> None:
        pack = context_pack.build_context_pack("x" * 5000)
        self.assertLessEqual(len(pack.goal), context_pack.MAX_GOAL_CHARS)

    def test_version_mismatch_rejected(self) -> None:
        record = context_pack.build_context_pack("fix").as_dict()
        record["context_pack_version"] = "9.9.9"
        with self.assertRaises(infra.InfraError):
            context_pack.validate_context_pack(record)

    def test_validate_rejects_non_string_list_item(self) -> None:
        record = context_pack.build_context_pack("fix").as_dict()
        record["relevant_files"] = ["ok", 3]
        with self.assertRaises(infra.InfraError):
            context_pack.validate_context_pack(record)


class ContextPackSecretTests(unittest.TestCase):
    def test_secret_in_goal_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            context_pack.build_context_pack("deploy with api_key=SuperSecret12345")

    def test_secret_in_list_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            context_pack.build_context_pack("fix", observations=["token: abcdefghijklmnop"])

    def test_secret_rejected_by_validator(self) -> None:
        record = context_pack.build_context_pack("fix").as_dict()
        record["observations"] = ["sk-" + "a" * 24]
        with self.assertRaises(infra.InfraError):
            context_pack.validate_context_pack(record)


class ContextPackRenderingTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        pack = context_pack.build_context_pack(
            "fix the bug", task_type="bounded", relevant_files=["src/app.py"],
            constraints=["do not change public API"], failure_category="test_failure",
            error_summary="assert failed", failed_tests=["test_stream"],
            lessons=["search for an existing helper"], recommended_next_action="inspect the helper",
        )
        self.assertEqual(context_pack.render_context_pack(pack), context_pack.render_context_pack(pack))

    def test_stable_before_volatile(self) -> None:
        text = context_pack.render_context_pack(
            context_pack.build_context_pack(
                "fix", constraints=["keep it small"], lessons=["reuse helpers"],
                relevant_files=["src/app.py"], current_state="half done",
                approaches_tried=["tried stream=false"], failure_category="test_failure",
                error_summary="assert failed", recommended_next_action="retry",
            )
        )
        stable = max(text.index("## Constraints"), text.index("## Relevant lessons"),
                     text.index("## Relevant files"), text.index("## Current state"))
        volatile = min(text.index("## Approaches tried"), text.index("## Failure"),
                       text.index("## Recommended next action"))
        self.assertLess(stable, volatile)

    def test_no_ids_or_timestamps(self) -> None:
        text = context_pack.render_context_pack(
            context_pack.build_context_pack("fix", source_provider="ollama", source_model="qwen"))
        self.assertIsNone(UUID_RE.search(text))
        self.assertIsNone(TIMESTAMP_RE.search(text))
        self.assertTrue(text.startswith("context_pack_version: 1.0.0"))
        self.assertIn("## Goal", text)


class ContextPackGrowthTests(unittest.TestCase):
    def test_duplicate_attempts_are_deduplicated(self) -> None:
        pack = context_pack.build_context_pack("fix")
        context_pack.record_attempt(pack, approach="tried stream=false")
        context_pack.record_attempt(pack, approach="tried stream=false")
        self.assertEqual(["tried stream=false"], pack.approaches_tried)

    def test_growth_is_bounded(self) -> None:
        limits = {"context_pack": {"max_items": 2}}
        pack = context_pack.build_context_pack("fix", limits=limits)
        for index in range(5):
            context_pack.record_attempt(pack, approach="approach-%d" % index, limits=limits)
        self.assertEqual(["approach-3", "approach-4"], pack.approaches_tried)

    def test_update_after_another_failure(self) -> None:
        pack = context_pack.build_context_pack("fix", source_provider="deepseek",
                                               source_model="deepseek-flash", source_variant="low")
        context_pack.record_attempt(pack, failure_category="test_failure", error_summary="first failure",
                                    recommended_next_action="disable stream")
        context_pack.record_attempt(pack, failure_category="invalid_structured_output",
                                    error_summary="second failure", recommended_next_action="fix schema",
                                    source_variant="high")
        self.assertEqual("invalid_structured_output", pack.failure_category)
        self.assertEqual("second failure", pack.error_summary)
        self.assertEqual("fix schema", pack.recommended_next_action)
        self.assertEqual("high", pack.source_variant)
        rendered = context_pack.render_context_pack(pack)
        self.assertIn("second failure", rendered)
        self.assertNotIn("first failure", rendered)

    def test_low_to_high_accumulates_attempts(self) -> None:
        pack = context_pack.build_context_pack("fix the bug")
        context_pack.record_attempt(pack, approach="deepseek low: tried stream=false",
                                    source_variant="low")
        context_pack.record_attempt(pack, approach="deepseek high: inspect stream_options batch",
                                    source_variant="high")
        self.assertEqual(["deepseek low: tried stream=false",
                          "deepseek high: inspect stream_options batch"], pack.approaches_tried)
        self.assertEqual("high", pack.source_variant)


class SelectLessonsTests(unittest.TestCase):
    def test_selects_only_reviewed_lesson_candidates(self) -> None:
        candidates = [
            lesson_candidate(),
            lesson_candidate(promotion_status="observation"),
            lesson_candidate(proposed_destination="eval"),
            lesson_candidate(failure_category="wrong_api_usage",
                             validated_correction="unrelated correction"),
        ]
        selected = context_pack.select_lessons(candidates, failure_category="test_failure")
        self.assertEqual(["search for an existing helper before writing one"], selected)

    def test_respects_limit_and_deduplicates(self) -> None:
        candidates = [
            lesson_candidate(validated_correction="same"),
            lesson_candidate(validated_correction="same"),
            lesson_candidate(validated_correction="other"),
        ]
        selected = context_pack.select_lessons(candidates, limit=1)
        self.assertEqual(["same"], selected)


class EscalationContextPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.spans = base / "spans.jsonl"
        self.escalations = base / "escalations.jsonl"
        self.limits = make_limits()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def policy(self, **overrides: object) -> escalation.EscalationPolicy:
        values = dict(enabled=True, mode="automatic", hosted_allowed=True,
                      fallback_provider="deepseek", max_depth=1)
        values.update(overrides)
        return escalation.EscalationPolicy(**values)

    def controller(self, fallback: providers.ModelProvider) -> escalation.EscalationController:
        return escalation.EscalationController(
            self.policy(), fallback, limits=self.limits, span_output=self.spans,
            escalation_output=self.escalations)

    def run_student(self, prompt: str = "fix the stream=false bug", **fields: object) -> tuple:
        agent = runtime.AgentRuntime(ReasoningFailureProvider(), limits=self.limits, span_output=self.spans)
        task = runtime.AgentTask(prompt=prompt, **fields)
        session = agent.open_session(task)
        return session, task, agent.run(task, session=session)

    def test_low_to_high_handoff(self) -> None:
        fallback = providers.FakeModelProvider(content="FIXED")
        session, task, student = self.run_student(task_type="bounded")
        outcome = self.controller(fallback).maybe_escalate(
            session, task, student, task_class="bounded", source_variant="low")

        self.assertTrue(outcome.escalated)
        pack = outcome.context_pack
        self.assertEqual("low", pack.source_variant)
        self.assertEqual("ollama", pack.source_provider)
        self.assertEqual("unknown", pack.failure_category)

        row = learning.load_jsonl(self.escalations)[0]
        learning.validate_escalation_record(row)
        self.assertEqual("1.2.0", row["schema_version"])
        self.assertEqual(pack.as_dict(), row["context_pack"])

        teacher_prompt = fallback.calls[0].prompt
        self.assertIn("fix the stream=false bug", teacher_prompt)
        self.assertIn("## Goal", teacher_prompt)
        self.assertIn("## Failure", teacher_prompt)
        self.assertEqual(context_pack.render_context_pack(pack), context_pack.render_context_pack(pack))

    def test_pack_accumulates_across_hops_without_restarting(self) -> None:
        seeded = context_pack.build_context_pack(
            "fix the stream=false bug",
            approaches_tried=["deepseek low: tried stream=false"],
            lessons=["search for an existing helper before writing one"],
        )
        fallback = providers.FakeModelProvider(content="FIXED")
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=time.perf_counter(), budget=100.0)
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt="fix the stream=false bug"),
            failed_result("validation_failure", code="bad_schema"),
            context_pack=seeded, source_variant="high")

        pack = outcome.context_pack
        self.assertEqual(["deepseek low: tried stream=false"], pack.approaches_tried)
        self.assertEqual(["search for an existing helper before writing one"], pack.lessons)
        self.assertEqual("invalid_structured_output", pack.failure_category)
        self.assertEqual("high", pack.source_variant)
        self.assertIn("deepseek low: tried stream=false", fallback.calls[0].prompt)

    def test_dirty_secret_in_pack_blocks_before_hosted_call(self) -> None:
        pack = context_pack.build_context_pack("fix")
        pack.lessons.append("api_key=SuperSecret12345")
        fallback = providers.FakeModelProvider()
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=time.perf_counter(), budget=100.0)
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt="fix"), failed_result("model_reasoning_failure"),
            context_pack=pack)
        self.assertEqual("blocked", outcome.action)
        self.assertEqual("hosted_secret_detected", outcome.reason)
        self.assertEqual(0, len(fallback.calls))
        self.assertFalse(self.escalations.exists())

    def test_secret_variant_blocks_before_hosted_call(self) -> None:
        fallback = providers.FakeModelProvider()
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=time.perf_counter(), budget=100.0)
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt="fix"), failed_result("model_reasoning_failure"),
            source_variant="api_key=SuperSecret12345")
        self.assertEqual("blocked", outcome.action)
        self.assertEqual(0, len(fallback.calls))

    def test_non_escalation_has_no_pack(self) -> None:
        fallback = providers.FakeModelProvider()
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=time.perf_counter(), budget=100.0)
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt="fix"), failed_result("environment_failure"))
        self.assertIsNone(outcome.context_pack)

    def test_invalid_pack_in_record_rejected(self) -> None:
        record = {
            "schema_version": "1.2.0",
            "run_id": str(uuid.uuid4()),
            "event_id": str(uuid.uuid4()),
            "timestamp_utc": infra.utc_now(),
            "trace_id": str(uuid.uuid4()),
            "task_class": "bounded",
            "student_model": "qwen",
            "student_outcome": "failed",
            "failure_category": "test_failure",
            "context_pack": {"context_pack_version": "1.0.0"},
        }
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(record)


class EscalationPackFromLessonsTests(unittest.TestCase):
    def test_lessons_selected_into_pack(self) -> None:
        candidates = [lesson_candidate(failure_category="unknown")]
        lessons = context_pack.select_lessons(candidates, failure_category="unknown")
        pack = context_pack.build_context_pack("fix", lessons=lessons)
        self.assertIn("search for an existing helper before writing one", pack.lessons)


if __name__ == "__main__":
    unittest.main()
