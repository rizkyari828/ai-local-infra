"""Execution Guardrails V0.1: intent model, plan validation, hard AI-call budget,
permission handling, and read-only enforcement. Fake backends only — no real AI."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import orchestrator  # noqa: E402
import execution  # noqa: E402
import evidence  # noqa: E402
import derived_knowledge  # noqa: E402


MIXED = "Audit the multi-machine AI infra, fix graphify config if needed, update docs, run tests, and give the final report"
BUG_OBJECTIVE = "Read-only: state the four symbolic routing tiers. Do not modify any files."


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "file.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


class IntentTests(unittest.TestCase):
    def test_readonly_examples(self) -> None:
        for task in ("inspect the configured models", "show configured routing tiers",
                     "verify configuration state", "review the current config",
                     "check whether the model is configured"):
            self.assertEqual(orchestrator.INTENT_READ_ONLY, orchestrator.execution_intent(task), task)

    def test_mutating_examples(self) -> None:
        for task in ("change the configured model", "update model-routing.yaml",
                     "fix the configuration", "implement the missing routing mapping"):
            self.assertEqual(orchestrator.INTENT_MUTATING, orchestrator.execution_intent(task), task)

    def test_configured_false_positive_fixed(self) -> None:
        self.assertFalse(orchestrator.has_mutation("inspect the configured models"))
        self.assertFalse(orchestrator.has_mutation("configuration"))
        self.assertFalse(orchestrator.has_mutation("config"))

    def test_negated_mutation_not_mutating(self) -> None:
        self.assertEqual(orchestrator.INTENT_READ_ONLY, orchestrator.execution_intent(BUG_OBJECTIVE))

    def test_mixed(self) -> None:
        self.assertEqual(orchestrator.INTENT_MIXED, orchestrator.execution_intent("audit and fix the config"))

    def test_explicit_read_only_override(self) -> None:
        self.assertEqual(orchestrator.INTENT_READ_ONLY,
                         orchestrator.execution_intent("fix the configuration", read_only=True))

    def test_read_only_plan_has_no_mutation_node(self) -> None:
        plan = orchestrator.decompose(BUG_OBJECTIVE)
        self.assertEqual("READ_ONLY", plan["intent"])
        self.assertEqual(["T1"], [n["task_id"] for n in plan["nodes"]])
        self.assertTrue(plan["valid"])

    def test_read_only_mutation_conflict(self) -> None:
        plan = orchestrator.decompose("fix the configuration", constraints={"read_only": True})
        self.assertFalse(plan["valid"])
        self.assertTrue(any(d.startswith("READ_ONLY_MUTATION_CONFLICT") for d in plan["diagnostics"]))


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, objective, backend, **kw):
        plan = orchestrator.decompose(objective)
        return execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                      confirm=True, record_evidence=kw.pop("record_evidence", False), **kw)

    def test_max_one_launches_only_one(self) -> None:
        backend = execution.FakeBackend()
        report = self._run(MIXED, backend, max_ai_calls=1)
        self.assertEqual("SUCCESS", report["nodes"]["T1"]["status"])
        self.assertEqual("CALL_BUDGET_EXHAUSTED", report["nodes"]["T2"]["status"])
        self.assertEqual(1, len(backend.calls))
        self.assertEqual(1, report["calls_used"])
        self.assertEqual("CALL_BUDGET_EXHAUSTED", report["error"])

    def test_max_two_launches_two(self) -> None:
        backend = execution.FakeBackend()
        report = self._run(MIXED, backend, max_ai_calls=2)
        self.assertEqual(2, len(backend.calls))
        self.assertEqual("CALL_BUDGET_EXHAUSTED", report["nodes"]["T3"]["status"])

    def test_retry_cannot_exceed_budget(self) -> None:
        backend = execution.FakeBackend(script={"T1": [
            {"status": "FAILURE", "failure_category": "PROVIDER_503"},
            {"status": "FAILURE", "failure_category": "PROVIDER_503"}]})
        report = self._run(BUG_OBJECTIVE, backend, max_ai_calls=1, infra_retries=1)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual("CALL_BUDGET_EXHAUSTED", report["nodes"]["T1"]["failure_category"])

    def test_escalation_cannot_exceed_budget(self) -> None:
        backend = execution.FakeBackend(script={"T1": [
            {"status": "FAILURE", "failure_category": "REASONING_INSUFFICIENT"},
            {"status": "SUCCESS", "completion_signal": "agent_reported"}]})
        report = self._run(BUG_OBJECTIVE, backend, max_ai_calls=1)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual("CALL_BUDGET_EXHAUSTED", report["nodes"]["T1"]["failure_category"])

    def test_dry_run_consumes_zero(self) -> None:
        backend = execution.FakeBackend()
        plan = orchestrator.decompose(MIXED)
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                        dry_run=True, max_ai_calls=2)
        self.assertEqual([], backend.calls)
        self.assertEqual(0, report["calls_used"])
        self.assertEqual(2, report["remaining_calls"])

    def test_resume_preserves_consumed_calls(self) -> None:
        backend = execution.FakeBackend()
        first = self._run(MIXED, backend, max_ai_calls=2, run_id="r1")
        self.assertEqual(2, first["calls_used"])
        plan = orchestrator.decompose(MIXED)
        second_backend = execution.FakeBackend()
        resumed = execution.execute_plan(plan, backend=second_backend, state_dir=self.state,
                                         repo_root=self.root, confirm=True, run_id="r1", resume=True,
                                         record_evidence=False)
        self.assertEqual(2, resumed["calls_used"])          # counter not reset
        self.assertEqual(0, resumed["remaining_calls"])     # persisted hard max still applies
        self.assertEqual([], second_backend.calls)

    def test_budget_not_counted_as_model_failure(self) -> None:
        backend = execution.FakeBackend()
        self._run(MIXED, backend, max_ai_calls=1, record_evidence=True)
        rows = evidence.all_rows(state_dir=self.state)
        self.assertEqual(1, len(rows))  # only the one executed node recorded


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_permission_required_not_escalated(self) -> None:
        backend = execution.FakeBackend(script={"T1": [
            {"status": "FAILURE", "failure_category": "PERMISSION_REQUIRED"}]})
        plan = orchestrator.decompose(BUG_OBJECTIVE)
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                        confirm=True, record_evidence=False)
        node = report["nodes"]["T1"]
        self.assertEqual("PERMISSION_REQUIRED", node["failure_category"])
        self.assertEqual(node["initial_tier"], node["final_tier"])  # no intelligence escalation

    def test_classify_permission_and_new_categories_non_intelligence(self) -> None:
        self.assertEqual("PERMISSION_REQUIRED", execution.classify_failure(1, "requires approval"))
        for category in ("PERMISSION_REQUIRED", "READ_ONLY_VIOLATION", "CALL_BUDGET_EXHAUSTED"):
            self.assertFalse(derived_knowledge.classify_failure(category)["intelligence_escalation"])


class ReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_readonly_run_succeeds_without_mutation(self) -> None:
        plan = orchestrator.decompose("inspect the repository state")
        report = execution.execute_plan(plan, backend=execution.FakeBackend(), state_dir=self.state,
                                        repo_root=self.root, confirm=True, record_evidence=False)
        self.assertEqual("SUCCESS", report["nodes"]["T1"]["status"])

    def test_readonly_violation_detected_and_stops(self) -> None:
        class Mutating(execution.Backend):
            name = "mutator"
            mode = "SIMULATED"
            def available(self): return True
            def execute(self, request):
                (self.root / "injected.txt").write_text("x", encoding="utf-8")
                return {"task_id": request["task_id"], "status": "SUCCESS",
                        "completion_signal": "agent_reported", "acceptance_results": {}}
        Mutating.root = self.root
        plan = orchestrator.decompose("inspect the multi-machine configuration architecture")
        report = execution.execute_plan(plan, backend=Mutating(), state_dir=self.state, repo_root=self.root,
                                        confirm=True, record_evidence=False)
        self.assertEqual("READ_ONLY_VIOLATION", report["error"])
        self.assertEqual("READ_ONLY_VIOLATION", report["nodes"]["T1"]["status"])
        self.assertTrue((self.root / "injected.txt").exists())  # no automatic reset/revert

    def test_codex_readonly_sandbox_flag(self) -> None:
        backend = execution.CliBackend("codex", "codex")
        command = backend._command({"prompt": "x", "model": "m", "provider": "p",
                                    "native_variant": "high", "working_dir": ".", "read_only": True})
        self.assertIn("read-only", command)
        self.assertEqual("read-only", command[command.index("-s") + 1])

    def test_orchestrator_and_execution_make_no_network_calls(self) -> None:
        for name in ("orchestrator.py", "execution.py"):
            source = (ROOT / "scripts/common" / name).read_text(encoding="utf-8")
            for needle in ("import urllib", "import requests", "import socket", "http.client"):
                self.assertNotIn(needle, source, f"{name} references {needle}")


if __name__ == "__main__":
    unittest.main()
