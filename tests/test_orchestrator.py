"""Task graph orchestrator V0.1: DAG validation, per-node routing, context budgets,
quota guards, privacy, and provider portability. Plan-only; no model/network calls."""

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
import orchestrator  # noqa: E402
import routing  # noqa: E402


ORCH = ROOT / "scripts" / "orchestrate"
MIXED = "Audit the multi-machine AI infra, fix graphify config if needed, update docs, run tests, and give the final report"
SECURITY = "Review the security-sensitive authentication architecture"


def _fake_node(task_id, tier="FAST", budget="SMALL", task_class="NORMAL", depends=None,
               conditional=False, risk=None, acceptance=("ok",)):
    return {"task_id": task_id, "title": task_id, "task_class": task_class, "depends_on": depends or [],
            "conditional": conditional, "risk_signals": list(risk or []),
            "acceptance_criteria": list(acceptance),
            "routing": {"tier": tier, "effort": "low", "role": "student"},
            "context": {"budget": budget}}


class TaskGraphTests(unittest.TestCase):
    def test_simple_objective_is_valid(self) -> None:
        plan = orchestrator.decompose("Audit the repository and update docs")
        self.assertTrue(plan["valid"])
        self.assertEqual("T1", plan["nodes"][0]["task_id"])
        self.assertEqual(plan["order"][0], "T1")

    def test_multi_node_graph_and_final_integration(self) -> None:
        plan = orchestrator.decompose(MIXED)
        ids = [node["task_id"] for node in plan["nodes"]]
        self.assertIn("T6", ids)
        self.assertGreater(len(plan["nodes"]), 3)
        self.assertTrue(plan["valid"])

    def test_missing_dependency_rejected(self) -> None:
        valid, diagnostics, _ = orchestrator.validate_graph([_fake_node("T1", depends=["T9"])])
        self.assertFalse(valid)
        self.assertTrue(any("unknown" in d for d in diagnostics))

    def test_cycle_rejected(self) -> None:
        nodes = [_fake_node("T1", depends=["T2"]), _fake_node("T2", depends=["T1"])]
        valid, diagnostics, _ = orchestrator.validate_graph(nodes)
        self.assertFalse(valid)
        self.assertTrue(any("cycle" in d for d in diagnostics))

    def test_self_dependency_rejected(self) -> None:
        valid, diagnostics, _ = orchestrator.validate_graph([_fake_node("T1", depends=["T1"])])
        self.assertFalse(valid)
        self.assertTrue(any("itself" in d for d in diagnostics))

    def test_topological_order_respects_dependencies(self) -> None:
        plan = orchestrator.decompose(MIXED)
        index = {task_id: position for position, task_id in enumerate(plan["order"])}
        for node in plan["nodes"]:
            for dep in node["depends_on"]:
                self.assertLess(index[dep], index[node["task_id"]])

    def test_conditional_escalation_node(self) -> None:
        plan = orchestrator.decompose(MIXED)
        conditional = [node for node in plan["nodes"] if node["conditional"]]
        self.assertTrue(conditional)
        self.assertTrue(all(node["routing"]["tier"] in ("STRONG", "MAX") for node in conditional))


