"""Multi-model execution V0.1: adapters, DAG execution, model switching, escalation,
context isolation, outcome learning, git safety, and call bounds — all via fake
backends. No real AI call is made."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import execution  # noqa: E402
import orchestrator  # noqa: E402
import evidence  # noqa: E402
import derived_knowledge  # noqa: E402


MIXED = "Audit the multi-machine AI infra, fix graphify config if needed, update docs, run tests, and give the final report"


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "file.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


class AdapterTests(unittest.TestCase):
    def test_capabilities_and_selection(self) -> None:
        self.assertEqual("SIMULATED", execution.FakeBackend().capabilities()["mode"])
        self.assertTrue(execution.FakeBackend().available())
        cli = execution.CliBackend("opencode", "opencode")
        self.assertEqual("SUPPORTED", cli.capabilities()["mode"])
        self.assertIsInstance(execution.backend_for("fake"), execution.FakeBackend)

    def test_resolution_carries_model_and_effort(self) -> None:
        plan = orchestrator.decompose(MIXED)
        bindings = execution.resolve_plan(plan, execution.FakeBackend())
        self.assertTrue(all(b["tier"] in execution.routing.DEFAULT_TIER_ORDER for b in bindings))
        self.assertTrue(all("native_variant" in b and "model" in b for b in bindings))

    def test_failure_classification(self) -> None:
        self.assertEqual("PROVIDER_503", execution.classify_failure(1, "internal 503"))
        self.assertEqual("RATE_LIMIT", execution.classify_failure(1, "429 rate limit"))
        self.assertEqual("TIMEOUT", execution.classify_failure(1, "request timed out"))
        self.assertEqual("AUTH_FAILURE", execution.classify_failure(1, "401 unauthorized"))
        self.assertEqual("ACCOUNT_MODEL_UNSUPPORTED", execution.classify_failure(1, "model not supported"))
        self.assertEqual("UNKNOWN_FAILURE", execution.classify_failure(1, "kaboom"))
        self.assertIsNone(execution.classify_failure(0, ""))

    def test_cli_backend_extracts_bounded_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = pathlib.Path(tmp) / "fakeopencode"
            binary.write_text("#!/bin/sh\necho '{\"reply\":\"ok\"}'\n", encoding="utf-8")
            binary.chmod(0o755)
            backend = execution.CliBackend("opencode", str(binary))
            request = {"task_id": "T1", "execution_id": "e1", "task_family": "f",
                       "task_class": "MECHANICAL", "tier": "FAST", "provider": "p", "model": "m",
                       "native_variant": "low", "objective_summary": "x", "acceptance_criteria": ["a"],
                       "dependency_summaries": [], "knowledge_refs": [], "context_manifest": [],
                       "working_dir": tmp, "prompt": "x"}
            result = backend.execute(request)
        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual("agent_reported", result["completion_signal"])

    def test_cli_backend_timeout_is_bounded(self) -> None:
        import time
        with tempfile.TemporaryDirectory() as tmp:
            binary = pathlib.Path(tmp) / "slowopencode"
            binary.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
            binary.chmod(0o755)
            backend = execution.CliBackend("opencode", str(binary), timeout=1)
            request = {"task_id": "T1", "execution_id": "e1", "task_family": "f",
                       "task_class": "MECHANICAL", "tier": "FAST", "provider": "p", "model": "m",
                       "native_variant": "low", "objective_summary": "x", "acceptance_criteria": ["a"],
                       "dependency_summaries": [], "knowledge_refs": [], "context_manifest": [],
                       "working_dir": tmp, "prompt": "x"}
            started = time.monotonic()
            result = backend.execute(request)
            elapsed = time.monotonic() - started
        self.assertEqual("TIMEOUT", result["failure_category"])
        self.assertLess(elapsed, 4)

    def test_unsupported_backend_reports_no_compatible(self) -> None:
        class Dead(execution.Backend):
            name = "dead"
            mode = "UNSUPPORTED"
            def available(self): return False
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(pathlib.Path(tmp))
            plan = orchestrator.decompose(MIXED)
            report = execution.execute_plan(plan, backend=Dead(), dry_run=True, repo_root=repo)
        self.assertIn("NO_COMPATIBLE_EXECUTION_BACKEND", report.get("error", ""))


class DagExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, backend, **kw):
        plan = orchestrator.decompose(MIXED)
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state,
                                        repo_root=self.root, confirm=True, **kw)
        return plan, report

    def test_sequential_success_and_order(self) -> None:
        backend = execution.FakeBackend()
        plan, report = self._run(backend)
        self.assertEqual("SUCCESS", report["nodes"]["T1"]["status"])
        self.assertEqual("SUCCESS", report["nodes"]["T6"]["status"])
        first_seen = [call["task_id"] for call in backend.calls]
        self.assertEqual(["T1", "T2", "T3", "T4", "T5", "T6"], first_seen)

    def test_dependency_summaries_passed(self) -> None:
        backend = execution.FakeBackend()
        self._run(backend)
        t4 = next(call for call in backend.calls if call["task_id"] == "T4")
        self.assertEqual(["T2", "T3"], t4["dependency_tasks"])

    def test_failed_dependency_blocks_downstream(self) -> None:
        backend = execution.FakeBackend(script={"T3": [{"status": "FAILURE", "failure_category": "UNKNOWN_FAILURE",
                                                        "summary": "boom"}]})
        plan, report = self._run(backend)
        self.assertEqual("FAILURE", report["nodes"]["T3"]["status"])
        self.assertEqual("BLOCKED", report["nodes"]["T4"]["status"])
        self.assertEqual("BLOCKED", report["nodes"]["T6"]["status"])

    def test_conditional_not_activated_on_success(self) -> None:
        plan, report = self._run(execution.FakeBackend())
        self.assertEqual("SKIPPED", report["nodes"]["T3E"]["status"])

    def test_conditional_activated_on_risk_signal(self) -> None:
        backend = execution.FakeBackend(script={"T3": [{"status": "SUCCESS",
                                                        "acceptance_results": {"requested change present": True,
                                                                               "no unintended diff": True},
                                                        "new_risk_signals": ["architecture-ambiguity"]}]})
        plan, report = self._run(backend)
        self.assertEqual("SUCCESS", report["nodes"]["T3E"]["status"])

    def test_success_not_inferred_without_completion_signal(self) -> None:
        class NoSignal(execution.Backend):
            name = "nosignal"
            mode = "SUPPORTED"
            def available(self): return True
            def execute(self, request): return {"task_id": request["task_id"], "status": "SUCCESS"}
        plan = orchestrator.decompose(MIXED)
        report = execution.execute_plan(plan, backend=NoSignal(), state_dir=self.state,
                                        repo_root=self.root, confirm=True, record_evidence=False)
        self.assertNotEqual("SUCCESS", report["nodes"]["T1"]["status"])

    def test_resume_skips_successful_nodes(self) -> None:
        # A failed READ_ONLY node is retry-safe on resume. (A failed launched MUTATING
        # node is deliberately non-replayable now — see the pass-3 uncertainty tests.)
        first = execution.FakeBackend(script={"T5": [{"status": "FAILURE", "failure_category": "UNKNOWN_FAILURE"}]})
        plan, report = self._run(first, run_id="r1", record_evidence=False)
        self.assertEqual("FAILURE", report["nodes"]["T5"]["status"])
        second = execution.FakeBackend()
        resume = execution.execute_plan(plan, backend=second, state_dir=self.state, repo_root=self.root,
                                        confirm=True, run_id="r1", resume=True, record_evidence=False)
        self.assertEqual("SUCCESS", resume["nodes"]["T5"]["status"])
        self.assertNotIn("T1", [call["task_id"] for call in second.calls])  # already-succeeded node skipped

    def test_resume_rejects_incompatible_head(self) -> None:
        plan = orchestrator.decompose(MIXED)
        run_dir = self.state / "orchestration" / "r2"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps({"head": "deadbeef", "repo": str(self.root), "nodes": {}}))
        report = execution.execute_plan(plan, backend=execution.FakeBackend(), state_dir=self.state,
                                        repo_root=self.root, confirm=True, run_id="r2", resume=True,
                                        record_evidence=False)
        self.assertIn("REFUSE_RESUME", report.get("error", ""))


class ModelSwitchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_one_run_uses_different_tiers(self) -> None:
        backend = execution.FakeBackend()
        plan = orchestrator.decompose(MIXED)
        execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                               confirm=True, record_evidence=False)
        tiers = {call["task_id"]: call["tier"] for call in backend.calls}
        self.assertEqual("FAST", tiers["T1"])
        self.assertEqual("STRONG", tiers["T2"])
        self.assertEqual("BALANCED", tiers["T3"])

    def test_graph_survives_provider_substitution(self) -> None:
        plan_a = orchestrator.decompose(MIXED)
        plan_b = orchestrator.decompose(MIXED)
        self.assertEqual(plan_a["routing_mix"], plan_b["routing_mix"])
        self.assertEqual([n["routing"]["tier"] for n in plan_a["nodes"]],
                         [n["routing"]["tier"] for n in plan_b["nodes"]])


class EscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reasoning_failure_escalates_only_that_node(self) -> None:
        backend = execution.FakeBackend(script={"T3": [
            {"status": "FAILURE", "failure_category": "REASONING_INSUFFICIENT"},
            {"status": "SUCCESS", "acceptance_results": {"requested change present": True,
                                                         "no unintended diff": True}}]})
        plan = orchestrator.decompose(MIXED)
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                        confirm=True, record_evidence=False)
        self.assertEqual("SUCCESS", report["nodes"]["T3"]["status"])
        self.assertEqual("STRONG", report["nodes"]["T3"]["final_tier"])
        self.assertTrue(report["nodes"]["T3"]["escalated"])
        self.assertEqual("BALANCED", report["nodes"]["T5"]["final_tier"])

    def test_infra_failure_does_not_escalate_intelligence(self) -> None:
        backend = execution.FakeBackend(script={"T1": [
            {"status": "FAILURE", "failure_category": "PROVIDER_503"},
            {"status": "FAILURE", "failure_category": "PROVIDER_503"}]})
        plan = orchestrator.decompose(MIXED)
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                        confirm=True, record_evidence=False, infra_retries=1)
        self.assertEqual("FAILURE", report["nodes"]["T1"]["status"])
        self.assertEqual("PROVIDER_503", report["nodes"]["T1"]["failure_category"])
        self.assertFalse(report["nodes"]["T1"]["escalated"])

    def test_max_tier_stops_after_bounded_escalation(self) -> None:
        backend = execution.FakeBackend(script={"T2": [
            {"status": "FAILURE", "failure_category": "REASONING_INSUFFICIENT"} for _ in range(5)]})
        plan = orchestrator.decompose(MIXED)
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                        confirm=True, record_evidence=False)
        self.assertEqual("FAILURE", report["nodes"]["T2"]["status"])
        self.assertEqual("MAX", report["nodes"]["T2"]["final_tier"])
        calls = [call for call in backend.calls if call["task_id"] == "T2"]
        self.assertLessEqual(len(calls), 4)  # bounded


class ContextIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_worker_receives_minimal_context_not_master(self) -> None:
        backend = execution.FakeBackend()
        plan = orchestrator.decompose(MIXED)
        execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                               confirm=True, record_evidence=False)
        joined = " ".join(call["prompt"] for call in backend.calls)
        self.assertNotIn(MIXED, joined)  # not the raw master objective
        self.assertNotIn("FULL_CHAT", joined)
        t3 = next(call for call in backend.calls if call["task_id"] == "T3")
        self.assertIn("Objective:", t3["prompt"])
        self.assertEqual(["T2"], t3["dependency_tasks"])
        self.assertIn(execution.WORKER_ISOLATION_NOTE, t3["prompt"])  # no nested agents

    def test_no_secrets_in_worker_prompt(self) -> None:
        secret = "sk-" + "a" * 30
        os.environ["DEEPSEEK_API_KEY"] = secret
        try:
            backend = execution.FakeBackend()
            plan = orchestrator.decompose(MIXED)
            execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                   confirm=True, record_evidence=False)
        finally:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        self.assertNotIn(secret, " ".join(call["prompt"] for call in backend.calls))


class OutcomeLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_one_observation_per_executed_node(self) -> None:
        backend = execution.FakeBackend()
        plan = orchestrator.decompose(MIXED)
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                        confirm=True, record_evidence=True)
        rows = evidence.all_rows(state_dir=self.state)
        executed = sum(1 for r in report["nodes"].values() if r["status"] == "SUCCESS")
        self.assertEqual(executed, len(rows))
        self.assertTrue(all(row["source"] == "observed_runtime" for row in rows))
        self.assertEqual(0, evidence.query(state_dir=self.state)["historical"])
        # backend/surface and normalized effort are recorded for fresh runtime evidence
        self.assertTrue(all(row["surface"] == "fake" for row in rows))
        self.assertTrue(all(row["normalized_effort"] for row in rows))

    def test_dry_run_records_nothing(self) -> None:
        plan = orchestrator.decompose(MIXED)
        execution.execute_plan(plan, backend=execution.FakeBackend(), state_dir=self.state,
                               repo_root=self.root, dry_run=True)
        self.assertFalse((self.state / "learning" / "evidence.db").exists())

    def test_infra_failure_recorded_as_non_intelligence(self) -> None:
        backend = execution.FakeBackend(script={"T1": [
            {"status": "FAILURE", "failure_category": "PROVIDER_503"},
            {"status": "FAILURE", "failure_category": "PROVIDER_503"}]})
        plan = orchestrator.decompose(MIXED)
        execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                               confirm=True, infra_retries=1)
        rows = [row for row in evidence.all_rows(state_dir=self.state) if row["failure_category"]]
        self.assertTrue(all(row["failure_category"] in derived_knowledge.FAILURE_CATEGORIES for row in rows))
        self.assertTrue(all(not derived_knowledge.classify_failure(row["failure_category"])["intelligence_escalation"]
                            for row in rows))


class GitSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dirty_tree_refused_without_override(self) -> None:
        (self.root / "file.txt").write_text("changed\n", encoding="utf-8")
        plan = orchestrator.decompose(MIXED)
        report = execution.execute_plan(plan, backend=execution.FakeBackend(), state_dir=self.state,
                                        repo_root=self.root, confirm=True, record_evidence=False)
        self.assertIn("dirty", report.get("error", ""))

    def test_no_auto_commit_or_push(self) -> None:
        head_before = execution._git(self.root, "rev-parse", "HEAD")
        plan = orchestrator.decompose(MIXED)
        execution.execute_plan(plan, backend=execution.FakeBackend(), state_dir=self.state,
                               repo_root=self.root, confirm=True, record_evidence=False)
        self.assertEqual(head_before, execution._git(self.root, "rev-parse", "HEAD"))
        source = (ROOT / "scripts/common/execution.py").read_text(encoding="utf-8")
        for forbidden in ('"commit"', '"push"', '"rebase"', '"reset"', '"checkout"'):
            self.assertNotIn(forbidden, source)


class CallBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_calls_are_bounded(self) -> None:
        plan = orchestrator.decompose(MIXED)
        bounds = execution.call_bounds(plan)
        backend = execution.FakeBackend()
        execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                               confirm=True, record_evidence=False)
        self.assertLessEqual(len(backend.calls), bounds["max_total_calls"])
        self.assertEqual(0, bounds["parallel_workers"])


if __name__ == "__main__":
    unittest.main()
