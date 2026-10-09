"""Deterministic-first / zero-additional-AI execution V0.1: TOOL_ONLY operations,
safety, planner bypass, budget-aware planning, no-op detection, deterministic
finalization, and outcome-learning separation. No real AI calls."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import deterministic  # noqa: E402
import orchestrator  # noqa: E402
import execution  # noqa: E402
import evidence  # noqa: E402
import outcomes  # noqa: E402


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "file.txt").write_text("x\n", encoding="utf-8")
    (tmp / "data.json").write_text(json.dumps({"a": 1, "b": {"c": "x"}}), encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


class ToolOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.tool = deterministic.ToolExecutor(root=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repository_state(self) -> None:
        result = self.tool.execute({"operation": "repository_state", "args": {}})
        self.assertEqual("git", result["executor"])
        self.assertTrue(result["clean"])
        self.assertTrue(len(result["head"]) >= 7)

    def test_git_head_matches(self) -> None:
        head = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        result = self.tool.execute({"operation": "git_head_matches", "args": {"expected": head}})
        self.assertTrue(result["matches"])

    def test_file_exists(self) -> None:
        self.assertTrue(self.tool.execute({"operation": "file_exists", "args": {"path": "file.txt"}})["exists"])
        self.assertFalse(self.tool.execute({"operation": "file_exists", "args": {"path": "nope.txt"}})["exists"])

    def test_config_parse(self) -> None:
        self.assertTrue(self.tool.execute({"operation": "config_parse", "args": {"path": "data.json"}})["parsed"])

    def test_config_value_matches_exact(self) -> None:
        match = self.tool.execute({"operation": "config_value_matches",
                                   "args": {"path": "data.json", "key": "b.c", "expected": "x"}})
        self.assertTrue(match["matches"])
        nomatch = self.tool.execute({"operation": "config_value_matches",
                                     "args": {"path": "data.json", "key": "a", "expected": 2}})
        self.assertFalse(nomatch["matches"])


class ToolSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.tool = deterministic.ToolExecutor(root=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unknown_operation_rejected(self) -> None:
        with self.assertRaises(deterministic.ToolPolicyError):
            self.tool.execute({"operation": "rm -rf", "args": {}})
        self.assertFalse(self.tool.can_execute({"operation": "shell", "args": {"command": "rm -rf /"}}))

    def test_path_escape_rejected(self) -> None:
        with self.assertRaises(deterministic.ToolPolicyError):
            self.tool.execute({"operation": "file_exists", "args": {"path": "../../etc/passwd"}})

    def test_secret_argument_rejected(self) -> None:
        with self.assertRaises(deterministic.ToolPolicyError):
            self.tool.execute({"operation": "config_value_matches",
                               "args": {"path": "data.json", "key": "a", "expected": "sk-" + "a" * 30}})

    def test_unknown_test_command_rejected(self) -> None:
        with self.assertRaises(deterministic.ToolPolicyError):
            self.tool.execute({"operation": "run_tests", "args": {"command": "rm -rf"}})


class PlannerBypassTests(unittest.TestCase):
    def test_simple_deterministic_bypasses_planner(self) -> None:
        plan = orchestrator.decompose("Report current branch, HEAD, and clean state.")
        self.assertEqual("NONE", plan["planner_mode"]["mode"])
        self.assertTrue(all(node.get("executor") == "TOOL_ONLY" for node in plan["nodes"]))
        self.assertEqual(0, plan["planning_budget"]["minimum_required_ai_calls"])

    def test_single_obvious_ai_direct(self) -> None:
        plan = orchestrator.decompose("review this bounded authentication architecture")
        self.assertEqual("NONE", plan["planner_mode"]["mode"])
        self.assertTrue(any(node.get("executor") == "AI" for node in plan["nodes"]))

    def test_complex_ambiguous_requires_planner(self) -> None:
        decision = orchestrator.planner_decision(
            "resolve the ambiguous security-sensitive authentication architecture with unclear requirements")
        self.assertEqual(orchestrator.PLANNER_AI, decision["mode"])

    def test_deterministic_objective_not_ai(self) -> None:
        decision = orchestrator.planner_decision("inspect the repository state")
        self.assertEqual(orchestrator.PLANNER_NONE, decision["mode"])


class BudgetAwarePlanningTests(unittest.TestCase):
    def test_zero_budget_all_tool_only_valid(self) -> None:
        plan = orchestrator.decompose("Report branch, HEAD, clean state and run tests",
                                      constraints={"max_ai_calls": 0})
        # no AI nodes -> valid even with a zero hard budget
        self.assertEqual(0, plan["planning_budget"]["minimum_required_ai_calls"])
        self.assertTrue(plan["valid"])

    def test_zero_budget_required_ai_rejected(self) -> None:
        plan = orchestrator.decompose("review this bounded authentication architecture",
                                      constraints={"max_ai_calls": 0})
        self.assertFalse(plan["valid"])
        self.assertTrue(any(d.startswith("INSUFFICIENT_AI_CALL_BUDGET") for d in plan["diagnostics"]))

    def test_budget_one_two_ai_nodes_rejected(self) -> None:
        plan = orchestrator.decompose(
            "Audit the multi-machine AI infra, fix graphify config if needed, update docs, run tests, and give the final report",
            constraints={"max_ai_calls": 1})
        self.assertFalse(plan["valid"])
        self.assertTrue(any("INSUFFICIENT_AI_CALL_BUDGET" in d for d in plan["diagnostics"]))


class DeterministicExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _plan(self, operation, args):
        nodes = [{"task_id": "D1", "title": "deterministic", "objective_summary": "det",
                  "task_family": "deterministic_tool", "task_class": "MECHANICAL", "depends_on": [],
                  "conditional": False, "intent": "READ_ONLY", "executor": "TOOL_ONLY",
                  "tool": {"operation": operation, "args": args}, "routing": None, "knowledge_refs": []}]
        return {"schema_version": 1, "valid": True, "intent": "READ_ONLY", "order": ["D1"],
                "nodes": nodes, "planner_mode": {"mode": "NONE"}}

    def test_tool_only_consumes_zero_ai_calls_and_no_model_evidence(self) -> None:
        report = execution.execute_plan(self._plan("repository_state", {}), backend=execution.FakeBackend(),
                                        state_dir=self.state, repo_root=self.root, confirm=True)
        self.assertEqual("SUCCESS", report["nodes"]["D1"]["status"])
        self.assertEqual(0, report["calls_used"])
        rows = evidence.all_rows(state_dir=self.state)
        self.assertTrue(all(row["source"] == "tool_only" and row["provider"] is None
                            and row["model"] is None for row in rows))
        # tool outcomes never enter model-performance stats
        self.assertEqual([], outcomes.model_performance(state_dir=self.state))

    def test_no_change_required(self) -> None:
        report = execution.execute_plan(
            self._plan("config_value_matches", {"path": "data.json", "key": "a", "expected": 1}),
            backend=execution.FakeBackend(), state_dir=self.state, repo_root=self.root,
            confirm=True, record_evidence=False)
        self.assertEqual("NO_CHANGE_REQUIRED", report["nodes"]["D1"]["status"])
        self.assertEqual(0, report["calls_used"])

    def test_desired_differs_is_not_noop(self) -> None:
        report = execution.execute_plan(
            self._plan("config_value_matches", {"path": "data.json", "key": "a", "expected": 999}),
            backend=execution.FakeBackend(), state_dir=self.state, repo_root=self.root,
            confirm=True, record_evidence=False)
        self.assertEqual("AI_REQUIRED", report["nodes"]["D1"]["status"])

    def test_tool_failure_is_not_intelligence(self) -> None:
        report = execution.execute_plan(
            self._plan("config_parse", {"path": "missing.json"}),
            backend=execution.FakeBackend(), state_dir=self.state, repo_root=self.root,
            confirm=True, record_evidence=False)
        self.assertIn(report["nodes"]["D1"]["status"], ("FAILURE", "AI_REQUIRED"))

    def test_deterministic_finalizer_and_failed_dependency(self) -> None:
        plan = orchestrator.decompose("Report branch HEAD and clean state")
        report = execution.execute_plan(plan, backend=execution.FakeBackend(), state_dir=self.state,
                                        repo_root=self.root, confirm=True, record_evidence=False)
        self.assertEqual("SUCCESS", report["nodes"]["DF"]["status"])


class OutcomeLearningSeparationTests(unittest.TestCase):
    def test_tool_categories_are_non_intelligence(self) -> None:
        import derived_knowledge
        for category in ("TOOL_EXECUTION_FAILED", "DETERMINISTIC_CHECK_FAILED", "NO_CHANGE_REQUIRED",
                         "AI_REQUIRED", "INSUFFICIENT_AI_CALL_BUDGET"):
            self.assertFalse(derived_knowledge.classify_failure(category)["intelligence_escalation"])


MIXED_OBJ = ("Inspect repository state, review the orchestration architecture for correctness, "
             "validate config/model-routing.yaml, run deterministic verification, report the final result.")


class MixedDecompositionTests(unittest.TestCase):
    def test_mixed_has_tool_only_and_ai(self) -> None:
        plan = orchestrator.decompose(MIXED_OBJ, constraints={"read_only": True, "max_ai_calls": 2})
        executors = {node.get("executor") for node in plan["nodes"]}
        self.assertEqual({"TOOL_ONLY", "AI"}, executors)
        self.assertTrue(plan["valid"])

    def test_mixed_deterministic_finalizer(self) -> None:
        plan = orchestrator.decompose(MIXED_OBJ, constraints={"max_ai_calls": 2})
        final = next(node for node in plan["nodes"] if node["task_id"] == "T6")
        self.assertEqual("TOOL_ONLY", final["executor"])
        self.assertEqual("finalize", final["tool"]["operation"])

    def test_mixed_semantic_synthesis_keeps_ai_finalizer(self) -> None:
        plan = orchestrator.decompose(
            "Inspect repository state and synthesize the architecture decision for the cross-machine design.",
            constraints={})
        final = next(node for node in plan["nodes"] if node["task_id"] == "T6")
        self.assertEqual("AI", final["executor"])

    def test_config_parse_detected_with_punctuation(self) -> None:
        plan = orchestrator.decompose(MIXED_OBJ)
        operations = [node.get("tool", {}).get("operation") for node in plan["nodes"]
                      if node.get("executor") == "TOOL_ONLY"]
        self.assertIn("config_parse", operations)

    def test_repository_state_converted_to_tool_only(self) -> None:
        plan = orchestrator.decompose(MIXED_OBJ)
        t1 = next(node for node in plan["nodes"] if node["task_id"] == "T1")
        self.assertEqual("TOOL_ONLY", t1["executor"])
        self.assertEqual("repository_state", t1["tool"]["operation"])

    def test_deterministic_nodes_do_not_consume_ai_budget(self) -> None:
        plan = orchestrator.decompose(MIXED_OBJ, constraints={"max_ai_calls": 2})
        ai = sum(1 for node in plan["nodes"] if node.get("executor") == "AI")
        self.assertEqual(ai, plan["planning_budget"]["minimum_required_ai_calls"])
        self.assertLess(plan["planning_budget"]["minimum_required_ai_calls"], len(plan["nodes"]))
        self.assertTrue(plan["valid"])

    def test_genuine_ai_over_budget_rejected(self) -> None:
        plan = orchestrator.decompose(MIXED_OBJ, constraints={"max_ai_calls": 1})
        self.assertFalse(plan["valid"])
        self.assertTrue(any("INSUFFICIENT_AI_CALL_BUDGET" in d for d in plan["diagnostics"]))

    def test_no_excessive_splitting(self) -> None:
        plan = orchestrator.decompose(MIXED_OBJ)
        self.assertLessEqual(len(plan["nodes"]), orchestrator.MAX_PLAN_NODES)

    def test_reasoning_not_classified_tool_only(self) -> None:
        plan = orchestrator.decompose(MIXED_OBJ)
        arch = next(node for node in plan["nodes"] if node["task_id"] == "T2")
        self.assertEqual("AI", arch["executor"])
        self.assertEqual("CORRECTNESS_SENSITIVE", arch["task_class"])

    def test_decomposition_provider_independent(self) -> None:
        a = orchestrator.decompose(MIXED_OBJ)
        b = orchestrator.decompose(MIXED_OBJ)
        self.assertEqual([n.get("executor") for n in a["nodes"]], [n.get("executor") for n in b["nodes"]])
        self.assertEqual([n["task_id"] for n in a["nodes"]], [n["task_id"] for n in b["nodes"]])


if __name__ == "__main__":
    unittest.main()