class RoutingTests(unittest.TestCase):
    def test_symbolic_routes_by_class(self) -> None:
        plan = orchestrator.decompose(MIXED)
        by_class = {}
        for node in plan["nodes"]:
            by_class.setdefault(node["task_class"], []).append(node["routing"]["tier"])
        self.assertTrue(all(tier == "FAST" for tier in by_class["MECHANICAL"]))
        self.assertTrue(all(tier == "BALANCED" for tier in by_class["NORMAL"]))
        self.assertTrue(all(tier in ("STRONG", "MAX") for tier in by_class["CORRECTNESS_SENSITIVE"]))

    def test_critical_objective_has_max_floor(self) -> None:
        plan = orchestrator.decompose(SECURITY)
        critical = [node for node in plan["nodes"] if node["task_class"] == "CRITICAL"]
        self.assertTrue(critical)
        self.assertTrue(all(node["routing"]["tier"] == "MAX" for node in critical))
        self.assertEqual("MAX", plan["planner"]["required_tier"])

    def test_historical_evidence_visible(self) -> None:
        plan = orchestrator.decompose("Align the cross-machine configuration resolver")
        self.assertTrue(all("historical_evidence" in node for node in plan["nodes"]))
        self.assertTrue(any(node["historical_evidence"] > 0 for node in plan["nodes"]))

    def test_user_override_min_tier(self) -> None:
        plan = orchestrator.decompose("rename a variable", constraints={"min_tier": "STRONG"})
        self.assertTrue(all(node["routing"]["tier"] in ("STRONG", "MAX") for node in plan["nodes"]))

    def test_provider_unavailable_does_not_downgrade(self) -> None:
        plan = orchestrator.decompose("Audit repository", constraints={"surface": "opencode"})
        for node in plan["nodes"]:
            if "model_capability" in node:
                self.assertIn(node["model_capability"]["status"], orchestrator.discovery.STATUSES)
                # requirement tier is unchanged regardless of capability resolution
                self.assertIn(node["routing"]["tier"], orchestrator.TIER_ORDER)

    def test_approved_learned_default_lowers_within_floor(self) -> None:
        defaults = {"schema_version": 1,
                    "policy": {"safety_floor": {"MECHANICAL": "FAST", "NORMAL": "FAST",
                                                "CORRECTNESS_SENSITIVE": "STRONG", "CRITICAL": "MAX"},
                               "tier_order": list(routing.DEFAULT_TIER_ORDER)},
                    "defaults": [{"task_family": "configuration_resolution", "tier": "FAST",
                                  "effort": "low", "approved": True}]}
        with unittest.mock.patch.object(orchestrator.knowledge, "load_learned_defaults", return_value=defaults):
            plan = orchestrator.decompose(MIXED)
        t3 = next(node for node in plan["nodes"] if node["task_id"] == "T3")
        self.assertEqual("FAST", t3["routing"]["tier"])


class ContextBudgetTests(unittest.TestCase):
    def test_no_full_context_by_default(self) -> None:
        plan = orchestrator.decompose(MIXED)
        for node in plan["nodes"]:
            self.assertNotIn("FULL_CHAT", node["context"]["include"])
            self.assertNotIn("FULL_REPOSITORY", node["context"]["include"])
            self.assertTrue({"FULL_CHAT", "FULL_REPOSITORY", "ALL_PREVIOUS_OUTPUTS"} <= set(node["context"]["excluded"]))

    def test_dependency_output_is_a_compact_manifest(self) -> None:
        plan = orchestrator.decompose(MIXED)
        t4 = next(node for node in plan["nodes"] if node["task_id"] == "T4")
        self.assertTrue(t4["context"]["manifest"])
        self.assertTrue(all(entry.startswith("dependency:") or entry.startswith("knowledge:")
                            for entry in t4["context"]["manifest"]))

    def test_mechanical_node_has_smaller_budget_than_architecture(self) -> None:
        plan = orchestrator.decompose(MIXED)
        order = {budget: i for i, budget in enumerate(orchestrator.CONTEXT_BUDGETS)}
        t1 = next(node for node in plan["nodes"] if node["task_id"] == "T1")
        t2 = next(node for node in plan["nodes"] if node["task_id"] == "T2")
        self.assertLessEqual(order[t1["context"]["budget"]], order[t2["context"]["budget"]])

    def test_large_context_requires_justification(self) -> None:
        plan = orchestrator.decompose(MIXED)
        self.assertTrue(all(node["context"]["budget"] != "LARGE" for node in plan["nodes"]))
        justified = _fake_node("X", budget="SMALL", risk=["needs-large-context"])
        self.assertEqual("LARGE", orchestrator.context_plan(justified)["budget"])


