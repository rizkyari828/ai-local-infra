from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import infra  # noqa: E402
import learning  # noqa: E402


def escalation(**overrides: object) -> dict:
    record = {
        "schema_version": "1.0.0",
        "run_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "timestamp_utc": "2026-09-17T10:00:00Z",
        "parent_run_id": None,
        "task_class": "bounded-code-transformation",
        "student_role": "student",
        "student_model": "qwen3.6:27b-coding",
        "student_profile": "coding-routine",
        "student_outcome": "failed",
        "failure_category": "missed_existing_pattern",
        "teacher_provider": "deepseek",
        "teacher_model": "deepseek-coder",
        "teacher_reason": "failure",
        "teacher_outcome": "accepted",
        "human_review_outcome": "accepted",
        "correction_required": True,
        "human_correction_count": 1,
        "tests_passed": True,
        "lesson_candidate": True,
        "eval_candidate": True,
        "training_candidate": False,
        "duration_ms": 8400,
        "cost_input_tokens": 1800,
        "cost_output_tokens": 420,
        "cost_cache_hit_tokens": None,
        "estimated_cost_usd": None,
        "pricing_source": None,
        "recorded_by": "tester",
        "sanitized_note": None,
    }
    record.update(overrides)
    return record


def local_success(**overrides: object) -> dict:
    base = {
        "student_outcome": "succeeded",
        "failure_category": None,
        "teacher_provider": None,
        "teacher_model": None,
        "teacher_reason": None,
        "teacher_outcome": None,
        "correction_required": False,
        "human_review_outcome": "accepted",
        "human_correction_count": 0,
    }
    base.update(overrides)
    return escalation(**base)


def candidate(**overrides: object) -> dict:
    record = {
        "schema_version": "1.0.0",
        "candidate_id": str(uuid.uuid4()),
        "timestamp_utc": "2026-09-17T10:00:00Z",
        "source_run_ids": [str(uuid.uuid4())],
        "task_class": "bounded-code-transformation",
        "observed_failure": "Missed the existing helper convention.",
        "failure_category": "missed_existing_pattern",
        "reviewed_root_cause": "No pattern search before writing a helper.",
        "validated_correction": "Search for an existing helper first.",
        "validation_evidence": ["regression eval qwen-regression-example passed"],
        "teacher_provider": "deepseek",
        "teacher_model": "deepseek-coder",
        "human_accepted": True,
        "reviewer": "human-reviewer",
        "proposed_destination": "lesson",
        "promotion_status": "validated",
        "project_consent": False,
        "sanitized_note": None,
    }
    record.update(overrides)
    return record


class TaxonomyTests(unittest.TestCase):
    def test_taxonomy_file_matches_code(self) -> None:
        data = infra.load_json(ROOT / "telemetry/taxonomy/failure-categories.yaml")
        ids = tuple(item["id"] for item in data["categories"])
        self.assertEqual(ids, learning.FAILURE_CATEGORIES)


class EscalationValidationTests(unittest.TestCase):
    def test_valid_teacher_escalation_accepted(self) -> None:
        self.assertIsNotNone(learning.validate_escalation_record(escalation()))

    def test_valid_local_only_success_accepted(self) -> None:
        self.assertIsNotNone(learning.validate_escalation_record(local_success()))

    def test_unknown_category_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(failure_category="made_up"))

    def test_bad_student_outcome_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(student_outcome="maybe"))

    def test_failed_without_category_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(failure_category=None))

    def test_escalation_without_reason_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(teacher_reason=None))

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(source_code="secret"))

    def test_negative_duration_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(duration_ms=-1))

    def test_negative_correction_count_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(human_correction_count=-2))

    def test_negative_token_count_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(cost_cache_hit_tokens=-3))

    def test_negative_estimated_cost_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(estimated_cost_usd=-0.01))

    def test_non_boolean_flag_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(lesson_candidate="yes"))

    def test_non_boolean_nullable_flag_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(correction_required="no"))

    def test_teacher_outcome_without_provider_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(
                escalation(teacher_provider=None, teacher_model=None, teacher_reason=None)
            )

    def test_provider_without_teacher_outcome_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(teacher_outcome=None))

    def test_partial_teacher_fields_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(
                escalation(teacher_model=None, teacher_reason=None, teacher_outcome=None)
            )

    def test_non_uuid_run_id_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(run_id="not-a-uuid"))

    def test_timestamp_without_timezone_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(timestamp_utc="2026-09-17T10:00:00"))

    def test_secret_like_note_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_escalation_record(escalation(sanitized_note="api_key=abcdefgh12345"))


