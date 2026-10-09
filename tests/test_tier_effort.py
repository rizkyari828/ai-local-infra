"""Tier / effort semantics V0.1: capability tier, provider-native effort, and
normalized effort are independent axes. No real AI calls."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import failover  # noqa: E402
import discovery  # noqa: E402
import execution  # noqa: E402
import evidence  # noqa: E402


def _spec(backend, provider, model, variant, tier, effort):
    return {"backend": backend, "provider": provider, "model": model, "native_variant": variant,
            "tier": tier, "effort": effort}


class AxisTests(unittest.TestCase):
    def _health(self):
        return failover.Health(binaries={"codex": True, "opencode": True},
                               providers={"codex": {"codex"}, "opencode": {"deepseek"}},
                               codex_model="gpt-6.1-sol")

    def test_strong_high(self) -> None:
        c = failover.resolve("STRONG", "high",
                             [_spec("opencode", "deepseek", "deepseek-flash", "high", "STRONG", "high")],
                             self._health())[0]
        self.assertEqual("STRONG", c["tier"])
        self.assertEqual("high", c["native_variant"])
        self.assertEqual("HIGH", c["normalized_effort"])

    def test_max_native_high_is_normalized_high_not_max(self) -> None:
        c = failover.resolve("MAX", "max",
                             [_spec("codex", "codex", None, "high", "MAX", "max")], self._health())[0]
        self.assertEqual("MAX", c["tier"])
        self.assertEqual("high", c["native_variant"])
        self.assertEqual("HIGH", c["normalized_effort"])  # tier MAX must not overwrite effort
        self.assertNotEqual("MAX", c["normalized_effort"])

    def test_native_effort_preserved_exactly(self) -> None:
        c = failover.resolve("MAX", "max", [_spec("codex", "codex", None, "high", "MAX", "max")],
                             self._health())[0]
        self.assertEqual("high", c["native_variant"])

    def test_normalized_derived_from_native_mapping_only(self) -> None:
        self.assertEqual("MEDIUM", discovery.normalize_effort("codex", "medium", discovery.load_config()))
        self.assertEqual("HIGH", discovery.normalize_effort("codex", "high", discovery.load_config()))
        self.assertEqual("VERY_HIGH", discovery.normalize_effort("anthropic", "xhigh", discovery.load_config()))

    def test_unknown_native_stays_unmapped_not_tier(self) -> None:
        c = failover.resolve("MAX", "max", [_spec("codex", "codex", None, "nonsense", "MAX", "max")],
                             self._health())[0]
        self.assertIsNone(c["normalized_effort"])

    def test_effort_floors_are_independent_of_tier(self) -> None:
        health = self._health()
        below_tier = failover.resolve("STRONG", "high",
                                      [_spec("opencode", "deepseek", "m", "high", "BALANCED", "high")], health)[0]
        self.assertFalse(below_tier["runnable"])
        self.assertIn("below_tier_floor", below_tier["blocking"])
        below_effort = failover.resolve("STRONG", "high",
                                        [_spec("opencode", "deepseek", "m", "medium", "STRONG", "medium")], health)[0]
        self.assertFalse(below_effort["runnable"])
        self.assertIn("below_effort_floor", below_effort["blocking"])

    def test_tool_only_axes_are_null(self) -> None:
        plan = orchestrator_plan()
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "state"
            execution.execute_plan(plan, backend=execution.FakeBackend(), state_dir=state,
                                   repo_root=ROOT, confirm=True, record_evidence=True, allow_dirty=True)
            rows = [r for r in evidence.all_rows(state_dir=state) if r["source"] == "tool_only"]
            self.assertTrue(rows)
            for row in rows:
                self.assertIsNone(row["provider"])
                self.assertIsNone(row["model"])
                self.assertIsNone(row["native_variant"])
                self.assertIsNone(row["normalized_effort"])


def orchestrator_plan():
    import orchestrator
    return orchestrator.decompose("Report branch HEAD and clean state")


class ExecutionEvidenceAxisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "t"], check=True)
        (self.root / "f").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "i"], check=True, capture_output=True)
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _plan(self, class_="CRITICAL", tier="MAX", effort="max", family="security_sensitive_design"):
        return {"schema_version": 1, "valid": True, "intent": "MUTATING", "order": ["T"],
                "nodes": [{"task_id": "T", "title": "x", "objective_summary": "x", "task_family": family,
                           "task_class": class_, "depends_on": [], "conditional": False, "intent": "READ_ONLY",
                           "executor": "AI", "routing": {"tier": tier, "effort": effort, "role": "teacher-premium"},
                           "context": {"manifest": []}, "acceptance_criteria": ["ok"], "risk_signals": [],
                           "knowledge_refs": []}]}

    def test_evidence_records_actual_axes(self) -> None:
        codex = execution.FakeBackend(script={})
        codex.name = "codex"
        codex._results = [{"task_id": "T", "status": "SUCCESS", "completion_signal": "agent_reported",
                           "acceptance_results": {"ok": True}}]
        codex.execute = lambda request: (codex.calls.append({"model": request["model"],
                                                             "variant": request["native_variant"]}) or codex._results.pop(0))
        health = failover.Health(binaries={"codex": True}, providers={"codex": {"codex"}}, codex_model="gpt-6.1-sol")
        specs = {"MAX": [_spec("codex", "codex", None, "high", "MAX", "max")]}
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier",
                                        side_effect=lambda tier, config=None: specs.get(tier, [])):
            execution.execute_plan(self._plan(), backend=codex, backends={"codex": codex}, health=health,
                                   repo_root=self.root, confirm=True, record_evidence=True,
                                   max_ai_calls=1, state_dir=self.state)
        row = [r for r in evidence.all_rows(state_dir=self.state) if r["source"] == "observed_runtime"][0]
        self.assertEqual("codex", row["surface"])
        self.assertEqual("codex", row["provider"])
        self.assertEqual("gpt-6.1-sol", row["model"])
        self.assertEqual("high", row["native_variant"])
        self.assertEqual("HIGH", row["normalized_effort"])  # never MAX
        self.assertEqual("MAX", row["initial_tier"])

    def test_verification_is_exact_pair(self) -> None:
        # A different native effort is not implied by the verified high pair.
        self.assertEqual("MEDIUM", discovery.normalize_effort("codex", "medium", discovery.load_config()))
        self.assertNotEqual(discovery.normalize_effort("codex", "medium", discovery.load_config()), "HIGH")


if __name__ == "__main__":
    unittest.main()
