"""Efficiency Telemetry V0.1 tests: metric semantics, no double counting, model/cache/
context breakdowns, entry modes, and security. Deterministic and offline; no AI calls."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import efficiency  # noqa: E402
import orchestration_store as store_mod  # noqa: E402
import context_retrieval  # noqa: E402


def _tool(task_id, op):
    return {"task_id": task_id, "title": op, "objective_summary": op, "executor": "TOOL_ONLY",
            "task_family": "deterministic_tool", "task_class": "MECHANICAL", "depends_on": [],
            "conditional": False, "intent": "READ_ONLY", "tool": {"operation": op, "args": {}},
            "routing": None, "context": {"budget": "TINY"}, "acceptance_criteria": ["ok"]}


def _ai(task_id, depends=None):
    return {"task_id": task_id, "title": task_id, "objective_summary": task_id, "executor": "AI",
            "task_family": "generic", "task_class": "NORMAL", "depends_on": list(depends or []),
            "conditional": False, "intent": "READ_ONLY",
            "routing": {"tier": "BALANCED", "effort": "medium"},
            "context": {"budget": "SMALL"}, "acceptance_criteria": ["ok"]}


def _plan(nodes):
    return {"schema_version": 1, "valid": True, "intent": "READ_ONLY",
            "order": [n["task_id"] for n in nodes], "nodes": nodes,
            "planner_mode": {"mode": "NONE"}, "objective_summary": "synthetic",
            "planning_budget": {"hard_max_ai_calls": None, "planned_ai_calls": 0,
                                "minimum_required_ai_calls": 0, "tool_only_nodes": 0}}


def _attempt(task_id, n, status, *, backend="opencode", provider="deepseek", model="deepseek-flash",
             tier="BALANCED", effort="medium", native="medium", normalized="MEDIUM",
             failure=None, retry=False, fallback=False):
    return {"task_id": task_id, "attempt_no": n, "backend": backend, "provider": provider,
            "model": model, "native_effort": native, "normalized_effort": normalized,
            "status": status, "required_tier": tier, "required_effort": effort,
            "failure_category": failure, "is_retry": retry, "is_fallback": fallback,
            "completion_signal": "agent_reported" if status == "SUCCESS" else None}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.store = store_mod.OrchestrationStore(state_dir=self.state)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _persist(self, plan, report, **kw):
        return store_mod.persist_run(plan, report, machine_id=kw.pop("machine_id", "work-mac"),
                                     store=self.store, **kw)


class ScenarioATests(Fixture):
    def _scenario_a(self):
        nodes = [_tool("D1", "repository_state"), _tool("D2", "config_parse"), _tool("D3", "finalize"),
                 _tool("N1", "config_value_matches"),
                 _ai("A1"), _ai("A2"), _ai("A3"), _ai("A4")]
        plan = _plan(nodes)
        report = {"run_id": "runA", "head": "abc", "calls_used": 2, "error": None,
                  "entry_mode": "DIRECT_CLI", "final": {"failed": 0},
                  "nodes": {
                      "D1": {"status": "SUCCESS", "execution_source": "TOOL_ONLY"},
                      "D2": {"status": "SUCCESS", "execution_source": "TOOL_ONLY"},
                      "D3": {"status": "SUCCESS", "execution_source": "TOOL_ONLY"},
                      "N1": {"status": "NO_CHANGE_REQUIRED", "execution_source": "TOOL_ONLY"},
                      "A1": {"status": "SUCCESS", "execution_source": "CACHE", "cache_entry_id": "c1",
                             "reused_from_run_id": "r0", "reused_from_task_id": "A1",
                             "input_fingerprint": "i1", "result_fingerprint": "rf1"},
                      "A2": {"status": "SUCCESS", "execution_source": "CACHE", "cache_entry_id": "c2",
                             "reused_from_run_id": "r0", "reused_from_task_id": "A2",
                             "input_fingerprint": "i2", "result_fingerprint": "rf2"},
                      "A3": {"status": "SUCCESS", "execution_source": "EXECUTED",
                             "input_fingerprint": "i3", "result_fingerprint": "rf3"},
                      "A4": {"status": "SUCCESS", "execution_source": "EXECUTED",
                             "input_fingerprint": "i4", "result_fingerprint": "rf4"}},
                  "attempts": [_attempt("A3", 1, "SUCCESS"), _attempt("A4", 1, "SUCCESS")],
                  "cache": {"decisions": [
                      {"task_id": "A1", "decision": "REUSE", "cache_entry_id": "c1", "reason": "UNCHANGED_INPUTS"},
                      {"task_id": "A2", "decision": "REUSE", "cache_entry_id": "c2", "reason": "UNCHANGED_INPUTS"},
                      {"task_id": "A3", "decision": "RERUN", "reason": "CACHE_MISS"},
                      {"task_id": "A3", "decision": "CACHE_WRITTEN", "cache_entry_id": "c3"},
                      {"task_id": "A4", "decision": "RERUN", "reason": "CACHE_MISS"},
                      {"task_id": "A4", "decision": "CACHE_WRITTEN", "cache_entry_id": "c4"}]}}
        self._persist(plan, report)
        return efficiency.get_run_efficiency("runA", store=self.store)

    def test_scenario_a_counts(self) -> None:
        eff = self._scenario_a()
        self.assertEqual(8, eff["tasks"]["total"])
        self.assertEqual(3, eff["tasks"]["tool_only"])
        self.assertEqual(1, eff["tasks"]["no_change"])
        self.assertEqual(4, eff["tasks"]["logical_ai"])
        self.assertEqual(2, eff["tasks"]["cache_reused_ai"])
        self.assertEqual(2, eff["tasks"]["fresh_ai"])
        self.assertEqual(2, eff["ai"]["real_invocation_attempts"])
        self.assertEqual(2, eff["cache"]["hits"])
        self.assertEqual(2, eff["cache"]["misses"])
        self.assertEqual(2, eff["cache"]["written"])
        self.assertEqual(2, eff["ai"]["calls_avoided_by_cache"])

    def test_tool_only_not_counted_as_ai_and_no_fake_savings(self) -> None:
        eff = self._scenario_a()
        # TOOL_ONLY is measured independently; it is never folded into "AI calls saved".
        self.assertEqual(0, eff["zero_ai"]["local_ai_calls"] - eff["ai"]["real_invocation_attempts"])
        self.assertEqual(2, eff["ai"]["calls_avoided_by_cache"])
        self.assertEqual({"repository_state": 1, "config_parse": 1, "finalize": 1,
                          "config_value_matches": 1}, eff["tool_operations"])

    def test_no_change_is_separate_from_cache_and_ai(self) -> None:
        eff = self._scenario_a()
        self.assertEqual(1, eff["tasks"]["no_change"])
        self.assertNotEqual(eff["tasks"]["no_change"], eff["cache"]["hits"])


class ScenarioBTests(Fixture):
    def test_retry_failover_no_double_count(self) -> None:
        plan = _plan([_ai("T1")])
        report = {"run_id": "runB", "head": "abc", "calls_used": 2, "error": None,
                  "entry_mode": "DIRECT_CLI", "final": {"failed": 0},
                  "nodes": {"T1": {"status": "SUCCESS", "execution_source": "EXECUTED",
                                   "result_fingerprint": "rf"}},
                  "attempts": [_attempt("T1", 1, "FAILED", backend="codex", provider="codex",
                                        model="gpt-6.1-sol", failure="RATE_LIMIT"),
                               _attempt("T1", 2, "SUCCESS", backend="opencode", provider="deepseek",
                                        model="deepseek-flash", fallback=True)],
                  "cache": {"decisions": [{"task_id": "T1", "decision": "RERUN", "reason": "CACHE_MISS"},
                                          {"task_id": "T1", "decision": "CACHE_WRITTEN",
                                           "cache_entry_id": "c1"}]}}
        self._persist(plan, report)
        eff = efficiency.get_run_efficiency("runB", store=self.store)
        self.assertEqual(1, eff["tasks"]["logical_ai"])          # one logical task, not two
        self.assertEqual(2, eff["ai"]["real_invocation_attempts"])
        self.assertEqual(1, eff["ai"]["failed_invocations"])
        self.assertEqual(1, eff["ai"]["successful_invocations"])
        self.assertEqual(1, eff["ai"]["fallback_calls"])
        self.assertEqual(0, eff["ai"]["retry_calls"])
        self.assertEqual({"RATE_LIMIT": 1}, eff["failures"]["by_category"])
        self.assertEqual(1, eff["failures"]["failover_selected"])


class ContextScenarioFTests(Fixture):
    def test_context_metrics_reconstruct(self) -> None:
        plan = _plan([_ai("A1"), _ai("A2")])
        report = {"run_id": "runF", "head": "abc", "calls_used": 0, "error": None,
                  "entry_mode": "DIRECT_CLI", "final": {"failed": 0},
                  "nodes": {"A1": {"status": "SUCCESS"}, "A2": {"status": "SUCCESS"}},
                  "attempts": [], "cache": {"decisions": []}}
        self._persist(plan, report)
        manifest_a = {"task_id": "A1", "context_budget": "SMALL", "size": 1000, "required_count": 2,
                      "optional_count": 3, "omitted_count": 1, "duplicate_chars_avoided": 50,
                      "fallback_used": True, "graphify_status": "MISSING", "items": []}
        manifest_b = {"task_id": "A2", "context_budget": "MEDIUM", "size": 2000, "required_count": 1,
                      "optional_count": 0, "omitted_count": 0, "duplicate_chars_avoided": 10,
                      "fallback_used": False, "graphify_status": "CURRENT", "items": []}
        context_retrieval.write_manifest(self.state, "runF", "A1", manifest_a)
        context_retrieval.write_manifest(self.state, "runF", "A2", manifest_b)
        eff = efficiency.get_run_efficiency("runF", store=self.store)
        ctx = eff["context"]
        self.assertTrue(ctx["available"])
        self.assertEqual(2, ctx["pack_count"])
        self.assertEqual(3000, ctx["total_chars"])
        self.assertEqual(3, ctx["required_items"])
        self.assertEqual(3, ctx["optional_items"])
        self.assertEqual(1, ctx["optional_items_dropped"])
        self.assertEqual(60, ctx["deduplicated_chars"])
        self.assertEqual(1, ctx["graphify_fallback_packs"])
        self.assertEqual({"SMALL": 1, "MEDIUM": 1}, ctx["budget_classes"])

    def test_context_absent_is_not_fabricated(self) -> None:
        plan = _plan([_ai("A1")])
        report = {"run_id": "runNoCtx", "head": "abc", "calls_used": 0, "error": None,
                  "entry_mode": "DIRECT_CLI", "final": {"failed": 0},
                  "nodes": {"A1": {"status": "SUCCESS"}}, "attempts": [], "cache": {"decisions": []}}
        self._persist(plan, report)
        ctx = efficiency.get_run_efficiency("runNoCtx", store=self.store)["context"]
        self.assertFalse(ctx["available"])
        self.assertEqual(0, ctx["pack_count"])


class QueryLayerTests(Fixture):
    def _seed(self, run_id, machine="work-mac", project="remote:x"):
        plan = _plan([_tool("D1", "finalize")])
        report = {"run_id": run_id, "head": "abc", "calls_used": 0, "error": None,
                  "entry_mode": "DIRECT_CLI", "final": {"failed": 0},
                  "nodes": {"D1": {"status": "SUCCESS", "execution_source": "TOOL_ONLY"}},
                  "attempts": [], "cache": {"decisions": []}}
        store_mod.persist_run(plan, report, machine_id=machine, store=self.store, project_id=None)

    def test_recent_and_machine_filter(self) -> None:
        self._seed("r1", machine="work-mac")
        self._seed("r2", machine="home-wsl")
        all_recent = efficiency.get_recent_efficiency(store=self.store)
        self.assertEqual(2, all_recent["totals"]["runs"])
        only_mac = efficiency.get_recent_efficiency(store=self.store, machine_id="work-mac")
        self.assertEqual(1, only_mac["totals"]["runs"])
        self.assertEqual("work-mac", only_mac["runs"][0]["machine_id"])

    def test_model_usage_and_cache_queries(self) -> None:
        plan = _plan([_ai("A1")])
        report = {"run_id": "rm", "head": "abc", "calls_used": 1, "error": None,
                  "entry_mode": "AUTOMATION", "final": {"failed": 0},
                  "nodes": {"A1": {"status": "SUCCESS", "execution_source": "EXECUTED"}},
                  "attempts": [_attempt("A1", 1, "SUCCESS")],
                  "cache": {"decisions": [{"task_id": "A1", "decision": "RERUN", "reason": "CACHE_INVALID"}]}}
        self._persist(plan, report)
        usage = efficiency.get_model_usage(store=self.store, run_id="rm")
        self.assertEqual(1, usage["total_invocations"])
        self.assertEqual("deepseek", usage["groups"][0]["provider"])
        cache = efficiency.get_cache_efficiency(store=self.store, run_id="rm")
        self.assertEqual(1, cache["misses"])
        self.assertEqual(1, cache["invalid"])
        self.assertEqual(0, cache["calls_avoided_by_valid_hit"])


class SecurityTests(Fixture):
    def test_no_prompts_secrets_or_bodies(self) -> None:
        secret = "sk-" + "a" * 30
        plan = _plan([_ai("A1")])
        report = {"run_id": "rs", "head": "abc", "calls_used": 1, "error": None,
                  "entry_mode": "DIRECT_CLI", "final": {"failed": 0},
                  "nodes": {"A1": {"status": "SUCCESS", "execution_source": "EXECUTED"}},
                  "attempts": [_attempt("A1", 1, "SUCCESS")], "cache": {"decisions": []}}
        self._persist(plan, report)
        blob = json.dumps(efficiency.get_run_efficiency("rs", store=self.store))
        for forbidden in ("prompt", "response", "transcript", "content", secret, "api_key", "token"):
            self.assertNotIn(forbidden, blob)


if __name__ == "__main__":
    unittest.main()