class CandidateValidationTests(unittest.TestCase):
    def test_valid_candidate_passes(self) -> None:
        self.assertIsNotNone(learning.validate_learning_candidate(candidate()))

    def test_valid_promoted_candidate_accepted(self) -> None:
        self.assertIsNotNone(
            learning.validate_learning_candidate(candidate(promotion_status="promoted"))
        )

    def test_reviewed_without_root_cause_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(
                candidate(promotion_status="reviewed", reviewed_root_cause=None)
            )

    def test_validated_without_correction_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(candidate(validated_correction=None))

    def test_validated_without_evidence_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(candidate(validation_evidence=[]))

    def test_promoted_requires_human_acceptance(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(
                candidate(promotion_status="promoted", human_accepted=False)
            )

    def test_promoted_requires_reviewer(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(candidate(promotion_status="promoted", reviewer=None))

    def test_promoted_requires_evidence(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(
                candidate(promotion_status="promoted", validation_evidence=[])
            )

    def test_training_candidate_requires_consent(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(candidate(proposed_destination="training-candidate"))

    def test_training_promoted_requires_consent(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(
                candidate(promotion_status="promoted", proposed_destination="training-candidate")
            )

    def test_rejected_candidate_without_evidence_accepted(self) -> None:
        self.assertIsNotNone(
            learning.validate_learning_candidate(
                candidate(promotion_status="rejected", validation_evidence=[])
            )
        )

    def test_secret_like_free_text_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(candidate(observed_failure="api_key=abcdefgh12345"))

    def test_empty_source_runs_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(candidate(source_run_ids=[]))

    def test_bad_source_run_uuid_rejected(self) -> None:
        with self.assertRaises(infra.InfraError):
            learning.validate_learning_candidate(candidate(source_run_ids=["not-a-uuid"]))


class MetricsTests(unittest.TestCase):
    def test_rates(self) -> None:
        records = [
            local_success(),
            escalation(),
            escalation(student_outcome="failed", failure_category="test_failure",
                       teacher_provider="codex", teacher_model="codex", teacher_reason="uncertainty",
                       teacher_outcome="rejected", human_review_outcome="rejected",
                       human_correction_count=3),
        ]
        metrics = learning.compute_metrics(records)
        self.assertEqual(3, metrics["total_tasks"])
        self.assertEqual(2, metrics["escalated_tasks"])
        self.assertEqual(1, metrics["local_success_tasks"])
        self.assertEqual(1, metrics["teacher_accepted_tasks"])
        self.assertAlmostEqual(2 / 3, metrics["paid_escalation_rate"])
        self.assertAlmostEqual(1 / 3, metrics["local_success_rate"])
        self.assertAlmostEqual(1 / 2, metrics["teacher_acceptance_rate"])
        self.assertAlmostEqual(1 / 3, metrics["first_pass_local_success_rate"])
        self.assertAlmostEqual(4 / 3, metrics["mean_human_corrections_per_task"])

    def test_human_accepted_teacher_counts(self) -> None:
        metrics = learning.compute_metrics(
            [escalation(teacher_outcome="accepted", human_review_outcome="accepted")]
        )
        self.assertEqual(1, metrics["teacher_accepted_tasks"])
        self.assertEqual(1.0, metrics["teacher_acceptance_rate"])

    def test_human_rejected_teacher_not_accepted(self) -> None:
        metrics = learning.compute_metrics(
            [escalation(teacher_outcome="accepted", human_review_outcome="rejected")]
        )
        self.assertEqual(0, metrics["teacher_accepted_tasks"])
        self.assertEqual(0.0, metrics["teacher_acceptance_rate"])

    def test_teacher_failed_not_accepted(self) -> None:
        metrics = learning.compute_metrics(
            [escalation(teacher_outcome="rejected", human_review_outcome="rejected")]
        )
        self.assertEqual(0, metrics["teacher_acceptance_rate"])

    def test_unreviewed_teacher_not_accepted(self) -> None:
        metrics = learning.compute_metrics([escalation(human_review_outcome="unreviewed")])
        self.assertEqual(0, metrics["teacher_acceptance_rate"])

    def test_unreviewed_qwen_not_first_pass(self) -> None:
        metrics = learning.compute_metrics([local_success(human_review_outcome="unreviewed")])
        self.assertEqual(0, metrics["first_pass_local_success_rate"])

    def test_unknown_correction_not_first_pass(self) -> None:
        metrics = learning.compute_metrics(
            [local_success(correction_required=None, human_review_outcome="accepted")]
        )
        self.assertEqual(0, metrics["first_pass_local_success_rate"])

    def test_explicit_no_correction_accepted_counts(self) -> None:
        metrics = learning.compute_metrics([local_success()])
        self.assertEqual(1, metrics["first_pass_local_success_tasks"])
        self.assertEqual(1.0, metrics["first_pass_local_success_rate"])

    def test_escalated_success_not_first_pass(self) -> None:
        metrics = learning.compute_metrics(
            [local_success(teacher_provider="deepseek", teacher_model="deepseek-coder",
                           teacher_reason="policy", teacher_outcome="accepted")]
        )
        self.assertEqual(0, metrics["first_pass_local_success_rate"])

    def test_mean_corrections_uses_known_only(self) -> None:
        metrics = learning.compute_metrics(
            [escalation(human_correction_count=2),
             escalation(human_correction_count=None),
             escalation(human_correction_count=4)]
        )
        self.assertEqual(2, metrics["tasks_with_known_correction_count"])
        self.assertAlmostEqual(3.0, metrics["mean_human_corrections_per_task"])

    def test_mean_corrections_null_when_all_unknown(self) -> None:
        metrics = learning.compute_metrics([escalation(human_correction_count=None)])
        self.assertEqual(0, metrics["tasks_with_known_correction_count"])
        self.assertIsNone(metrics["mean_human_corrections_per_task"])

    def test_empty_denominators_are_null(self) -> None:
        metrics = learning.compute_metrics([])
        self.assertEqual(0, metrics["total_tasks"])
        for key in ("paid_escalation_rate", "local_success_rate", "teacher_acceptance_rate",
                    "first_pass_local_success_rate", "mean_human_corrections_per_task"):
            self.assertIsNone(metrics[key], key)
        partial = learning.compute_metrics([local_success()])
        self.assertIsNone(partial["teacher_acceptance_rate"])
        self.assertEqual(1.0, partial["first_pass_local_success_rate"])


class SecretGuardTests(unittest.TestCase):
    def test_representative_patterns_detected(self) -> None:
        for sample in (
            "sk-ABCDEFGHIJKLMNOPQRSTUVWX",
            "AKIAIOSFODNN7EXAMPLE",
            "-----BEGIN RSA PRIVATE KEY-----",
            "api_key=abcdefgh12345",
            "password: hunter2hunter2",
            "access_token=abcdefghij",
        ):
            self.assertTrue(learning.contains_secret_like(sample), sample)

    def test_benign_text_not_detected(self) -> None:
        for sample in ("bounded-code-transformation", "qwen3.6:27b-coding",
                       "Teacher generalized the helper; no source or prompt stored.",
                       "cost tokens per run"):
            self.assertFalse(learning.contains_secret_like(sample), sample)


class ExampleTelemetryTests(unittest.TestCase):
    def test_example_jsonl_records_validate(self) -> None:
        records = learning.load_jsonl(ROOT / "telemetry/examples/escalations.jsonl")
        self.assertGreaterEqual(len(records), 2)
        for record in records:
            learning.validate_escalation_record(record)

    def test_example_json_matches_schema_shape(self) -> None:
        record = infra.load_json(ROOT / "telemetry/schemas/example.escalation-run.json")
        learning.validate_escalation_record(record)


class RolesTests(unittest.TestCase):
    def test_roles_are_expected(self) -> None:
        data = infra.load_json(ROOT / "models/roles.yaml")
        roles = {role["id"]: role for role in data["roles"]}
        self.assertEqual({"student", "teacher-cheap", "teacher-premium"}, set(roles))
        self.assertTrue(roles["student"]["required_for_operation"])
        self.assertFalse(roles["teacher-cheap"]["required_for_operation"])
        self.assertFalse(roles["teacher-premium"]["required_for_operation"])

    def test_roles_have_no_secret_values(self) -> None:
        text = (ROOT / "models/roles.yaml").read_text(encoding="utf-8")
        pattern = re.compile(r"(sk-[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN|api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9]{8,})")
        self.assertIsNone(pattern.search(text))


class ScriptTests(unittest.TestCase):
    def _run(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / f"scripts/{script}"), *args],
                              cwd=ROOT, text=True, capture_output=True)

    def test_record_escalation_valid_teacher_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "runs.jsonl"
            run_id = str(uuid.uuid4())
            result = self._run(
                "record-escalation", "--run-id", run_id, "--task-class", "bounded",
                "--student-model", "qwen3.6:27b-coding", "--student-outcome", "failed",
                "--failure-category", "test_failure", "--teacher-provider", "deepseek",
                "--teacher-model", "deepseek-coder", "--teacher-reason", "failure",
                "--teacher-outcome", "accepted", "--human-review-outcome", "accepted",
                "--output", str(output),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            records = learning.load_jsonl(output)
            self.assertEqual(1, len(records))
            learning.validate_escalation_record(records[0])
            self.assertEqual(run_id, records[0]["run_id"])

    def test_record_escalation_valid_local_only_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "runs.jsonl"
            result = self._run(
                "record-escalation", "--run-id", str(uuid.uuid4()), "--task-class", "bounded",
                "--student-model", "qwen3.6:27b-coding", "--student-outcome", "succeeded",
                "--correction-required", "no", "--human-review-outcome", "accepted",
                "--output", str(output),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(output.exists())

    def test_record_escalation_rejects_negative_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "runs.jsonl"
            result = self._run(
                "record-escalation", "--run-id", str(uuid.uuid4()), "--task-class", "bounded",
                "--student-model", "qwen3.6:27b-coding", "--student-outcome", "failed",
                "--failure-category", "test_failure", "--duration-ms", "-1", "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())

    def test_record_escalation_rejects_inconsistent_teacher_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "runs.jsonl"
            result = self._run(
                "record-escalation", "--run-id", str(uuid.uuid4()), "--task-class", "bounded",
                "--student-model", "qwen3.6:27b-coding", "--student-outcome", "failed",
                "--failure-category", "test_failure", "--teacher-provider", "deepseek",
                "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())

    def test_record_escalation_rejects_secret_without_echo(self) -> None:
        secret = "SuperSecret12345"
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "runs.jsonl"
            result = self._run(
                "record-escalation", "--run-id", str(uuid.uuid4()), "--task-class", "bounded",
                "--student-model", "qwen3.6:27b-coding", "--student-outcome", "succeeded",
                "--note", f"password={secret}", "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_record_candidate_refuses_unreviewed_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "candidates.jsonl"
            result = self._run(
                "record-learning-candidate", "--source-run", str(uuid.uuid4()),
                "--task-class", "bounded", "--observed-failure", "x",
                "--failure-category", "test_failure", "--destination", "lesson",
                "--status", "promoted", "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())

    def test_record_candidate_reviewed_without_root_cause_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / "candidates.jsonl"
            result = self._run(
                "record-learning-candidate", "--source-run", str(uuid.uuid4()),
                "--task-class", "bounded", "--observed-failure", "x",
                "--failure-category", "test_failure", "--destination", "lesson",
                "--status", "reviewed", "--output", str(output),
            )
            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())

    def test_learning_metrics_reads_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "runs.jsonl"
            source.write_text("\n".join(json.dumps(escalation()) for _ in range(2)) + "\n", encoding="utf-8")
            result = self._run("learning-metrics", "--input", str(source), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            metrics = json.loads(result.stdout)
            self.assertEqual(2, metrics["total_tasks"])
            self.assertEqual(1.0, metrics["paid_escalation_rate"])

    def test_learning_metrics_mean_uses_known_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "runs.jsonl"
            rows = [escalation(human_correction_count=2), escalation(human_correction_count=None)]
            source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            result = self._run("learning-metrics", "--input", str(source), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            metrics = json.loads(result.stdout)
            self.assertEqual(2.0, metrics["mean_human_corrections_per_task"])
            self.assertEqual(1, metrics["tasks_with_known_correction_count"])

    def test_run_eval_discovers_regression_suite(self) -> None:
        result = self._run("run-eval", "--help")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("qwen-regression-example", result.stdout)


class ConfigSyntaxTests(unittest.TestCase):
    def test_schemas_and_configs_parse(self) -> None:
        for relative in (
            "models/roles.yaml",
            "telemetry/taxonomy/failure-categories.yaml",
            "telemetry/schemas/escalation-run.schema.json",
            "datasets/schemas/learning-candidate.schema.json",
        ):
            self.assertIsInstance(infra.load_json(ROOT / relative), dict)


if __name__ == "__main__":
    unittest.main()
