"""Execution Contract Stabilization V0.1 — FIX PASS 4 adversarial regressions.

Deterministic, fake/stub adapters only, temporary repositories, synthetic fixtures.
Zero real model/provider calls. One focused regression per remaining Astra bypass,
plus a nearby equivalent variant for each probe.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import evidence  # noqa: E402
import execution  # noqa: E402
import failover  # noqa: E402
import orchestrator  # noqa: E402


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "input.txt").write_text("input v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


def _accept(*criteria) -> dict:
    return {c: True for c in (criteria or ("ok",))}


def _ai(task_id, title, *, deps=None, intent="READ_ONLY", acceptance=("ok",),
        family="generic", task_class="NORMAL", tier="BALANCED", effort="medium", budget="SMALL",
        contract=None):
    node = {"task_id": task_id, "title": title, "objective_summary": title, "task_family": family,
            "task_class": task_class, "depends_on": list(deps or []), "conditional": False,
            "intent": intent, "executor": "AI",
            "routing": {"tier": tier, "effort": effort, "role": "student"},
            "context": {"budget": budget}, "acceptance_criteria": list(acceptance),
            "risk_signals": [], "knowledge_refs": [], "context_refs": []}
    if contract is not None:
        node["task_contract"] = contract
    return node


def _plan(nodes, *, intent="READ_ONLY"):
    return {"schema_version": 1, "valid": True, "intent": intent, "planner_mode": {"mode": "NONE"},
            "order": [n["task_id"] for n in nodes], "nodes": nodes}


def permit_local_inference(test: unittest.TestCase) -> None:
    """Declare the machine this fixture runs on (see tests/test_stabilization_fix_pass2.py).

    Hard machine policy binds at the shared execution boundary, so a mechanics fixture
    states a machine whose policy permits local inference. No assertion changes.
    """
    patcher = unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "home-wsl"})
    patcher.start()
    test.addCleanup(patcher.stop)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"
        permit_local_inference(self)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, plan, backend=None, **kw):
        kw.setdefault("confirm", True)
        kw.setdefault("allow_dirty", True)
        kw.setdefault("record_evidence", False)
        backend = backend or execution.FakeBackend()
        return execution.execute_plan(plan, backend=backend, state_dir=self.state,
                                      repo_root=self.root, **kw), backend


# --------------------------------------------------------------------------- #
# 1. MUTATING same-run retry must fail closed
# --------------------------------------------------------------------------- #
class MutationRetryTests(Fixture):
    def test_mutating_timeout_is_not_retried(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "FAILURE", "failure_category": "TIMEOUT"},
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": _accept("ok")},
        ]})
        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        report, _ = self._run(plan, backend=backend, infra_retries=1)
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual("TIMEOUT", report["nodes"]["A1"]["failure_category"])
        self.assertEqual(1, len(backend.calls))  # hypothetical second success never launched
        self.assertEqual(1, report["calls_used"])
        self.assertEqual(1, len(report["attempts"]))
        self.assertEqual("MUTATION_OUTCOME_UNCERTAIN", report["nodes"]["A1"]["mutation_outcome"])

    def test_mutating_adapter_exception_is_not_retried(self) -> None:
        class Boom(execution.Backend):
            name = "boomm"
            mode = "SUPPORTED"

            def __init__(self):
                self.calls = []

            def available(self):
                return True

            def execute(self, request):
                self.calls.append(request)
                raise RuntimeError("adapter exploded after side effect")

        backend = Boom()
        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        report, _ = self._run(plan, backend=backend, infra_retries=1)
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual(1, len(backend.calls))
        self.assertEqual(1, report["calls_used"])
        self.assertEqual("MUTATION_OUTCOME_UNCERTAIN", report["nodes"]["A1"]["mutation_outcome"])

    def test_read_only_infra_failure_retains_retry_semantics(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "FAILURE", "failure_category": "PROVIDER_503"},
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": _accept("ok")},
        ]})
        plan = _plan([_ai("A1", "review")])
        report, _ = self._run(plan, backend=backend, infra_retries=1)
        self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual(2, len(backend.calls))
        self.assertEqual(2, report["calls_used"])
        self.assertEqual(2, len(report["attempts"]))

    def test_mutating_cancellation_is_not_retried(self) -> None:
        workspace = self.root

        class CancelMutator(execution.Backend):
            name = "cancelretry"
            mode = "SUPPORTED"

            def __init__(self):
                self.calls = []

            def available(self):
                return True

            def execute(self, request):
                self.calls.append(request)
                (workspace / "mutated.txt").write_text("x", encoding="utf-8")
                raise KeyboardInterrupt()

        backend = CancelMutator()
        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        report, _ = self._run(plan, backend=backend)
        self.assertEqual("CANCELLED", report["nodes"]["A1"]["status"])
        self.assertEqual(1, len(backend.calls))
        self.assertEqual(1, len(report["attempts"]))
        self.assertEqual("CANCELLED", report["attempts"][0]["status"])


# --------------------------------------------------------------------------- #
# 2. Whole-object acceptance reconciliation
# --------------------------------------------------------------------------- #
class AcceptanceReconciliationTests(Fixture):
    def _status(self, criteria, acceptance, **kw):
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": acceptance}]})
        report, _ = self._run(_plan([_ai("A1", "review", acceptance=criteria)]), backend=backend, **kw)
        return report["nodes"]["A1"]["status"], report["nodes"]["A1"].get("failure_category"), report

    def test_positive_criterion_cannot_override_overall_failed(self) -> None:
        status, _, _ = self._status(("criterion",), {"criterion": True, "status": "FAILED"})
        self.assertNotEqual("SUCCESS", status)
        self.assertEqual("FAILURE", status)

    def test_overall_passed_cannot_hide_required_criterion_failure(self) -> None:
        status, _, _ = self._status(("criterion",), {"criterion": False, "status": "PASSED"})
        self.assertNotEqual("SUCCESS", status)
        self.assertEqual("FAILURE", status)

    def test_valid_consistent_result_still_passes(self) -> None:
        status, _, _ = self._status(("criterion",), {"criterion": True, "status": "PASSED"})
        self.assertEqual("SUCCESS", status)

    def test_conflicting_evidence_produces_no_positive_evidence_or_cache(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported",
             "acceptance_results": {"criterion": True, "status": "FAILED"}}]})
        report, _ = self._run(_plan([_ai("A1", "review", acceptance=("criterion",))]), backend=backend,
                              cache=True, record_evidence=True)
        self.assertNotEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual(0, report["cache"]["writes"])
        rows = [row for row in evidence.all_rows(state_dir=self.state)
                if row["source"] == "observed_runtime" and row["outcome"] == "SUCCESS"]
        self.assertEqual([], rows)


# --------------------------------------------------------------------------- #
# 3. Authoritative objective text must be lossless
# --------------------------------------------------------------------------- #
class ObjectiveLosslessTests(Fixture):
    def _single_task(self, objective):
        plan = orchestrator.decompose(objective)
        ai = [n for n in plan["nodes"] if n.get("executor") == "AI"]
        self.assertEqual(1, len(ai))
        return plan, ai[0]

    def test_double_space_requirement_is_preserved(self) -> None:
        exact = 'Fix the parser. The quoted requirement is "alpha  beta" and it must hold.'
        plan, node = self._single_task(exact)
        self.assertIn('"alpha  beta"', node["task_contract"]["objective"])
        collapsed, _ = self._single_task('Fix the parser. The quoted requirement is "alpha beta" and it must hold.')
        self.assertNotEqual(node["task_contract"]["master_fingerprint"],
                            collapsed["nodes"][0]["task_contract"]["master_fingerprint"])

    def test_double_space_reaches_worker_and_changes_delivered_identity(self) -> None:
        exact = 'Fix the parser. The quoted requirement is "alpha  beta" and it must hold.'
        collapsed = 'Fix the parser. The quoted requirement is "alpha beta" and it must hold.'
        captured: list[dict] = []

        class Probe(execution.FakeBackend):
            def execute(self, request):
                captured.append(request)
                return super().execute(request)

        self._run(orchestrator.decompose(exact), backend=Probe())
        self.assertTrue(captured)
        self.assertIn('"alpha  beta"', captured[0]["prompt"])
        self.assertNotIn('"alpha beta"', captured[0]["prompt"])
        left, _ = self._single_task(exact)
        right, _ = self._single_task(collapsed)
        self.assertNotEqual(execution.delivered_input_fingerprint(left["nodes"][0], [], None),
                            execution.delivered_input_fingerprint(right["nodes"][0], [], None))

    def test_newline_code_fragment_is_preserved(self) -> None:
        exact = ("Fix the parser.\n\nRequirement snippet:\n    def f():\n        return 1\n\n"
                 "Keep the indentation exactly as shown.")
        plan, node = self._single_task(exact)
        objective = node["task_contract"]["objective"]
        self.assertIn("    def f():\n        return 1", objective)
        self.assertIn("\n\nRequirement snippet:", objective)

    def test_tab_in_requirement_is_preserved(self) -> None:
        exact = "Fix the parser.\tThe requirement\tis a tab inside the quoted text."
        plan, node = self._single_task(exact)
        self.assertIn("\t", node["task_contract"]["objective"])


# --------------------------------------------------------------------------- #
# 4. Escalation must not relabel the same candidate
# --------------------------------------------------------------------------- #
class EscalationAttributionTests(Fixture):
    def test_same_candidate_is_not_relabeled_as_strong(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "FAILURE", "failure_category": "REASONING_INSUFFICIENT"},
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": _accept("ok")},
        ]})
        plan = _plan([_ai("A1", "analyze architecture", tier="BALANCED", effort="high")])
        report, _ = self._run(plan, backend=backend)
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual("BALANCED", report["nodes"]["A1"]["final_tier"])
        self.assertEqual("BALANCED", report["nodes"]["A1"]["initial_tier"])
        self.assertFalse(report["nodes"]["A1"]["escalated"])
        self.assertEqual(1, len(backend.calls))

    def test_escalation_request_without_qualified_candidate_promotes_nothing(self) -> None:
        # STRONG candidates exist in inventory but no runnable qualified one is selectable.
        backend = execution.FakeBackend(script={"A1": [
            {"status": "FAILURE", "failure_category": "REASONING_INSUFFICIENT"}]})
        backend.name = "codex"
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}},
                                 codex_model="gpt-b")
        balanced = [{"backend": "codex", "provider": "codex", "model": "gpt-b", "native_variant": "high",
                     "tier": "BALANCED", "effort": "high"}]
        strong = [{"backend": "opencode", "provider": "deepseek", "model": "deepseek-strong",
                   "native_variant": "max", "tier": "STRONG", "effort": "max"}]
        plan = _plan([_ai("A1", "analyze architecture", tier="BALANCED", effort="high")])

        def candidates(tier, config=None):
            return strong if tier == "STRONG" else balanced

        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", side_effect=candidates):
            report = execution.execute_plan(plan, backend=backend, backends={"codex": backend}, health=health,
                                            constraints={}, repo_root=self.root, confirm=True,
                                            record_evidence=False, infra_retries=0, max_ai_calls=3,
                                            state_dir=self.state, allow_dirty=True)
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual("BALANCED", report["nodes"]["A1"]["final_tier"])
        self.assertEqual(1, len(backend.calls))

    def test_real_stronger_candidate_escalates_truthfully(self) -> None:
        failed = execution.FakeBackend(script={"A1": [
            {"status": "FAILURE", "failure_category": "REASONING_INSUFFICIENT"}]})
        failed.name = "codex"
        accepted = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": _accept("ok")}]})
        accepted.name = "opencode"
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}},
                                 codex_model="gpt-b")
        balanced = [{"backend": "codex", "provider": "codex", "model": "gpt-b", "native_variant": "high",
                     "tier": "BALANCED", "effort": "high"}]
        strong = [{"backend": "opencode", "provider": "deepseek", "model": "deepseek-strong",
                   "native_variant": "max", "tier": "STRONG", "effort": "max"}]

        def candidates(tier, config=None):
            return strong if tier == "STRONG" else balanced

        plan = _plan([_ai("A1", "analyze architecture", tier="BALANCED", effort="high")])
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", side_effect=candidates):
            report = execution.execute_plan(plan, backend=accepted, backends={"opencode": accepted, "codex": failed},
                                            health=health, constraints={}, repo_root=self.root, confirm=True,
                                            record_evidence=False, infra_retries=0, max_ai_calls=3,
                                            state_dir=self.state, allow_dirty=True)
        self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual("STRONG", report["nodes"]["A1"]["final_tier"])
        self.assertTrue(report["nodes"]["A1"]["escalated"])
        self.assertEqual(1, len(failed.calls))
        self.assertEqual(1, len(accepted.calls))


# --------------------------------------------------------------------------- #
# 5. Attempt exists before result processing can fail
# --------------------------------------------------------------------------- #
class AttemptAtLaunchTests(Fixture):
    def test_adapter_returning_none_records_attempt(self) -> None:
        class NoneBackend(execution.Backend):
            name = "noneback"
            mode = "SUPPORTED"

            def __init__(self):
                self.calls = []

            def available(self):
                return True

            def execute(self, request):
                self.calls.append(request)
                return None

        backend = NoneBackend()
        report, _ = self._run(_plan([_ai("A1", "review")]), backend=backend)
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual("UNKNOWN_FAILURE", report["nodes"]["A1"]["failure_category"])
        self.assertEqual(1, report["calls_used"])
        self.assertEqual(1, len(report["attempts"]))
        self.assertEqual("FAILED", report["attempts"][0]["status"])
        self.assertEqual(1, len(backend.calls))

    def test_unexpected_result_type_records_attempt(self) -> None:
        class ListBackend(execution.Backend):
            name = "listback"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                return ["not", "a", "dict"]

        report, _ = self._run(_plan([_ai("A1", "review")]), backend=ListBackend())
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual(1, report["calls_used"])
        self.assertEqual(1, len(report["attempts"]))

    def test_post_launch_processing_exception_preserves_attempt(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": _accept("ok")}]})
        with unittest.mock.patch.object(execution, "_acceptance", side_effect=RuntimeError("parse exploded")):
            report, _ = self._run(_plan([_ai("A1", "review")]), backend=backend)
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual("UNKNOWN_FAILURE", report["nodes"]["A1"]["failure_category"])
        self.assertEqual(1, report["calls_used"])
        self.assertEqual(1, len(report["attempts"]))
        self.assertEqual("FAILED", report["attempts"][0]["status"])

    def test_pre_launch_rejection_still_records_no_attempt(self) -> None:
        plan = _plan([_ai("A1", "review")])
        report, backend = self._run(plan, max_ai_calls=0)
        self.assertEqual([], report["attempts"])
        self.assertEqual([], backend.calls)


# --------------------------------------------------------------------------- #
# Adversarial probes P1-P5 (independent variants of the exact Astra cases)
# --------------------------------------------------------------------------- #
class AdversarialProbes(Fixture):
    def test_p1_mutating_side_effect_plus_timeout_is_single_launch(self) -> None:
        workspace = self.root

        class TimeoutMutator(execution.Backend):
            name = "timeoutmut"
            mode = "SUPPORTED"

            def __init__(self):
                self.calls = []

            def available(self):
                return True

            def execute(self, request):
                self.calls.append(request)
                (workspace / "side-effect.txt").write_text("once", encoding="utf-8")
                return {"task_id": request["task_id"], "status": "FAILURE", "failure_category": "TIMEOUT"}

        backend = TimeoutMutator()
        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        report, _ = self._run(plan, backend=backend, infra_retries=1)
        self.assertEqual(1, len(backend.calls))
        self.assertEqual("once", (workspace / "side-effect.txt").read_text(encoding="utf-8"))
        self.assertEqual(1, report["calls_used"])

    def test_p2_positive_criterion_with_failed_overall_is_not_success(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported",
             "acceptance_results": {"tests": True, "lint": True, "status": "FAILED"}}]})
        report, _ = self._run(_plan([_ai("A1", "review", acceptance=("tests", "lint"))]), backend=backend)
        self.assertNotEqual("SUCCESS", report["nodes"]["A1"]["status"])

    def test_p3_double_space_objective_reaches_worker_exactly(self) -> None:
        exact = "Fix it so that the marker is alpha  beta inside the output."
        captured: list[str] = []

        class Probe(execution.FakeBackend):
            def execute(self, request):
                captured.append(request["prompt"])
                return super().execute(request)

        self._run(orchestrator.decompose(exact), backend=Probe())
        self.assertTrue(captured)
        self.assertIn("alpha  beta", captured[0])
        self.assertNotIn("alpha beta", captured[0].replace("alpha  beta", ""))
        self.assertNotEqual(orchestrator._objective_fingerprint(exact),
                            orchestrator._objective_fingerprint("Fix it so that the marker is alpha beta inside the output."))

    def test_p4_requested_strong_but_only_balanced_retains_balanced(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "FAILURE", "failure_category": "REASONING_INSUFFICIENT"},
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": _accept("ok")}]})
        plan = _plan([_ai("A1", "analyze architecture", tier="BALANCED", effort="high",
                          contract={"objective": "analyze architecture", "capability_requirement":
                                    {"tier": "STRONG", "effort": "max"}})])
        report, _ = self._run(plan, backend=backend)
        self.assertEqual("BALANCED", report["nodes"]["A1"]["final_tier"])
        self.assertEqual(1, len(backend.calls))

    def test_p5_none_result_attempt_count_matches_launch_count(self) -> None:
        class NoneBackend(execution.Backend):
            name = "noneback2"
            mode = "SUPPORTED"

            def __init__(self):
                self.calls = []

            def available(self):
                return True

            def execute(self, request):
                self.calls.append(request)
                return None

        backend = NoneBackend()
        report, _ = self._run(_plan([_ai("A1", "review"), _ai("A2", "review two", deps=["A1"])]), backend=backend)
        self.assertEqual(len(backend.calls), report["calls_used"])
        self.assertEqual(len(backend.calls), len(report["attempts"]))
        self.assertEqual(1, len(backend.calls))  # A2 blocked, A1 launch accounted


if __name__ == "__main__":
    unittest.main()
