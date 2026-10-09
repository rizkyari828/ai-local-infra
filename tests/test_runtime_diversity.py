"""Runtime diversity V0.1: Work Mac machine policy, Codex concrete-model wiring,
cross-backend candidate resolution, effort floors, and the two-call ceiling.
Fake backends/health only — no real AI calls."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import failover  # noqa: E402
import execution  # noqa: E402
import orchestrator  # noqa: E402
import derived_knowledge  # noqa: E402


def _spec(backend, provider, model, variant, tier, effort):
    return {"backend": backend, "provider": provider, "model": model, "native_variant": variant,
            "tier": tier, "effort": effort}


class MachinePolicyTests(unittest.TestCase):
    def test_not_configured_by_machine_policy_is_not_failure(self) -> None:
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models=None,
                                 local_inference=failover.NOT_CONFIGURED_BY_MACHINE_POLICY)
        candidates = failover.resolve("BALANCED", "medium",
                                      [_spec("opencode", "ollama", "qwen", "medium", "BALANCED", "medium")],
                                      health)
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, candidates[0]["status"])
        self.assertFalse(candidates[0]["runnable"])
        self.assertIn(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, candidates[0]["blocking"])

    def test_policy_is_machine_specific_not_global(self) -> None:
        # Without the Work Mac policy, an unreachable local provider is merely UNKNOWN.
        other = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                ollama_models=None)
        candidates = failover.resolve("BALANCED", "medium",
                                      [_spec("opencode", "ollama", "qwen", "medium", "BALANCED", "medium")],
                                      other)
        self.assertEqual(failover.UNKNOWN, candidates[0]["status"])

    def test_work_mac_profile_declares_policy_and_home_wsl_does_not(self) -> None:
        work = json.loads((ROOT / "machines/work-mac.yaml").read_text(encoding="utf-8"))
        home = json.loads((ROOT / "machines/home-wsl.yaml").read_text(encoding="utf-8"))
        self.assertEqual("NOT_CONFIGURED_BY_MACHINE_POLICY", work.get("local_inference"))
        self.assertNotEqual("NOT_CONFIGURED_BY_MACHINE_POLICY", home.get("local_inference"))


class CodexMappingTests(unittest.TestCase):
    def test_local_codex_model_filled_without_routing_core_change(self) -> None:
        health = failover.Health(binaries={"codex": True}, providers={"codex": {"codex"}},
                                 codex_model="gpt-6.1-sol")
        candidates = failover.resolve("MAX", "high",
                                      [_spec("codex", "codex", None, "high", "MAX", "max")], health)
        self.assertEqual("gpt-6.1-sol", candidates[0]["model"])
        source = (ROOT / "scripts/common/orchestrator.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("gpt-6.1", source)
        self.assertNotIn("codex", source)

    def test_known_but_unverified_codex_is_unverified(self) -> None:
        health = failover.Health(binaries={"codex": True}, providers={"codex": {"codex"}},
                                 codex_model="gpt-6.1-sol")
        candidates = failover.resolve("MAX", "high",
                                      [_spec("codex", "codex", None, "high", "MAX", "max")], health)
        self.assertFalse(candidates[0]["verified"])


class CrossBackendResolutionTests(unittest.TestCase):
    def _health(self):
        return failover.Health(binaries={"opencode": True, "codex": True},
                               providers={"opencode": {"deepseek", "ollama"}, "codex": {"codex"}},
                               codex_model="gpt-6.1-sol",
                               local_inference=failover.NOT_CONFIGURED_BY_MACHINE_POLICY)

    def test_different_backends_satisfy_different_tiers(self) -> None:
        health = self._health()
        strong = failover.select(failover.resolve("STRONG", "high", [
            _spec("opencode", "deepseek", "deepseek-flash", "high", "STRONG", "high"),
            _spec("codex", "codex", None, "high", "STRONG", "high")], health))
        maximum = failover.select(failover.resolve("MAX", "max", [
            _spec("codex", "codex", None, "high", "MAX", "max"),
            _spec("opencode", "deepseek", "deepseek-flash", "max", "MAX", "max")], health))
        self.assertEqual("opencode", strong["backend"])
        self.assertEqual("codex", maximum["backend"])
        self.assertNotEqual(strong["backend"], maximum["backend"])

    def test_equivalent_backends_failover_same_tier(self) -> None:
        health = self._health()
        candidates = failover.resolve("STRONG", "high", [
            _spec("opencode", "deepseek", "deepseek-flash", "high", "STRONG", "high"),
            _spec("codex", "codex", None, "high", "STRONG", "high")], health)
        first = failover.select(candidates)
        replacement = failover.failover(candidates, first, health, "PROVIDER_503")
        self.assertEqual("codex", replacement["backend"])  # capability-preserving (same tier)

    def test_effort_floor_preserved(self) -> None:
        health = self._health()
        candidates = failover.resolve("STRONG", "high",
                                      [_spec("opencode", "deepseek", "m", "medium", "BALANCED", "medium")], health)
        self.assertFalse(candidates[0]["runnable"])
        self.assertIn("below_tier_floor", candidates[0]["blocking"])


class TwoCallCeilingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "repo"
        self.root.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "t"], check=True)
        (self.root / "f").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "i"], check=True, capture_output=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _plan(self):
        return {"schema_version": 1, "valid": True, "intent": "MUTATING", "order": ["T2", "T2S"],
                "nodes": [
                    {"task_id": "T2", "title": "arch", "objective_summary": "arch review",
                     "task_family": "multi_machine_acceptance", "task_class": "CORRECTNESS_SENSITIVE",
                     "depends_on": [], "conditional": False, "intent": "READ_ONLY", "executor": "AI",
                     "routing": {"tier": "STRONG", "effort": "high", "role": "teacher-cheap"},
                     "context": {"manifest": []}, "acceptance_criteria": ["ok"], "risk_signals": [],
                     "knowledge_refs": []},
                    {"task_id": "T2S", "title": "security", "objective_summary": "security review",
                     "task_family": "security_sensitive_design", "task_class": "CRITICAL",
                     "depends_on": ["T2"], "conditional": False, "intent": "READ_ONLY", "executor": "AI",
                     "routing": {"tier": "MAX", "effort": "max", "role": "teacher-premium"},
                     "context": {"manifest": []}, "acceptance_criteria": ["ok"], "risk_signals": [],
                     "knowledge_refs": []}]}

    @staticmethod
    def _scripted(name, results):
        backend = execution.FakeBackend(script={})
        backend.name = name
        backend._results = list(results)
        backend.execute = lambda request: (backend.calls.append({"task_id": request["task_id"],
                                                                 "backend": backend.name,
                                                                 "model": request["model"]}) or backend._results.pop(0))
        return backend

    def test_two_ai_nodes_within_two_call_ceiling_across_backends(self) -> None:
        opencode = self._scripted("opencode", [{"task_id": "T2", "status": "SUCCESS",
                                                "completion_signal": "agent_reported", "acceptance_results": {"ok": True}}])
        codex = self._scripted("codex", [{"task_id": "T2S", "status": "SUCCESS",
                                          "completion_signal": "agent_reported", "acceptance_results": {"ok": True}}])
        health = failover.Health(binaries={"opencode": True, "codex": True},
                                 providers={"opencode": {"deepseek"}, "codex": {"codex"}},
                                 codex_model="gpt-6.1-sol")
        by_tier = {"STRONG": [_spec("opencode", "deepseek", "deepseek-flash", "high", "STRONG", "high")],
                   "MAX": [_spec("codex", "codex", None, "high", "MAX", "max")]}
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier",
                                        side_effect=lambda tier, config=None: by_tier.get(tier, [])):
            report = execution.execute_plan(self._plan(), backend=opencode,
                                            backends={"opencode": opencode, "codex": codex}, health=health,
                                            repo_root=self.root, confirm=True, record_evidence=False,
                                            max_ai_calls=2)
        self.assertEqual("SUCCESS", report["nodes"]["T2"]["status"])
        self.assertEqual("SUCCESS", report["nodes"]["T2S"]["status"])
        self.assertEqual(2, report["calls_used"])
        self.assertEqual("opencode", opencode.calls[0]["backend"])
        self.assertEqual("codex", codex.calls[0]["backend"])
        self.assertEqual("gpt-6.1-sol", codex.calls[0]["model"])

    def test_third_call_blocked_by_ceiling(self) -> None:
        opencode = self._scripted("opencode", [{"task_id": "T2", "status": "SUCCESS",
                                                "completion_signal": "agent_reported", "acceptance_results": {"ok": True}}])
        codex = self._scripted("codex", [{"task_id": "T2S", "status": "FAILURE", "failure_category": "PROVIDER_503"}])
        health = failover.Health(binaries={"opencode": True, "codex": True},
                                 providers={"opencode": {"deepseek"}, "codex": {"codex"}}, codex_model="gpt-6.1-sol")
        by_tier = {"STRONG": [_spec("opencode", "deepseek", "deepseek-flash", "high", "STRONG", "high")],
                   "MAX": [_spec("codex", "codex", None, "high", "MAX", "max"),
                           _spec("opencode", "deepseek", "deepseek-flash", "max", "MAX", "max")]}
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier",
                                        side_effect=lambda tier, config=None: by_tier.get(tier, [])):
            report = execution.execute_plan(self._plan(), backend=opencode,
                                            backends={"opencode": opencode, "codex": codex}, health=health,
                                            repo_root=self.root, confirm=True, record_evidence=False,
                                            max_ai_calls=2, infra_retries=0)
        self.assertLessEqual(report["calls_used"], 2)  # codex 503 -> fallback blocked at ceiling

    def test_account_model_unsupported_is_non_intelligence(self) -> None:
        self.assertFalse(derived_knowledge.classify_failure("ACCOUNT_MODEL_UNSUPPORTED")
                         ["intelligence_escalation"])


if __name__ == "__main__":
    unittest.main()
