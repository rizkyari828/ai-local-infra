"""Result Cache + Incremental DAG Reuse V0.1 integration tests. Deterministic,
fake-backend only — proves cache hits consume zero AI calls, only affected DAG
branches rerun, and descendants survive when an upstream accepted result is
unchanged. No real model calls, no network."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import execution  # noqa: E402
import evidence  # noqa: E402
import orchestration_store  # noqa: E402
import result_cache  # noqa: E402


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "stable.json").write_text('{"mode": "safe"}\n', encoding="utf-8")
    (tmp / "arch.txt").write_text("architecture notes v1\n", encoding="utf-8")
    (tmp / "cfg.txt").write_text("config body v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


def _tool(task_id, operation, *, deps=None, args=None):
    return {"task_id": task_id, "title": operation, "objective_summary": operation,
            "task_family": "deterministic_tool", "task_class": "MECHANICAL",
            "depends_on": list(deps or []), "conditional": False, "intent": "READ_ONLY",
            "executor": "TOOL_ONLY", "tool": {"operation": operation, "args": args or {}},
            "routing": None, "context": {"budget": "TINY"}, "acceptance_criteria": ["ok"],
            "risk_signals": [], "knowledge_refs": []}


def _ai(task_id, title, *, deps=None, refs=None, tier="BALANCED", effort="medium",
        family="generic", task_class="NORMAL", acceptance=("ok",), conditional=False):
    return {"task_id": task_id, "title": title, "objective_summary": title, "task_family": family,
            "task_class": task_class, "depends_on": list(deps or []), "conditional": conditional,
            "intent": "READ_ONLY", "executor": "AI",
            "routing": {"tier": tier, "effort": effort, "role": "student"},
            "context": {"budget": "SMALL"}, "acceptance_criteria": list(acceptance),
            "risk_signals": [], "knowledge_refs": [], "context_refs": list(refs or [])}


def _plan():
    return {"schema_version": 1, "valid": True, "intent": "READ_ONLY",
            "order": ["T1", "T2", "T3", "T4", "T5"], "nodes": [
                _tool("T1", "config_parse", deps=[], args={"path": "stable.json"}),
                _ai("T2", "analyze architecture", deps=["T1"],
                    refs=[{"path": "arch.txt", "start": 1, "end": 10}], tier="STRONG", effort="high",
                    family="multi_machine_acceptance", task_class="CORRECTNESS_SENSITIVE"),
                _ai("T3", "analyze configuration", deps=["T1"],
                    refs=[{"path": "cfg.txt", "start": 1, "end": 10}], family="configuration_resolution"),
                _tool("T4", "finalize", deps=["T2", "T3"]),
                _ai("T5", "final semantic analysis", deps=["T2", "T3", "T4"],
                    family="report_recovery_validation"),
            ]}


class ReuseFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, backend, plan=None, **kw):
        kw.setdefault("allow_dirty", True)
        kw.setdefault("cache", True)
        kw.setdefault("record_evidence", False)
        return execution.execute_plan(plan or _plan(), backend=backend, state_dir=self.state,
                                      repo_root=self.root, confirm=True, **kw)

    def _warm(self, **kw):
        backend = execution.FakeBackend()
        return self._run(backend, **kw), backend


class ColdWarmTests(ReuseFixture):
    def test_cold_run_writes_and_warm_run_reuses(self) -> None:
        cold, cold_backend = self._warm()
        self.assertEqual(0, cold["cache"]["hits"])
        self.assertGreater(cold["cache"]["writes"], 0)
        self.assertEqual(3, cold["logical_ai_nodes"])
        self.assertEqual(3, len(cold_backend.calls))

        warm, warm_backend = self._warm()
        self.assertEqual([], warm_backend.calls)  # zero AI calls on a full hit
        self.assertEqual(3, warm["cache"]["hits"])
        self.assertEqual(0, warm["real_ai_calls"])
        for tid in ("T2", "T3", "T5"):
            self.assertEqual("CACHE", warm["nodes"][tid]["execution_source"])

    def test_cache_hit_consumes_zero_ai_budget(self) -> None:
        self._warm()
        warm, backend = self._warm(max_ai_calls=0)  # would block any real call
        self.assertEqual([], backend.calls)
        self.assertEqual(0, warm["calls_used"])
        self.assertFalse(warm.get("error"))


class IncrementalDagTests(ReuseFixture):
    def test_one_relevant_change_only_that_node_reruns(self) -> None:
        self._warm()
        (self.root / "cfg.txt").write_text("config body v2\n", encoding="utf-8")
        backend = execution.FakeBackend()
        report = self._run(backend, record_evidence=False)
        decisions = {d["task_id"]: d for d in report["cache"]["decisions"] if d.get("decision") != "CACHE_WRITTEN"}
        self.assertEqual("REUSE", decisions["T2"]["decision"])
        self.assertEqual("RERUN", decisions["T3"]["decision"])
        self.assertEqual("CACHE_MISS", decisions["T3"]["reason"])
        self.assertEqual(["T3"], [c["task_id"] for c in backend.calls])  # only T3 executed

    def test_independent_sibling_unaffected(self) -> None:
        self._warm()
        (self.root / "arch.txt").write_text("architecture notes v2\n", encoding="utf-8")
        backend = execution.FakeBackend()
        report = self._run(backend, record_evidence=False)
        decisions = {d["task_id"]: d for d in report["cache"]["decisions"] if d.get("decision") != "CACHE_WRITTEN"}
        self.assertEqual("RERUN", decisions["T2"]["decision"])
        self.assertEqual("REUSE", decisions["T3"]["decision"])
        self.assertEqual(["T2"], [c["task_id"] for c in backend.calls])

    def test_ancestor_reruns_but_same_result_keeps_descendant_reusable(self) -> None:
        self._warm()
        # Change only T3's input; the fake worker returns an identical accepted result.
        (self.root / "cfg.txt").write_text("config body v2\n", encoding="utf-8")
        backend = execution.FakeBackend()
        report = self._run(backend, record_evidence=False)
        decisions = {d["task_id"]: d for d in report["cache"]["decisions"] if d.get("decision") != "CACHE_WRITTEN"}
        self.assertEqual("RERUN", decisions["T3"]["decision"])
        self.assertEqual("REUSE", decisions["T5"]["decision"])  # dependency fingerprint unchanged
        self.assertEqual(["T3"], [c["task_id"] for c in backend.calls])

    def test_ancestor_changed_result_invalidates_descendant(self) -> None:
        self._warm()
        (self.root / "cfg.txt").write_text("config body v2\n", encoding="utf-8")
        backend = execution.FakeBackend(script={"T3": [
            {"status": "SUCCESS", "completion_signal": "agent_reported",
             "acceptance_results": {"ok": True}, "summary": "T3 changed conclusion"}]})
        report = self._run(backend, record_evidence=False)
        decisions = {d["task_id"]: d for d in report["cache"]["decisions"] if d.get("decision") != "CACHE_WRITTEN"}
        self.assertEqual("RERUN", decisions["T3"]["decision"])
        self.assertEqual("RERUN", decisions["T5"]["decision"])  # dependency fingerprint changed
        self.assertEqual({"T3", "T5"}, {c["task_id"] for c in backend.calls})

    def test_unknown_dependency_completeness_forces_conservative_rerun(self) -> None:
        # T2 completes but returns an unstructured result (no acceptance map -> no
        # acceptance evidence, no fingerprint). The milestone contract makes such a
        # result UNVERIFIED rather than SUCCESS, so T5 cannot prove its dependency is
        # unchanged and stays unresolved instead of being silently reused.
        backend = execution.FakeBackend(script={"T2": [
            {"status": "SUCCESS", "completion_signal": "agent_reported",
             "acceptance_results": {}, "summary": "unstructured"}]})
        report = self._run(backend)
        self.assertEqual("UNVERIFIED", report["nodes"]["T2"]["status"])
        self.assertNotIn("result_fingerprint", report["nodes"]["T2"])
        self.assertEqual("BLOCKED", report["nodes"]["T5"]["status"])
        self.assertIn("T2", report["final"]["unresolved_blockers"])

    def test_acceptance_change_invalidates(self) -> None:
        self._warm()
        changed = _plan()
        for node in changed["nodes"]:
            if node["task_id"] == "T3":
                node["acceptance_criteria"] = ["ok", "stricter criterion"]
        backend = execution.FakeBackend()
        report = self._run(backend, plan=changed, record_evidence=False)
        decisions = {d["task_id"]: d for d in report["cache"]["decisions"] if d.get("decision") != "CACHE_WRITTEN"}
        self.assertEqual("RERUN", decisions["T3"]["decision"])
        self.assertEqual("CACHE_MISS", decisions["T3"]["reason"])


class ForceRefreshTests(ReuseFixture):
    def test_force_refresh_bypasses_cache_and_keeps_entries(self) -> None:
        self._warm()
        before = result_cache.ResultCache(state_dir=self.state).status()["entries"]
        backend = execution.FakeBackend()
        report = self._run(backend, record_evidence=False, force_refresh=True)
        self.assertEqual(3, len(backend.calls))  # every AI task re-executed
        self.assertEqual(0, report["cache"]["hits"])
        reasons = {d["task_id"]: d["reason"] for d in report["cache"]["decisions"]
                   if d.get("decision") in ("REUSE", "RERUN")}
        self.assertTrue(all(reasons[t] == result_cache.REASON_USER_FORCED_REFRESH for t in ("T2", "T3", "T5")))
        after = result_cache.ResultCache(state_dir=self.state).status()["entries"]
        self.assertGreaterEqual(after, before)  # entries are kept, never deleted

    def test_no_cache_disables_reuse_and_writes(self) -> None:
        self._warm()
        backend = execution.FakeBackend()
        report = execution.execute_plan(_plan(), backend=backend, state_dir=self.state,
                                        repo_root=self.root, confirm=True, record_evidence=False,
                                        cache=False, allow_dirty=True)
        self.assertFalse(report["cache"]["enabled"])
        self.assertEqual(3, len(backend.calls))


class BudgetTests(ReuseFixture):
    def test_known_reuse_fits_hard_budget(self) -> None:
        self._warm()
        (self.root / "cfg.txt").write_text("config body v2\n", encoding="utf-8")
        backend = execution.FakeBackend()
        report = self._run(backend, record_evidence=False, max_ai_calls=1)
        self.assertEqual(1, len(backend.calls))          # only the one changed node
        self.assertEqual(2, report["cache"]["hits"])     # T2 and T5 reused
        self.assertEqual(1, report["real_ai_calls"])
        self.assertEqual(3, report["logical_ai_nodes"])
        self.assertFalse(report.get("error"))

    def test_without_reuse_hard_budget_is_enforced(self) -> None:
        backend = execution.FakeBackend()
        report = execution.execute_plan(_plan(), backend=backend, state_dir=self.state,
                                        repo_root=self.root, confirm=True, record_evidence=False,
                                        cache=False, max_ai_calls=1, allow_dirty=True)
        self.assertIsNotNone(report.get("error"))
        self.assertLessEqual(len(backend.calls), 1)


class OutcomeLearningTests(ReuseFixture):
    def test_cache_hit_creates_no_model_evidence(self) -> None:
        self._warm(record_evidence=False)
        warm, _ = self._warm(record_evidence=True)
        rows = [row for row in evidence.all_rows(state_dir=self.state)
                if row["source"] == "observed_runtime"]
        self.assertEqual([], rows)  # no model ran; no fake success observed

    def test_fresh_execution_still_records_model_evidence(self) -> None:
        backend = execution.FakeBackend()
        self._run(backend, record_evidence=True, cache=False)
        rows = [row for row in evidence.all_rows(state_dir=self.state)
                if row["source"] == "observed_runtime"]
        self.assertEqual(3, len(rows))


class OperationalDataTests(ReuseFixture):
    def test_reuse_provenance_and_events(self) -> None:
        cold, _ = self._warm()
        warm, _ = self._warm()
        store = orchestration_store.OrchestrationStore(state_dir=self.state)
        try:
            run_id = orchestration_store.persist_run(_plan(), warm, machine_id="work-mac", store=store)
            tasks = {t["task_id"]: t for t in store.run_tasks(run_id)}
            self.assertEqual("CACHE", tasks["T2"]["execution_source"])
            self.assertIsNotNone(tasks["T2"]["cache_entry_id"])
            self.assertIsNotNone(tasks["T2"]["result_fingerprint"])
            self.assertEqual([], store.run_attempts(run_id))  # waiter/reuse run owns no fake Attempts
            events = [e["event_type"] for e in store.run_events(run_id)]
            self.assertIn("CACHE_HIT", events)
            self.assertIn("TASK_REUSED", events)
            summary = store.reuse_summary(run_id)
            self.assertEqual({"T2", "T3", "T5"}, set(summary["reused"]))
            self.assertEqual(["T1", "T4"], sorted(summary["tool_only"]))
        finally:
            store.close()


class OfflineAcceptanceTests(ReuseFixture):
    def test_deterministic_comparison_counts_only(self) -> None:
        cold, cold_backend = self._warm()
        warm, warm_backend = self._warm()
        logical = cold["logical_ai_nodes"]
        self.assertEqual(logical, warm["logical_ai_nodes"])
        self.assertEqual(logical, len(cold_backend.calls))
        self.assertEqual(0, len(warm_backend.calls))
        self.assertEqual(logical - len(warm_backend.calls), warm["cache"]["hits"])


if __name__ == "__main__":
    unittest.main()