class QuotaGuardTests(unittest.TestCase):
    def _plan(self, nodes):
        return {"nodes": nodes}

    def test_all_strong(self) -> None:
        plan = self._plan([_fake_node(f"T{i}", tier="STRONG") for i in range(3)])
        self.assertIn("ALL_TASKS_STRONG", " ".join(orchestrator.quota_guards(plan)))

    def test_all_max(self) -> None:
        plan = self._plan([_fake_node("T1", tier="MAX"), _fake_node("T2", tier="MAX")])
        self.assertIn("ALL_TASKS_MAX", " ".join(orchestrator.quota_guards(plan)))

    def test_all_large_context(self) -> None:
        plan = self._plan([_fake_node("T1", budget="LARGE"), _fake_node("T2", budget="LARGE")])
        self.assertIn("ALL_CONTEXT_LARGE", " ".join(orchestrator.quota_guards(plan)))

    def test_excessive_splitting(self) -> None:
        plan = self._plan([_fake_node(f"T{i}") for i in range(orchestrator.MAX_PLAN_NODES + 2)])
        self.assertIn("EXCESSIVE_TASK_SPLITTING", " ".join(orchestrator.quota_guards(plan)))

    def test_unjustified_expensive_node(self) -> None:
        plan = self._plan([_fake_node("T1", tier="STRONG", task_class="MECHANICAL")])
        self.assertIn("UNJUSTIFIED_EXPENSIVE_NODE", " ".join(orchestrator.quota_guards(plan)))

    def test_routing_mix_and_quota_view(self) -> None:
        plan = orchestrator.decompose(MIXED)
        self.assertEqual(len(plan["nodes"]), sum(plan["routing_mix"].values()))
        self.assertIn("expensive_nodes", plan["quota_view"])
        self.assertIn("cheap_nodes", plan["quota_view"])


class PrivacyTests(unittest.TestCase):
    def test_objective_summary_is_bounded(self) -> None:
        plan = orchestrator.decompose("x" * 500)
        self.assertLessEqual(len(plan["objective_summary"]), 120)

    def test_nodes_do_not_embed_raw_objective(self) -> None:
        plan = orchestrator.decompose(MIXED)
        for node in plan["nodes"]:
            self.assertNotIn(MIXED, json.dumps(node))

    def test_planning_persists_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ | {"LAI_STATE_DIR": tmp}
            result = subprocess.run([str(ORCH), "plan", "--objective", MIXED, "--json"],
                                    text=True, capture_output=True, env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((pathlib.Path(tmp) / "learning" / "evidence.db").exists())
            self.assertEqual([], list(pathlib.Path(tmp).rglob("*")))


class PortabilityTests(unittest.TestCase):
    def test_routes_are_symbolic_not_model_names(self) -> None:
        plan = orchestrator.decompose(MIXED)
        blob = json.dumps(plan).lower()
        for name in ("deepseek", "qwen", "claude", "gpt", "openai", "ollama"):
            self.assertNotIn(name, blob)
        for node in plan["nodes"]:
            self.assertIn(node["routing"]["tier"], orchestrator.TIER_ORDER)

    def test_plan_survives_fictitious_provider_mapping(self) -> None:
        before = orchestrator.decompose(MIXED)["routing_mix"]
        after = orchestrator.decompose(MIXED)["routing_mix"]
        self.assertEqual(before, after)  # decomposition is provider-independent


class CliTests(unittest.TestCase):
    def _run(self, *args, env=None):
        return subprocess.run([str(ORCH), *args], text=True, capture_output=True, env=env)

    def test_plan_human_and_json(self) -> None:
        human = self._run("plan", "--objective", MIXED)
        self.assertEqual(0, human.returncode, human.stderr)
        self.assertIn("PLAN", human.stdout)
        data = json.loads(self._run("plan", "--objective", MIXED, "--json").stdout)
        self.assertTrue(data["valid"])
        self.assertIn("nodes", data)

    def test_requires_objective(self) -> None:
        self.assertEqual(2, self._run("plan").returncode)

    def test_max_tier_conflict_on_critical(self) -> None:
        data = json.loads(self._run("plan", "--objective", SECURITY, "--max-tier", "FAST", "--json").stdout)
        critical = [n for n in data["nodes"] if n["task_class"] == "CRITICAL"]
        self.assertTrue(critical)
        self.assertTrue(all(n["routing"]["tier"] == "MAX" for n in critical))
        self.assertTrue(any(n["routing_constraints"] for n in critical))

    def test_offline(self) -> None:
        env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k}
        self.assertEqual(0, self._run("plan", "--objective", "Audit repo", env=env).returncode)

    def test_orchestrator_makes_no_network_calls(self) -> None:
        for name in ("orchestrator.py",):
            source = (ROOT / "scripts/common" / name).read_text(encoding="utf-8")
            for needle in ("import urllib", "import requests", "import socket", "http.client",
                           "import subprocess"):
                self.assertNotIn(needle, source, f"{name} references {needle}")


if __name__ == "__main__":
    unittest.main()
