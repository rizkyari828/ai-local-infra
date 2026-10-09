"""Capability-preserving backend failover V0.1: runtime availability, capability
floors, circuit breaker, call budget interaction, user constraints, portability.
Fake backends/health only — no real AI calls."""

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
import execution  # noqa: E402
import derived_knowledge  # noqa: E402


def _spec(backend, provider, model, variant, tier="STRONG", effort="high"):
    return {"backend": backend, "provider": provider, "model": model,
            "native_variant": variant, "tier": tier, "effort": effort}


def _repo(tmp):
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


class AvailabilityTests(unittest.TestCase):
    def test_healthy_backend_runnable(self) -> None:
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"deepseek"}})
        candidates = failover.resolve("STRONG", "high", [_spec("opencode", "deepseek", "m", "high")], health)
        self.assertEqual(failover.BACKEND_HEALTHY, candidates[0]["status"])
        self.assertTrue(candidates[0]["runnable"])

    def test_backend_down(self) -> None:
        health = failover.Health(binaries={"opencode": False}, providers={"opencode": {"deepseek"}})
        candidates = failover.resolve("STRONG", "high", [_spec("opencode", "deepseek", "m", "high")], health)
        self.assertEqual(failover.BACKEND_UNAVAILABLE, candidates[0]["status"])
        self.assertFalse(candidates[0]["runnable"])

    def test_provider_unavailable(self) -> None:
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"deepseek"}})
        candidates = failover.resolve("STRONG", "high", [_spec("opencode", "ollama", "m", "high")], health)
        self.assertEqual(failover.PROVIDER_UNAVAILABLE, candidates[0]["status"])

    def test_model_not_installed(self) -> None:
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models={"other-model"})
        candidates = failover.resolve("FAST", "low", [_spec("opencode", "ollama", "qwen", "low", "FAST", "low")], health)
        self.assertEqual(failover.MODEL_UNAVAILABLE, candidates[0]["status"])

    def test_unknown_availability(self) -> None:
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models=None)
        candidates = failover.resolve("FAST", "low", [_spec("opencode", "ollama", "qwen", "low", "FAST", "low")], health)
        self.assertEqual(failover.UNKNOWN, candidates[0]["status"])
        self.assertFalse(candidates[0]["runnable"])

    def test_account_unsupported(self) -> None:
        health = failover.Health(binaries={"codex": True}, providers={"codex": {"codex"}},
                                 unavailable={"codex:codex": failover.ACCOUNT_MODEL_UNSUPPORTED})
        candidates = failover.resolve("MAX", "max", [_spec("codex", "codex", "gpt-x", "max", "MAX", "max")], health)
        self.assertEqual(failover.ACCOUNT_MODEL_UNSUPPORTED, candidates[0]["status"])

    def test_registry_presence_does_not_imply_runnable(self) -> None:
        # a candidate exists in config but the backend binary is absent
        health = failover.Health(binaries={"codex": False}, providers={"codex": {"codex"}}, codex_model="gpt-x")
        candidates = failover.resolve("MAX", "max", [_spec("codex", "codex", "gpt-x", "max", "MAX", "max")], health)
        self.assertFalse(candidates[0]["runnable"])


class FailoverTests(unittest.TestCase):
    def test_codex_rate_limited_fails_over_to_opencode(self) -> None:
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}},
                                 codex_model="gpt-x")
        specs = [_spec("codex", "codex", "gpt-x", "high"),
                 _spec("opencode", "deepseek", "deepseek-flash", "high")]
        candidates = failover.resolve("STRONG", "high", specs, health)
        selected = failover.select(candidates)
        self.assertEqual("codex", selected["backend"])
        nxt = failover.failover(candidates, selected, health, "RATE_LIMIT")
        self.assertEqual("opencode", nxt["backend"])
        self.assertTrue(health.is_tripped({"backend": "codex", "provider": "codex"}))
        self.assertFalse(failover.failover(candidates, selected, health, "REASONING_INSUFFICIENT"))

    def test_no_capability_preserving_fallback(self) -> None:
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"deepseek"}})
        # node requires STRONG; only a BALANCED candidate exists
        candidates = failover.resolve("STRONG", "high",
                                      [_spec("opencode", "deepseek", "m", "medium", "BALANCED", "medium")], health)
        self.assertFalse(candidates[0]["runnable"])
        self.assertIn("below_tier_floor", candidates[0]["blocking"])
        self.assertIsNone(failover.select(candidates))

    def test_reasoning_is_not_infra_failover(self) -> None:
        self.assertNotIn("REASONING_INSUFFICIENT", failover.FAILOVER_REASONS)


class CircuitBreakerTests(unittest.TestCase):
    def test_rate_limit_trips_then_next_node_skips(self) -> None:
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}},
                                 codex_model="gpt-x")
        specs = [_spec("codex", "codex", "gpt-x", "high"),
                 _spec("opencode", "deepseek", "deepseek-flash", "high")]
        first = failover.resolve("STRONG", "high", specs, health)
        failover.failover(first, failover.select(first), health, "RATE_LIMIT")
        # a subsequent node no longer selects the cooled-down backend
        second = failover.resolve("STRONG", "high", specs, health)
        self.assertEqual("opencode", failover.select(second)["backend"])
        self.assertFalse(health.is_tripped({"backend": "opencode", "provider": "deepseek"}))

    def test_no_permanent_blacklist(self) -> None:
        health = failover.Health(tripped={"codex"})
        self.assertTrue(health.is_tripped({"backend": "codex", "provider": "codex"}))
        fresh = failover.Health()  # per-run scope: a new run has no cooldown
        self.assertFalse(fresh.is_tripped({"backend": "codex", "provider": "codex"}))


