"""Direct Mode Foundation + Automatic Surface Hooks V0.1 tests. Deterministic,
fake-adapter only. Proves direct entry, true-zero-AI, automatic lifecycle recording,
idempotence, crash/interrupt behavior, and cache-hit lifecycle. No real AI calls."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import execution  # noqa: E402
import efficiency  # noqa: E402
import evidence  # noqa: E402
import failover  # noqa: E402
import lifecycle  # noqa: E402
import orchestration_store  # noqa: E402

ORCH = ROOT / "scripts" / "common" / "orchestrate_cli.py"


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "stable.json").write_text('{"mode": "safe"}\n', encoding="utf-8")
    (tmp / "arch.txt").write_text("architecture v1\n", encoding="utf-8")
    (tmp / "cfg.txt").write_text("config v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


def _ai(task_id, *, deps=None, refs=None, tier="BALANCED", effort="medium", acceptance=("ok",)):
    return {"task_id": task_id, "title": task_id, "objective_summary": task_id, "task_family": "generic",
            "task_class": "NORMAL", "depends_on": list(deps or []), "conditional": False,
            "intent": "READ_ONLY", "executor": "AI",
            "routing": {"tier": tier, "effort": effort, "role": "student"},
            "context": {"budget": "SMALL"}, "acceptance_criteria": list(acceptance),
            "risk_signals": [], "knowledge_refs": [], "context_refs": list(refs or [])}


def _plan(nodes):
    return {"schema_version": 1, "valid": True, "intent": "READ_ONLY",
            "order": [n["task_id"] for n in nodes], "nodes": nodes}


def _plan_ai1():
    return _plan([_ai("A1", refs=[{"path": "arch.txt", "start": 1, "end": 5}])])


def _plan_ai2():
    return _plan([_ai("A1", refs=[{"path": "arch.txt", "start": 1, "end": 5}]),
                  _ai("A2", refs=[{"path": "cfg.txt", "start": 1, "end": 5}])])


class DirectFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _direct(self, plan, *, backend=None, **kw):
        backend = backend or execution.FakeBackend()
        kw.setdefault("confirm", True)
        kw.setdefault("cache", True)
        kw.setdefault("allow_dirty", True)
        kw.setdefault("record_evidence", False)
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                        entry_mode="DIRECT_CLI", **kw)
        lifecycle.record_run(plan, report, state_dir=self.state, machine_id="work-mac",
                             entry_mode="DIRECT_CLI")
        return report, backend


class DirectZeroAiTests(DirectFixture):
    def test_deterministic_true_zero_ai_via_run_direct(self) -> None:
        plan, report = lifecycle.run_direct("Report current branch, report HEAD, and inspect repository state.",
                                            backend=execution.FakeBackend(), state_dir=self.state,
                                            repo_root=self.root, confirm=True, allow_dirty=True,
                                            entry_mode="DIRECT_CLI")
        eff = efficiency.get_run_efficiency(report["run_id"], state_dir=self.state)
        self.assertEqual("DIRECT_CLI", eff["entry_mode"])
        self.assertEqual("NONE", eff["planner_mode"])
        self.assertEqual(2, eff["tasks"]["tool_only"])
        self.assertEqual(0, eff["tasks"]["logical_ai"])
        self.assertEqual(0, eff["ai"]["real_invocation_attempts"])
        self.assertTrue(eff["zero_ai"]["true_zero_ai"])
        self.assertEqual(0, eff["zero_ai"]["controller_ai_calls"])

    def test_cache_only_true_zero_ai(self) -> None:
        cold, cold_backend = self._direct(_plan_ai2())
        self.assertEqual(2, len(cold_backend.calls))
        warm, warm_backend = self._direct(_plan_ai2())
        self.assertEqual([], warm_backend.calls)
        eff = efficiency.get_run_efficiency(warm["run_id"], state_dir=self.state)
        self.assertEqual(2, eff["tasks"]["logical_ai"])
        self.assertEqual(2, eff["tasks"]["cache_reused_ai"])
        self.assertEqual(0, eff["ai"]["real_invocation_attempts"])
        self.assertTrue(eff["zero_ai"]["true_zero_ai"])
        self.assertEqual(2, eff["ai"]["calls_avoided_by_cache"])
        self.assertTrue(all(warm["nodes"][t]["execution_source"] == "CACHE" for t in ("A1", "A2")))

    def test_fresh_fake_ai_is_not_true_zero(self) -> None:
        report, backend = self._direct(_plan_ai1())
        self.assertEqual(1, len(backend.calls))
        eff = efficiency.get_run_efficiency(report["run_id"], state_dir=self.state)
        self.assertEqual(1, eff["ai"]["real_invocation_attempts"])
        self.assertFalse(eff["zero_ai"]["true_zero_ai"])
        self.assertFalse(eff["zero_ai"]["zero_additional_ai"])


class ChatHandoffTests(DirectFixture):
    def test_zero_additional_ai_but_not_true_zero(self) -> None:
        # A chat controller may already have spent AI calls the engine cannot see.
        plan, report = lifecycle.run_direct("Report current branch, report HEAD, and inspect repository state.",
                                            backend=execution.FakeBackend(), state_dir=self.state,
                                            repo_root=self.root, confirm=True, allow_dirty=True,
                                            entry_mode="CHAT_HANDOFF")
        store = orchestration_store.OrchestrationStore(state_dir=self.state)
        try:
            run = store.get_run(report["run_id"])
            self.assertEqual("CHAT_HANDOFF", run["entry_mode"])
            self.assertIsNone(run["controller_ai_calls"])  # never invented
        finally:
            store.close()
        eff = efficiency.get_run_efficiency(report["run_id"], state_dir=self.state)
        self.assertTrue(eff["zero_ai"]["zero_additional_ai"])
        self.assertFalse(eff["zero_ai"]["true_zero_ai"])


class DirectSafetyTests(DirectFixture):
    def test_read_only_intent_preserved(self) -> None:
        with self.assertRaises(execution.ExecutionError):
            lifecycle.run_direct("fix the configuration", backend=execution.FakeBackend(),
                                 state_dir=self.state, repo_root=self.root, confirm=True,
                                 allow_dirty=True, constraints={"read_only": True})

    def test_hard_budget_preserved(self) -> None:
        backend = execution.FakeBackend()
        report = execution.execute_plan(_plan_ai2(), backend=backend, state_dir=self.state,
                                        repo_root=self.root, confirm=True, allow_dirty=True,
                                        record_evidence=False, cache=False, max_ai_calls=1,
                                        entry_mode="DIRECT_CLI")
        self.assertLessEqual(len(backend.calls), 1)
        self.assertIsNotNone(report.get("error"))

    def test_objective_file_input(self) -> None:
        objective_file = pathlib.Path(self.tmp.name) / "objective.txt"
        objective_file.write_text("Report current branch, report HEAD, and inspect repository state.", encoding="utf-8")
        env = {"LAI_STATE_DIR": str(self.state), "LAI_MACHINE": "work-mac", "PATH": "/usr/bin:/bin"}
        result = subprocess.run([sys.executable, str(ORCH), "direct", "--objective-file", str(objective_file),
                                 "--backend", "fake", "--confirm", "--allow-dirty", "--json"],
                                text=True, capture_output=True, cwd=str(ROOT), env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        # The raw objective is never persisted; only a bounded summary is stored.
        store = orchestration_store.OrchestrationStore(state_dir=self.state)
        try:
            run = store.get_run(store.recent_runs(1)[0]["run_id"])
            self.assertEqual("DIRECT_CLI", run["entry_mode"])
            self.assertLessEqual(len(run["objective_summary"]), 200)
        finally:
            store.close()


class AutomaticHookTests(DirectFixture):
    def _scripted(self, name, results):
        backend = execution.FakeBackend()
        backend.name = name

        def execute(request):
            backend.calls.append({"task_id": request["task_id"]})
            return results.pop(0)
        backend.execute = execute
        return backend

    def test_opencode_and_codex_share_lifecycle_contract(self) -> None:
        for name in ("opencode", "codex"):
            with self.subTest(backend=name):
                state = pathlib.Path(self.tmp.name) / f"state-{name}"
                backend = self._scripted(name, [{"task_id": "A1", "status": "SUCCESS",
                                                 "completion_signal": "agent_reported",
                                                 "acceptance_results": {"ok": True}}])
                report = execution.execute_plan(_plan_ai1(), backend=backend, state_dir=state,
                                                repo_root=self.root, confirm=True, allow_dirty=True,
                                                record_evidence=True, entry_mode="DIRECT_CLI")
                lifecycle.record_run(_plan_ai1(), report, state_dir=state, machine_id="work-mac",
                                     entry_mode="DIRECT_CLI")
                store = orchestration_store.OrchestrationStore(state_dir=state)
                try:
                    self.assertEqual(name, store.run_attempts(report["run_id"])[0]["backend"])
                finally:
                    store.close()
                self.assertEqual(0, evidence.status(state_dir=state)["pending"])

    def test_record_run_is_idempotent(self) -> None:
        backend = execution.FakeBackend()
        plan = _plan_ai1()
        report = execution.execute_plan(plan, backend=backend, state_dir=self.state, repo_root=self.root,
                                        confirm=True, allow_dirty=True, record_evidence=True,
                                        entry_mode="DIRECT_CLI")
        first = lifecycle.record_run(plan, report, state_dir=self.state, machine_id="work-mac")
        before = self._counts()
        second = lifecycle.record_run(plan, report, state_dir=self.state, machine_id="work-mac")
        self.assertEqual(first, second)
        self.assertEqual(before, self._counts())  # no duplicate run/task/attempt/event

    def _counts(self):
        store = orchestration_store.OrchestrationStore(state_dir=self.state)
        try:
            return (store.status()["runs"], store.status()["attempts"], store.status()["events"],
                    len(evidence.all_rows(state_dir=self.state)))
        finally:
            store.close()

    def test_double_finish_rejected(self) -> None:
        decision_id = evidence.start(task_family="demo", state_dir=self.state)
        evidence.finish(decision_id, outcome="SUCCESS", completion_signal="agent_reported",
                        state_dir=self.state)
        with self.assertRaises(evidence.EvidenceError):
            evidence.finish(decision_id, outcome="SUCCESS", completion_signal="agent_reported",
                            state_dir=self.state)


class LifecycleEdgeTests(DirectFixture):
    def test_adapter_exception_is_not_false_success(self) -> None:
        class Boom(execution.Backend):
            name = "boom"
            mode = "SIMULATED"

            def available(self):
                return True

            def execute(self, request):
                raise RuntimeError("boom")

        report = execution.execute_plan(_plan_ai1(), backend=Boom(), state_dir=self.state,
                                        repo_root=self.root, confirm=True, allow_dirty=True,
                                        record_evidence=True, entry_mode="DIRECT_CLI")
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual("UNKNOWN_FAILURE", report["nodes"]["A1"]["failure_category"])
        self.assertEqual(0, evidence.status(state_dir=self.state)["pending"])  # finished, not orphan

    def test_timeout_is_non_intelligence_failure(self) -> None:
        backend = execution.FakeBackend(script={"A1": [{"status": "FAILURE", "failure_category": "TIMEOUT"}]})
        report = execution.execute_plan(_plan_ai1(), backend=backend, state_dir=self.state,
                                        repo_root=self.root, confirm=True, allow_dirty=True,
                                        record_evidence=True, infra_retries=0, entry_mode="DIRECT_CLI")
        self.assertEqual("TIMEOUT", report["nodes"]["A1"]["failure_category"])
        rows = [r for r in evidence.all_rows(state_dir=self.state) if r["source"] == "observed_runtime"]
        self.assertTrue(all(r["failure_category"] == "TIMEOUT" for r in rows))

    def test_retry_lifecycle_records_two_attempts_one_task(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "FAILURE", "failure_category": "IMPLEMENTATION_ERROR"},
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": {"ok": True}}]})
        report = execution.execute_plan(_plan_ai1(), backend=backend, state_dir=self.state,
                                        repo_root=self.root, confirm=True, allow_dirty=True,
                                        record_evidence=True, entry_mode="DIRECT_CLI")
        lifecycle.record_run(_plan_ai1(), report, state_dir=self.state, machine_id="work-mac")
        eff = efficiency.get_run_efficiency(report["run_id"], state_dir=self.state)
        self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual(1, eff["tasks"]["logical_ai"])
        self.assertEqual(2, eff["ai"]["real_invocation_attempts"])
        self.assertEqual(1, eff["ai"]["retry_calls"])

    def test_fallback_lifecycle(self) -> None:
        plan = _plan([_ai("A1", tier="STRONG", effort="high")])
        codex = execution.FakeBackend(script={"A1": [{"task_id": "A1", "status": "FAILURE",
                                                      "failure_category": "RATE_LIMIT"}]})
        codex.name = "codex"
        opencode = execution.FakeBackend(script={"A1": [{"task_id": "A1", "status": "SUCCESS",
                                                         "completion_signal": "agent_reported",
                                                         "acceptance_results": {"ok": True}}]})
        opencode.name = "opencode"
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}}, codex_model="gpt-x")
        specs = [{"backend": "codex", "provider": "codex", "model": "gpt-x", "native_variant": "high",
                  "tier": "STRONG", "effort": "high"},
                 {"backend": "opencode", "provider": "deepseek", "model": "deepseek-flash",
                  "native_variant": "high", "tier": "STRONG", "effort": "high"}]
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", return_value=specs):
            report = execution.execute_plan(plan, backend=opencode,
                                            backends={"opencode": opencode, "codex": codex}, health=health,
                                            constraints={}, repo_root=self.root, confirm=True,
                                            record_evidence=False, infra_retries=0, max_ai_calls=3,
                                            state_dir=self.state, entry_mode="DIRECT_CLI")
        lifecycle.record_run(plan, report, state_dir=self.state, machine_id="work-mac")
        eff = efficiency.get_run_efficiency(report["run_id"], state_dir=self.state)
        self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual(2, eff["ai"]["real_invocation_attempts"])
        self.assertEqual(1, eff["ai"]["fallback_calls"])

    def test_interrupt_leaves_no_false_success_and_pending_detectable(self) -> None:
        class Interrupt(execution.Backend):
            name = "intr"
            mode = "SIMULATED"

            def available(self):
                return True

            def execute(self, request):
                raise KeyboardInterrupt()

        report = execution.execute_plan(_plan_ai1(), backend=Interrupt(), state_dir=self.state,
                                        repo_root=self.root, confirm=True, allow_dirty=True,
                                        record_evidence=True, entry_mode="DIRECT_CLI")
        self.assertEqual("CANCELLED", report["nodes"]["A1"]["status"])
        # The in-flight decision remains detectably pending; it is never fabricated as SUCCESS.
        self.assertGreaterEqual(evidence.status(state_dir=self.state)["pending"], 1)


class CacheHitLifecycleTests(DirectFixture):
    def test_cache_hit_lifecycle(self) -> None:
        self._direct(_plan_ai1())  # warm
        report, backend = self._direct(_plan_ai1())
        self.assertEqual([], backend.calls)
        store = orchestration_store.OrchestrationStore(state_dir=self.state)
        try:
            task = store.run_tasks(report["run_id"])[0]
            self.assertEqual("SUCCESS", task["status"])
            self.assertEqual("CACHE", task["execution_source"])
            self.assertIsNotNone(task["cache_entry_id"])
            self.assertEqual([], store.run_attempts(report["run_id"]))  # no attempt on reuse
        finally:
            store.close()
        runtime_rows = [r for r in evidence.all_rows(state_dir=self.state)
                        if r["source"] == "observed_runtime"]
        self.assertEqual([], runtime_rows)  # no model success observed for a cache hit


class ToolOnlyLifecycleTests(DirectFixture):
    def test_tool_only_lifecycle_no_model_evidence(self) -> None:
        plan, report = lifecycle.run_direct("Report current branch, report HEAD, and inspect repository state.",
                                            backend=execution.FakeBackend(), state_dir=self.state,
                                            repo_root=self.root, confirm=True, allow_dirty=True,
                                            record_evidence=True, entry_mode="DIRECT_CLI")
        store = orchestration_store.OrchestrationStore(state_dir=self.state)
        try:
            self.assertEqual([], store.run_attempts(report["run_id"]))
            self.assertTrue(all(t["executor_type"] == "TOOL_ONLY" for t in store.run_tasks(report["run_id"])))
        finally:
            store.close()
        runtime_rows = [r for r in evidence.all_rows(state_dir=self.state)
                        if r["source"] == "observed_runtime"]
        self.assertEqual([], runtime_rows)
        self.assertEqual(0, evidence.status(state_dir=self.state)["pending"])


if __name__ == "__main__":
    unittest.main()
