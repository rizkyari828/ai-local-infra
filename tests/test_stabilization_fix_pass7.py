"""Governing-action ownership, exact source spans and final offline delivery."""

from __future__ import annotations

from test_stabilization_fix_pass5 import Fixture, execution, orchestrator


AMBIGUOUS = (
    "Delete docs and current branch",
    "Update docs and current branch",
    "Delete docs and repository status",
    "Rename config and current branch",
    "Inspect docs and current branch",
    "current branch and update docs",
    "Create file and worktree",
    "Rename config and repository status",
    "Remove cache and HEAD",
    "Move file and repository metadata",
    "Delete config and working tree",  # nearby noun continuation, not a listed probe
    "Report current branch, HEAD, and clean state.",
    "Report branch, HEAD, clean state and run tests",
    "Report branch HEAD and clean state",
)

EXPLICIT = (
    "Inspect docs and report current branch",
    "Check repository status and run tests",
    "Update docs and run tests",
    "Review config; update docs; run tests",
    "Fix alpha and update tests",
    "report current branch and update docs",
    "Inspect repository state and report current branch",
    "Report HEAD and inspect worktree",
)


class SemanticActionOwnershipTests(Fixture):
    def delivery(self, plan, *, incomplete=False):
        requests, tools = [], []

        class Worker(execution.FakeBackend):
            def execute(self, request):
                requests.append(request)
                result = super().execute(request)
                if incomplete:
                    result["acceptance_results"] = {}
                return result

        class Tool(execution.deterministic.ToolExecutor):
            def execute(self, task):
                tools.append(task)
                if task["operation"] == "run_tests":
                    return {"ok": True}  # offline test adapter, never a nested suite/provider
                return super().execute(task)

        report, _ = self._run(plan, backend=Worker(), tool_executor=Tool(root=self.root))
        return report, requests, tools

    def assert_intact(self, objective):
        self.assertFalse(orchestrator.objective_is_provably_safe_for_decomposition(objective))
        plan = orchestrator.decompose(objective)
        self.assertTrue(plan["valid"], plan["diagnostics"])
        self.assertEqual(1, len(plan["nodes"]))
        node = plan["nodes"][0]
        self.assertEqual("AI", node["executor"])
        self.assertEqual([0, len(objective)], node["objective_span"])
        self.assertEqual(objective, node["task_contract"]["objective"])
        report, requests, tools = self.delivery(plan)
        self.assertEqual("SUCCESS", report["nodes"][node["task_id"]]["status"])
        self.assertEqual([], tools)
        self.assertEqual([objective], [r["task_contract"]["objective"] for r in requests])
        self.assertIn(f"Objective: {objective}\nOperation:", requests[0]["prompt"])

    def test_shared_action_and_bare_nouns_reach_one_worker_intact(self):
        for objective in AMBIGUOUS:
            with self.subTest(objective=objective):
                self.assert_intact(objective)

    def test_mutation_verbs_do_not_inherit_across_separators(self):
        for verb in ("Delete", "Remove", "Rename", "Move", "Modify", "Update", "Create", "Reset", "Switch"):
            for separator in (" and ", ", ", "; "):
                with self.subTest(verb=verb, separator=separator):
                    objective = f"{verb} docs{separator}current branch"
                    self.assert_intact(objective)
                    plan = orchestrator.decompose(objective)
                    _, requests, _ = self.delivery(plan)
                    self.assertFalse(requests[0]["read_only"])
        for verb in ("move", "reset", "switch"):
            self.assertFalse(orchestrator.has_mutation(f"Inspect docs and do not {verb} current branch"))

    def test_explicit_actions_have_complete_final_execution_ownership(self):
        for objective in EXPLICIT:
            with self.subTest(objective=objective):
                plan = orchestrator.decompose(objective)
                self.assertTrue(plan["valid"], plan["diagnostics"])
                report, requests, tools = self.delivery(plan)
                self.assertTrue(all(n["status"] in ("SUCCESS", "SKIPPED") for n in report["nodes"].values()))
                self.assertTrue(orchestrator.objective_is_provably_safe_for_decomposition(objective))
                for node in plan["nodes"]:
                    start, end = node["objective_span"]
                    self.assertEqual(objective[start:end], node["task_contract"]["objective"])
                for request in requests:
                    start, end = request["task_contract"]["source_span"]
                    self.assertEqual(objective[start:end], request["task_contract"]["objective"])
                    self.assertIn(f"Objective: {objective[start:end]}\nOperation:", request["prompt"])
                # Inspect actual workers/tools, not only planned contracts.
                for _, _, clause in orchestrator._clause_spans(objective):
                    self.assertTrue(
                        any(clause in r["task_contract"]["objective"]
                            and (not orchestrator.has_mutation(clause) or not r["read_only"])
                            for r in requests)
                        or any(orchestrator._deterministic_covers(clause, [t]) for t in tools),
                        clause,
                    )
                if objective not in ("Check repository status and run tests", "Report HEAD and inspect worktree"):
                    self.assertGreater(len(plan["nodes"]), 1)
                if "current branch" in objective:
                    self.assertTrue(any(t["operation"] == "repository_state" for t in tools))

    def test_repository_nouns_never_prove_tool_action(self):
        task = {"operation": "repository_state", "args": {}}
        for objective in ("current branch", "repository status", "repository state", "HEAD", "worktree", "docs", "tests", "run current branch", "give current branch", "Update current branch"):
            with self.subTest(objective=objective):
                self.assertFalse(orchestrator._deterministic_covers(objective, [task]))
                plan = orchestrator.decompose(objective)
                report, requests, tools = self.delivery(plan)
                self.assertEqual([], tools)
                self.assertEqual([objective], [r["task_contract"]["objective"] for r in requests])
                self.assertEqual("SUCCESS", report["nodes"]["T1"]["status"])
        for objective in ("inspect current branch", "report current branch", "check repository state", "read repository state"):
            with self.subTest(objective=objective):
                self.assertTrue(orchestrator._deterministic_covers(objective, [task]))
                _, requests, tools = self.delivery(orchestrator.decompose(objective))
                self.assertEqual([], requests)
                self.assertIn("repository_state", [t["operation"] for t in tools])

    def test_mixed_tools_cannot_launder_an_unowned_action(self):
        # A run_tests operation elsewhere cannot make "run current branch" read-like.
        objective = "run current branch and run tests"
        tasks = execution.deterministic.classify_objective(objective)
        self.assertFalse(orchestrator._deterministic_covers(objective, tasks))
        for objective in (objective, "Update current branch and run tests", "Inspect current branch; update docs; report current branch", "Run tests and read tests", "Run tests and inspect tests"):
            with self.subTest(objective=objective):
                plan = orchestrator.decompose(objective)
                self.assertEqual([objective], [n["task_contract"]["objective"] for n in plan["nodes"]])
                _, requests, tools = self.delivery(plan)
                self.assertEqual([], tools)
                self.assertEqual([objective], [r["task_contract"]["objective"] for r in requests])

    def test_owner_permissions_use_the_governing_verb_not_target_words(self):
        plan = orchestrator.decompose("Review update and report current branch")
        report, requests, tools = self.delivery(plan)
        self.assertTrue(plan["valid"])
        self.assertTrue(all(r["read_only"] for r in requests))
        self.assertEqual(["Review update"], [r["task_contract"]["objective"] for r in requests])
        self.assertEqual(["repository_state", "finalize"], [t["operation"] for t in tools])
        self.assertTrue(all(n["status"] == "SUCCESS" for n in report["nodes"].values()))

    def test_conditional_stage_cannot_solely_own_an_unconditional_action(self):
        for verb in ("update", "move"):
            objective = f"Review config; {verb} docs; reset architecture; report current branch"
            with self.subTest(objective=objective):
                self.assertTrue(orchestrator.objective_is_provably_safe_for_decomposition(objective))
                plan = orchestrator.decompose(objective)
                self.assertEqual([objective], [n["task_contract"]["objective"] for n in plan["nodes"]])
                report, requests, tools = self.delivery(plan)
                self.assertEqual([], tools)
                self.assertEqual([objective], [r["task_contract"]["objective"] for r in requests])
                self.assertFalse(requests[0]["read_only"])
                self.assertEqual("SUCCESS", report["nodes"]["T1"]["status"])

    def test_supporting_reuse_cannot_mark_a_new_clause_owned(self):
        objective = "Review config; reset architecture"
        slicer = orchestrator._Slicer(orchestrator._clause_spans(objective))
        first = slicer.for_kind("architecture")
        reused = slicer.for_kind("implement", reuse=True)
        self.assertEqual("Review config", first[2])
        self.assertIsNone(reused)
        self.assertEqual({0}, slicer.used)

    def test_source_coverage_gate_rejects_forged_noun_span(self):
        objective = "Delete docs and current branch"
        spans = [(0, 11, "Delete docs"), (16, len(objective), "current branch")]
        slicer = orchestrator._Slicer(spans)
        nodes = [{"task_id": "T1", "executor": "TOOL_ONLY", "tool": {"operation": "repository_state", "args": {}}}]
        self.assertFalse(orchestrator._assign_task_slices(nodes, slicer, objective))

    def test_incomplete_worker_acceptance_cannot_report_success(self):
        objective = "Delete docs and current branch"
        plan = orchestrator.decompose(objective)
        report, requests, tools = self.delivery(plan, incomplete=True)
        self.assertEqual([], tools)
        self.assertEqual([objective], [r["task_contract"]["objective"] for r in requests])
        self.assertEqual("UNVERIFIED", report["nodes"]["T1"]["status"])
        self.assertNotEqual("PASSED", report["nodes"]["T1"].get("acceptance"))

    def test_unknown_syntax_still_falls_back_intact(self):
        for objective in (
            "Fix output to exactly “alpha and beta”; update docs; run tests",
            "Fix code to:\n~~~python\nalpha and beta\n~~~\n; run tests",
            "Inspect docs → report current branch",
        ):
            with self.subTest(objective=objective):
                self.assert_intact(objective)


if __name__ == "__main__":
    import unittest
    unittest.main()