class ConstraintTests(unittest.TestCase):
    def _candidates(self, **constraints):
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek", "ollama"}},
                                 ollama_models={"qwen"}, codex_model="gpt-x")
        specs = [_spec("codex", "codex", "gpt-x", "high"),
                 _spec("opencode", "deepseek", "deepseek-flash", "high")]
        return failover.resolve("STRONG", "high", specs, health, constraints=constraints)

    def test_no_premium_excludes_max(self) -> None:
        health = failover.Health(binaries={"codex": True}, providers={"codex": {"codex"}}, codex_model="gpt-x")
        candidates = failover.resolve("MAX", "max", [_spec("codex", "codex", "gpt-x", "max", "MAX", "max")],
                                      health, constraints={"no_premium": True})
        self.assertFalse(candidates[0]["runnable"])

    def test_local_only_excludes_remote(self) -> None:
        candidates = self._candidates(local_only=True)
        self.assertTrue(all(c["provider"] == "ollama" for c in candidates if c["runnable"]) or
                        failover.select(candidates) is None)

    def test_prefer_provider_orders_first(self) -> None:
        selected = failover.select(self._candidates(prefer_provider="opencode"))
        self.assertEqual("opencode", selected["backend"])


class ExecutionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _plan(self):
        return {"schema_version": 1, "valid": True, "intent": "MUTATING", "order": ["T1"],
                "nodes": [{"task_id": "T1", "title": "analyze", "objective_summary": "analyze routing",
                           "task_family": "multi_machine_acceptance",
                           "task_class": "CORRECTNESS_SENSITIVE", "depends_on": [], "conditional": False,
                           "intent": "READ_ONLY", "routing": {"tier": "STRONG", "effort": "high", "role": "teacher-cheap"},
                           "context": {"manifest": []}, "acceptance_criteria": ["ok"], "risk_signals": [],
                           "knowledge_refs": []}]}

    @staticmethod
    def _scripted(name, results):
        class B(execution.FakeBackend):
            pass
        backend = B(script={})
        backend.name = name
        backend._results = list(results)
        backend.execute = lambda request: (backend.calls.append({"task_id": request["task_id"],
                                                                 "backend": backend.name}) or backend._results.pop(0))
        return backend

    def test_rate_limited_backend_fails_over_within_budget(self) -> None:
        codex = self._scripted("codex", [{"task_id": "T1", "status": "FAILURE", "failure_category": "RATE_LIMIT"}])
        opencode = self._scripted("opencode", [{"task_id": "T1", "status": "SUCCESS",
                                               "completion_signal": "agent_reported", "acceptance_results": {"ok": True}}])
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}}, codex_model="gpt-x")
        specs = [_spec("codex", "codex", "gpt-x", "high"),
                 _spec("opencode", "deepseek", "deepseek-flash", "high")]
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", return_value=specs):
            report = execution.execute_plan(self._plan(), backend=opencode,
                                            backends={"opencode": opencode, "codex": codex},
                                            health=health, constraints={}, repo_root=self.root,
                                            confirm=True, record_evidence=False, infra_retries=0,
                                            max_ai_calls=3)
        self.assertEqual("SUCCESS", report["nodes"]["T1"]["status"])
        self.assertEqual(2, report["calls_used"])
        self.assertTrue(health.is_tripped({"backend": "codex", "provider": "codex"}))

    def test_unavailable_candidate_skipped_before_launch(self) -> None:
        # codex candidate is unrunnable; opencode selected without a wasted call
        codex = self._scripted("codex", [])
        opencode = self._scripted("opencode", [{"task_id": "T1", "status": "SUCCESS",
                                               "completion_signal": "agent_reported", "acceptance_results": {"ok": True}}])
        health = failover.Health(binaries={"codex": False, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}}, codex_model="gpt-x")
        specs = [_spec("codex", "codex", "gpt-x", "high"),
                 _spec("opencode", "deepseek", "deepseek-flash", "high")]
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", return_value=specs):
            report = execution.execute_plan(self._plan(), backend=opencode,
                                            backends={"opencode": opencode, "codex": codex},
                                            health=health, repo_root=self.root, confirm=True,
                                            record_evidence=False, max_ai_calls=3)
        self.assertEqual("SUCCESS", report["nodes"]["T1"]["status"])
        self.assertEqual([], codex.calls)  # unavailable backend never launched
        self.assertEqual(1, report["calls_used"])

    def test_no_capability_preserving_fallback_blocks(self) -> None:
        opencode = self._scripted("opencode", [])
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"deepseek"}})
        specs = [_spec("opencode", "deepseek", "m", "medium", "BALANCED", "medium")]
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", return_value=specs):
            report = execution.execute_plan(self._plan(), backend=opencode, health=health,
                                            repo_root=self.root, confirm=True, record_evidence=False)
        self.assertEqual("NO_CAPABILITY_PRESERVING_FALLBACK", report.get("error"))
        self.assertEqual([], opencode.calls)


class PortabilityTests(unittest.TestCase):
    def test_no_provider_branching_and_new_category(self) -> None:
        source = (ROOT / "scripts/common/execution.py").read_text(encoding="utf-8")
        for needle in ("if provider ==", "if provider==", "startswith(\"claude", "qwen"):
            self.assertNotIn(needle, source)
        self.assertFalse(derived_knowledge.classify_failure("NO_CAPABILITY_PRESERVING_FALLBACK")
                         ["intelligence_escalation"])

    def test_fictitious_providers_resolve_without_semantics_change(self) -> None:
        health = failover.Health(binaries={"b": True}, providers={"b": {"future-provider"}})
        candidates = failover.resolve("STRONG", "high",
                                      [_spec("b", "future-provider", "future-model-x", "high")], health)
        self.assertTrue(candidates[0]["runnable"])


if __name__ == "__main__":
    unittest.main()
